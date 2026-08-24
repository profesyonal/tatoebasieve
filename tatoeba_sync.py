#!/usr/bin/env python3

from array import array
import csv
import getpass
import os
import sqlite3
import sys
import time

from tatoeba_common import (
    BASE_URL,
    DATA_DIR,
    EXPORT_BASE,
    EXTRA_KNOWN_NAMES,
    LINKS_ARCHIVE,
    LINKS_URL,
    NAMES_ARCHIVE,
    NAMES_URL,
    SENTENCES_ARCHIVE,
    SENTENCES_URL,
    add_one_sentence,
    archive_lines,
    bare_word,
    capitalized_word,
    find_candidate_spans,
    first_word_token_index,
    login,
    make_signature,
    refresh_download,
    remove_one_sentence,
    span_value,
    tokenize,
    known_name_candidate,
)

USER_AGENT = (
    "TatoebaAdditiveListSynchronizer/1.0 "
    "(personal sentence-list maintenance script)"
)

TARGET_LANG = "eng"

# This script keeps its own database, separate from tatoebalist.py's --
# see the note in tatoeba_common.py on why that separation matters.
DB_PATH = DATA_DIR / "sync.sqlite3"
LISTS_ARCHIVE = DATA_DIR / "sentences_in_lists.tar.bz2"
TO_ADD_FILE = DATA_DIR / "to_add_sentence_ids.txt"
TO_REMOVE_FILE = DATA_DIR / "to_remove_sentence_ids.txt"

LISTS_URL = f"{EXPORT_BASE}/sentences_in_lists.tar.bz2"

# One request every half second by default. Increase this if Tatoeba asks you to.
UPLOAD_DELAY = 0.5
RECENT_UPLOAD_CACHE_DAYS = 14

# When a sentence already on the list is later found to be a
# proper-name-only variant of a better-ranked sentence (e.g. someone
# translated the sentence its "winning" variant was linked to, and
# that translation now outranks what's on the list), removal is
# computed and reported either way, but only actually SENT to Tatoeba
# when this is True. Keep this off until you've confirmed
# remove_one_sentence's URL against the real site -- see its
# docstring in tatoeba_common.py -- since it was inferred rather than
# confirmed from Tatoeba's HTML the way add_one_sentence's was.
ENABLE_REMOVALS = False


def connect_db():
    DATA_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=FILE")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sentences (
            id   INTEGER PRIMARY KEY,
            lang TEXT NOT NULL,
            text TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS effective_ids (
            sentence_id  INTEGER PRIMARY KEY,
            effective_id INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS effective_ids_rank
            ON effective_ids(effective_id, sentence_id);

        CREATE TABLE IF NOT EXISTS signatures (
            lang        TEXT NOT NULL,
            signature   TEXT NOT NULL,
            sentence_id INTEGER NOT NULL,
            name_value  TEXT NOT NULL,
            PRIMARY KEY (lang, signature)
        );

        CREATE TABLE IF NOT EXISTS excluded_variants (
            id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS excluded_linked (
            id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS existing_target (
            id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS upload_cache (
            list_id     INTEGER NOT NULL,
            sentence_id INTEGER NOT NULL,
            added_at    INTEGER NOT NULL,
            PRIMARY KEY (list_id, sentence_id)
        );
        """
    )
    return db


def prune_upload_cache(db):
    """
    upload_cache rows older than the recency window are never consulted
    again (see load_existing_target/upload_candidates), so drop them
    instead of letting the table grow forever.
    """
    cutoff = int(time.time()) - RECENT_UPLOAD_CACHE_DAYS * 86400
    deleted = db.execute(
        "DELETE FROM upload_cache WHERE added_at < ?", (cutoff,)
    ).rowcount
    db.commit()
    if deleted:
        print(f"Pruned {deleted:,} stale upload_cache rows.")


def reset_analysis(db):
    print("Resetting analysis tables for the current exports...")
    db.executescript(
        """
        DELETE FROM sentences;
        DELETE FROM effective_ids;
        DELETE FROM signatures;
        DELETE FROM excluded_variants;
        DELETE FROM excluded_linked;
        DELETE FROM existing_target;
        """
    )
    db.commit()


def import_target_language_sentences(db):
    """Import only English text, while still learning the corpus-wide max ID."""
    print(f"Importing {TARGET_LANG} sentences from the complete sentence export...")
    batch = []
    max_id = 0
    english_count = 0
    scanned = 0

    for line in archive_lines(SENTENCES_ARCHIVE):
        scanned += 1
        try:
            sid_s, lang, text = line.split("\t", 2)
            sid = int(sid_s)
        except ValueError:
            continue

        if sid > max_id:
            max_id = sid

        if lang == TARGET_LANG:
            batch.append((sid, lang, text))
            english_count += 1

        if len(batch) >= 50_000:
            db.executemany(
                "INSERT INTO sentences(id, lang, text) VALUES (?, ?, ?)", batch
            )
            db.commit()
            batch.clear()

        if scanned % 1_000_000 == 0:
            print(
                f"\rScanned {scanned:,} total sentences; "
                f"loaded {english_count:,} {TARGET_LANG}",
                end="",
            )

    if batch:
        db.executemany(
            "INSERT INTO sentences(id, lang, text) VALUES (?, ?, ?)", batch
        )
    db.commit()
    print(
        f"\rScanned {scanned:,} total sentences; "
        f"loaded {english_count:,} {TARGET_LANG}"
    )
    return max_id, english_count


def build_noninitial_caps(db):
    """
    Learn capitalization evidence from English only.
    German, Bavarian, Swabian, etc. never enter this set.
    """
    print("Finding words capitalized somewhere other than the first English word...")
    result = set()
    cursor = db.execute("SELECT id, text FROM sentences ORDER BY id")

    for n, (_, text) in enumerate(cursor, 1):
        tokens = tokenize(text)
        first_word = first_word_token_index(tokens)
        for i, token in enumerate(tokens):
            if i == first_word:
                continue
            if capitalized_word(token):
                word = bare_word(token).casefold()
                if word:
                    result.add(word)

        if n % 100_000 == 0:
            print(
                f"\rScanned {n:,} English sentences; "
                f"{len(result):,} capitalization candidates",
                end="",
            )

    print(
        f"\rScanned {n:,} English sentences; "
        f"{len(result):,} capitalization candidates"
    )
    return result


def load_known_names():
    """
    Load known first names from the downloaded dataset, plus the
    manually-maintained EXTRA_KNOWN_NAMES stragglers, as a set of
    casefolded single words (compound names are split, since matching
    happens one token at a time).
    """
    print("Loading known first names...")
    result = set(EXTRA_KNOWN_NAMES)

    with open(NAMES_ARCHIVE, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            for column in ("Localized Name", "Romanized Name"):
                value = (row.get(column) or "").strip()
                if not value:
                    continue
                for word in value.split():
                    word = word.casefold()
                    if word:
                        result.add(word)

    print(f"Loaded {len(result):,} known first names.")
    return result


def build_effective_ids(db, max_id):
    """
    effective_id(English sentence) = minimum of:
      * its own sentence ID
      * every sentence ID directly linked to it, in any language
    """
    print("Calculating effective IDs from all direct translation links...")

    if max_id <= 0:
        raise RuntimeError("Could not determine the maximum Tatoeba sentence ID.")

    effective = array("I", [0]) * (max_id + 1)
    if effective.itemsize < 4:
        raise RuntimeError("Unsigned-int array is unexpectedly too small.")

    for (sid,) in db.execute("SELECT id FROM sentences"):
        effective[sid] = sid

    scanned = 0
    for line in archive_lines(LINKS_ARCHIVE):
        try:
            a_s, b_s = line.split("\t", 1)
            a = int(a_s)
            b = int(b_s)
        except ValueError:
            continue

        scanned += 1
        if a < len(effective) and effective[a] and b < effective[a]:
            effective[a] = b
        if b < len(effective) and effective[b] and a < effective[b]:
            effective[b] = a

        if scanned % 1_000_000 == 0:
            print(f"\rScanned {scanned:,} links", end="")

    print(f"\rScanned {scanned:,} links")
    print("Writing effective IDs to SQLite...")

    batch = []
    for sid, value in enumerate(effective):
        if value:
            batch.append((sid, value))
        if len(batch) >= 50_000:
            db.executemany(
                "INSERT INTO effective_ids(sentence_id, effective_id) VALUES (?, ?)",
                batch,
            )
            db.commit()
            batch.clear()

    if batch:
        db.executemany(
            "INSERT INTO effective_ids(sentence_id, effective_id) VALUES (?, ?)",
            batch,
        )
    db.commit()


def find_name_variants(db, noninitial_caps, known_names):
    """
    Rank English sentences by (effective_id, sentence_id).
    A later-ranked sentence loses when it differs from an earlier-ranked one
    only at a probable proper-name position.
    """
    print("Finding English proper-name-only variants...")

    cursor = db.execute(
        """
        SELECT s.id, s.lang, s.text, e.effective_id
        FROM sentences s
        JOIN effective_ids e ON e.sentence_id = s.id
        ORDER BY e.effective_id ASC, s.id ASC
        """
    )

    excluded_count = 0

    for n, (sid, lang, text, effective_id) in enumerate(cursor, 1):
        tokens = tokenize(text)
        first_word = first_word_token_index(tokens)
        spans = find_candidate_spans(
            tokens,
            first_word,
            lambda tok, is_first: known_name_candidate(
                tok, is_first, noninitial_caps, known_names
            ),
        )
        candidates = [
            (make_signature(tokens, start, end), span_value(tokens, start, end))
            for start, end in spans
        ]

        # Do not let a sentence that loses one family become the canonical
        # representative of a different family.
        loses = False
        for signature, value in candidates:
            previous = db.execute(
                """
                SELECT sentence_id, name_value
                FROM signatures
                WHERE lang = ? AND signature = ?
                """,
                (lang, signature),
            ).fetchone()

            if previous is not None and previous[1] != value:
                loses = True
                break

        if loses:
            db.execute(
                "INSERT OR IGNORE INTO excluded_variants(id) VALUES (?)", (sid,)
            )
            excluded_count += 1
        else:
            for signature, value in candidates:
                db.execute(
                    """
                    INSERT OR IGNORE INTO signatures
                        (lang, signature, sentence_id, name_value)
                    VALUES (?, ?, ?, ?)
                    """,
                    (lang, signature, sid, value),
                )

        if n % 20_000 == 0:
            db.commit()
            print(
                f"\rChecked {n:,} English sentences; "
                f"excluded {excluded_count:,} name variants",
                end="",
            )

    db.commit()
    print(
        f"\rChecked {n:,} English sentences; "
        f"excluded {excluded_count:,} name variants"
    )


def exclude_direct_links_to_variants(db):
    """
    Exclude every sentence directly linked to an excluded name variant.
    This is deliberately one hop, not recursive graph propagation.
    """
    print("Excluding sentences directly linked to excluded variants...")
    excluded = {row[0] for row in db.execute("SELECT id FROM excluded_variants")}
    print(f"There are {len(excluded):,} excluded name variants.")

    batch = []
    scanned = 0
    for line in archive_lines(LINKS_ARCHIVE):
        try:
            a_s, b_s = line.split("\t", 1)
            a = int(a_s)
            b = int(b_s)
        except ValueError:
            continue

        scanned += 1
        if a in excluded:
            batch.append((b,))
        if b in excluded:
            batch.append((a,))

        if len(batch) >= 50_000:
            db.executemany(
                "INSERT OR IGNORE INTO excluded_linked(id) VALUES (?)", batch
            )
            db.commit()
            batch.clear()

        if scanned % 1_000_000 == 0:
            print(f"\rScanned {scanned:,} links", end="")

    if batch:
        db.executemany(
            "INSERT OR IGNORE INTO excluded_linked(id) VALUES (?)", batch
        )
    db.commit()
    print(f"\rScanned {scanned:,} links")


def load_existing_target(db, list_id):
    """
    Load current target-list membership from Tatoeba's weekly
    sentences_in_lists export, then supplement it with recent successful
    uploads from this script in case the export has not caught up yet.
    """
    print(f"Finding sentences already on Tatoeba list #{list_id}...")
    batch = []
    found = 0
    scanned = 0

    for line in archive_lines(LISTS_ARCHIVE):
        try:
            list_s, sentence_s = line.split("\t", 1)
            current_list_id = int(list_s)
            sentence_id = int(sentence_s)
        except ValueError:
            continue

        scanned += 1
        if current_list_id == list_id:
            batch.append((sentence_id,))
            found += 1

        if len(batch) >= 50_000:
            db.executemany(
                "INSERT OR IGNORE INTO existing_target(id) VALUES (?)", batch
            )
            db.commit()
            batch.clear()

        if scanned % 5_000_000 == 0:
            print(
                f"\rScanned {scanned:,} list-membership rows; "
                f"found {found:,} for target",
                end="",
            )

    if batch:
        db.executemany(
            "INSERT OR IGNORE INTO existing_target(id) VALUES (?)", batch
        )

    # The official export is weekly. Avoid re-attempting sentences this script
    # successfully added very recently but which may not be in the export yet.
    cutoff = int(time.time()) - RECENT_UPLOAD_CACHE_DAYS * 86400
    db.execute(
        """
        INSERT OR IGNORE INTO existing_target(id)
        SELECT sentence_id
        FROM upload_cache
        WHERE list_id = ? AND added_at >= ?
        """,
        (list_id, cutoff),
    )
    db.commit()

    actual = db.execute("SELECT COUNT(*) FROM existing_target").fetchone()[0]
    print(
        f"\rScanned {scanned:,} list-membership rows; "
        f"{actual:,} target IDs known after recent-upload cache"
    )
    return actual


def write_to_add_file(db):
    query = """
        SELECT s.id
        FROM sentences s
        WHERE NOT EXISTS (
            SELECT 1 FROM excluded_variants v WHERE v.id = s.id
        )
        AND NOT EXISTS (
            SELECT 1 FROM excluded_linked l WHERE l.id = s.id
        )
        AND NOT EXISTS (
            SELECT 1 FROM existing_target t WHERE t.id = s.id
        )
        ORDER BY s.id
    """

    count = 0
    with TO_ADD_FILE.open("w", encoding="utf-8") as f:
        for (sid,) in db.execute(query):
            f.write(f"{sid}\n")
            count += 1

    total = db.execute("SELECT COUNT(*) FROM sentences").fetchone()[0]
    variants = db.execute("SELECT COUNT(*) FROM excluded_variants").fetchone()[0]
    linked = db.execute(
        """
        SELECT COUNT(*)
        FROM excluded_linked l
        WHERE EXISTS (SELECT 1 FROM sentences s WHERE s.id = l.id)
        """
    ).fetchone()[0]
    already = db.execute(
        """
        SELECT COUNT(*)
        FROM existing_target t
        WHERE EXISTS (SELECT 1 FROM sentences s WHERE s.id = t.id)
        """
    ).fetchone()[0]

    print()
    print(f"English sentences in export:          {total:,}")
    print(f"Excluded proper-name variants:        {variants:,}")
    print(f"English sentences linked to variants: {linked:,}")
    print(f"English sentences already on list:    {already:,}")
    print(f"New qualifying sentences to add:      {count:,}")
    print(f"Candidate IDs written to: {TO_ADD_FILE}")
    return count


def write_to_remove_file(db):
    """
    Find sentences currently on the target list that this run's fresh
    analysis now excludes -- typically because a newly-created (or
    newly-linked) translation elsewhere in the corpus now outranks
    them for the same proper-name signature. Scoped to English
    sentences this script actually imported, so a non-English or
    otherwise out-of-scope entry someone added to the list by hand is
    never touched.
    """
    query = """
        SELECT t.id
        FROM existing_target t
        JOIN sentences s ON s.id = t.id
        WHERE EXISTS (
            SELECT 1 FROM excluded_variants v WHERE v.id = t.id
        )
        OR EXISTS (
            SELECT 1 FROM excluded_linked l WHERE l.id = t.id
        )
        ORDER BY t.id
    """

    count = 0
    with TO_REMOVE_FILE.open("w", encoding="utf-8") as f:
        for (sid,) in db.execute(query):
            f.write(f"{sid}\n")
            count += 1

    print(f"List entries superseded by a better-ranked variant: {count:,}")
    print(f"Candidate removal IDs written to: {TO_REMOVE_FILE}")
    return count


def upload_candidates(db, session, list_id):
    total = sum(1 for _ in TO_ADD_FILE.open(encoding="utf-8"))
    if total == 0:
        print("Nothing new to add.")
        return

    print(f"Adding {total:,} new qualifying sentences to list #{list_id}...")
    added = 0
    errors = 0
    consecutive_errors = 0

    with TO_ADD_FILE.open(encoding="utf-8") as f:
        for processed, line in enumerate(f, 1):
            sid = int(line.strip())

            # The local cache makes interrupted uploads resumable without
            # waiting for the next weekly sentences_in_lists export.
            cutoff = int(time.time()) - RECENT_UPLOAD_CACHE_DAYS * 86400
            cached = db.execute(
                """
                SELECT 1
                FROM upload_cache
                WHERE list_id = ? AND sentence_id = ? AND added_at >= ?
                """,
                (list_id, sid, cutoff),
            ).fetchone()
            if cached:
                continue

            ok, response = add_one_sentence(session, list_id, sid)

            if ok:
                now = int(time.time())
                db.execute(
                    """
                    INSERT OR REPLACE INTO upload_cache(list_id, sentence_id, added_at)
                    VALUES (?, ?, ?)
                    """,
                    (list_id, sid, now),
                )
                db.execute(
                    "INSERT OR IGNORE INTO existing_target(id) VALUES (?)", (sid,)
                )
                db.commit()
                added += 1
                consecutive_errors = 0
            else:
                errors += 1
                consecutive_errors += 1
                print(f"\nTatoeba refused sentence #{sid}: {response}")

                if consecutive_errors >= 20:
                    raise RuntimeError(
                        "20 consecutive additions failed. Check that the list ID "
                        "exists and that this account can edit it."
                    )

            if processed % 100 == 0:
                print(
                    f"\rProcessed {processed:,}/{total:,}; "
                    f"added {added:,}; errors {errors:,}",
                    end="",
                )

            time.sleep(UPLOAD_DELAY)

    print(
        f"\rProcessed {total:,}/{total:,}; added {added:,}; errors {errors:,}"
    )


def upload_removals(db, session, list_id):
    total = sum(1 for _ in TO_REMOVE_FILE.open(encoding="utf-8"))
    if total == 0:
        print("Nothing to remove.")
        return

    print(f"Removing {total:,} superseded sentences from list #{list_id}...")
    removed = 0
    errors = 0
    consecutive_errors = 0

    with TO_REMOVE_FILE.open(encoding="utf-8") as f:
        for processed, line in enumerate(f, 1):
            sid = int(line.strip())

            ok, response = remove_one_sentence(session, list_id, sid)

            if ok:
                db.execute("DELETE FROM existing_target WHERE id = ?", (sid,))
                db.execute(
                    "DELETE FROM upload_cache WHERE list_id = ? AND sentence_id = ?",
                    (list_id, sid),
                )
                db.commit()
                removed += 1
                consecutive_errors = 0
            else:
                errors += 1
                consecutive_errors += 1
                print(f"\nTatoeba refused to remove sentence #{sid}: {response}")

                if consecutive_errors >= 20:
                    raise RuntimeError(
                        "20 consecutive removals failed. Check that the list ID "
                        "exists, that this account can edit it, and that "
                        "remove_one_sentence's URL is actually correct (see its "
                        "docstring in tatoeba_common.py)."
                    )

            if processed % 100 == 0:
                print(
                    f"\rProcessed {processed:,}/{total:,}; "
                    f"removed {removed:,}; errors {errors:,}",
                    end="",
                )

            time.sleep(UPLOAD_DELAY)

    print(
        f"\rProcessed {total:,}/{total:,}; removed {removed:,}; errors {errors:,}"
    )


def get_credentials():
    """
    For cron/systemd/other unattended runs, read credentials from the
    environment: TATOEBA_USERNAME, TATOEBA_PASSWORD, TATOEBA_LIST_ID.
    Interactively, fall back to the original prompts. If neither a
    terminal nor the full set of environment variables is available,
    fail fast with a clear message instead of hanging on input().
    """
    username = os.environ.get("TATOEBA_USERNAME")
    password = os.environ.get("TATOEBA_PASSWORD")
    list_id_str = os.environ.get("TATOEBA_LIST_ID")

    if username and password and list_id_str:
        try:
            list_id = int(list_id_str)
        except ValueError:
            sys.exit("TATOEBA_LIST_ID must be an integer.")
        return username, password, list_id

    if not sys.stdin.isatty():
        sys.exit(
            "No terminal attached, and TATOEBA_USERNAME / TATOEBA_PASSWORD / "
            "TATOEBA_LIST_ID are not all set in the environment. Set them "
            "(e.g. in the cron entry or a wrapper script) or run this "
            "script interactively."
        )

    username = input("Tatoeba username: ").strip()
    password = getpass.getpass("Tatoeba password: ")

    try:
        list_id = int(input("Existing Tatoeba list ID: ").strip())
    except ValueError:
        sys.exit("List ID must be an integer.")

    return username, password, list_id


def main():
    print("Tatoeba additive English-list synchronizer")
    if ENABLE_REMOVALS:
        print(
            "List entries superseded by a better-ranked variant "
            "WILL be removed (ENABLE_REMOVALS is on).\n"
        )
    else:
        print(
            "List entries are never removed while ENABLE_REMOVALS is off "
            "-- superseded entries are only reported.\n"
        )

    username, password, list_id = get_credentials()

    if not username or not password or list_id <= 0:
        sys.exit("Username, password, and a positive list ID are required.")

    DATA_DIR.mkdir(exist_ok=True)

    # Refresh all data relevant to the decision and to current list membership.
    refresh_download(SENTENCES_URL, SENTENCES_ARCHIVE)
    refresh_download(LINKS_URL, LINKS_ARCHIVE)
    refresh_download(LISTS_URL, LISTS_ARCHIVE)
    refresh_download(NAMES_URL, NAMES_ARCHIVE)

    db = connect_db()
    try:
        prune_upload_cache(db)
        reset_analysis(db)
        max_id, _ = import_target_language_sentences(db)
        noninitial_caps = build_noninitial_caps(db)
        known_names = load_known_names()
        build_effective_ids(db, max_id)
        find_name_variants(db, noninitial_caps, known_names)
        exclude_direct_links_to_variants(db)
        load_existing_target(db, list_id)
        to_add = write_to_add_file(db)
        to_remove = write_to_remove_file(db)

        if to_add == 0 and (not ENABLE_REMOVALS or to_remove == 0):
            print("The list is already up to date with the current export.")
            if to_remove:
                print(
                    f"({to_remove:,} superseded entries would be removed "
                    "if ENABLE_REMOVALS were on.)"
                )
            return

        session = login(username, password, USER_AGENT)

        if to_add:
            upload_candidates(db, session, list_id)

        if ENABLE_REMOVALS:
            if to_remove:
                upload_removals(db, session, list_id)
        elif to_remove:
            print(
                f"\n{to_remove:,} list entry/entries would be removed if "
                "ENABLE_REMOVALS were on -- not removed."
            )

        print()
        print(f"Synchronization complete for list #{list_id}.")
        print(f"List: {BASE_URL}/en/sentences_lists/show/{list_id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
