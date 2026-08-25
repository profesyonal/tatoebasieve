#!/usr/bin/env python3

import csv
import getpass
import sqlite3
import sys
import time
from array import array
from pathlib import Path

from tatoeba_common import (
    BASE_URL,
    DATA_DIR,
    EXTRA_KNOWN_NAMES,
    INITIAL_NON_NAMES,
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
    create_list,
    find_candidate_spans,
    first_word_token_index,
    login,
    make_signature,
    refresh_download,
    span_value,
    tokenize,
)

USER_AGENT = (
    "TatoebaSentenceListFilter/1.0 "
    "(personal Tatoeba corpus-management script)"
)

# This script keeps its own database, separate from tatoeba_sync.py's --
# see the note in tatoeba_common.py on why that separation matters.
DB_PATH = DATA_DIR / "list_builder.sqlite3"

OUTPUT_IDS = Path("kept_sentence_ids.txt")
UPLOAD_PROGRESS = Path("tatoeba_upload_progress.txt")
LIST_ID_FILE = Path("tatoeba_list_id.txt")

# Be polite to Tatoeba when adding items to the list.
UPLOAD_DELAY = 0.05


# ============================================================
# Database
# ============================================================

def connect_db():
    DATA_DIR.mkdir(exist_ok=True)

    db = sqlite3.connect(DB_PATH)

    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=FILE")

    db.executescript("""
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
            ON effective_ids(effective_id);

        CREATE TABLE IF NOT EXISTS noninitial_caps (
            word TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS known_names (
            word TEXT PRIMARY KEY
        );

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

        CREATE INDEX IF NOT EXISTS signatures_sentence
            ON signatures(sentence_id);
    """)

    return db


# ============================================================
# Import sentences
# ============================================================

def import_sentences(db):
    count = db.execute(
        "SELECT COUNT(*) FROM sentences"
    ).fetchone()[0]

    if count:
        print(f"Sentence database already contains {count:,} sentences.")
        return

    print("Importing the complete sentence corpus...")

    batch = []

    for n, line in enumerate(archive_lines(SENTENCES_ARCHIVE), 1):
        try:
            sid, lang, text = line.split("\t", 2)
            batch.append((int(sid), lang, text))
        except ValueError:
            continue

        if len(batch) >= 50_000:
            db.executemany(
                "INSERT OR IGNORE INTO sentences(id, lang, text) "
                "VALUES (?, ?, ?)",
                batch,
            )
            db.commit()
            batch.clear()
            print(f"\rImported {n:,} sentences", end="")

    if batch:
        db.executemany(
            "INSERT OR IGNORE INTO sentences(id, lang, text) "
            "VALUES (?, ?, ?)",
            batch,
        )

    db.commit()
    print(f"\rImported {n:,} sentences")


# ============================================================
# Import known first names
# ============================================================

def import_known_names(db):
    count = db.execute(
        "SELECT COUNT(*) FROM known_names"
    ).fetchone()[0]

    if count:
        print(f"Known-names table already contains {count:,} names.")
        return

    print("Importing known first names...")

    batch = []

    with open(NAMES_ARCHIVE, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            for column in ("Localized Name", "Romanized Name"):
                value = (row.get(column) or "").strip()

                if not value:
                    continue

                # Compound names ("Ana Maria", "Juan Carlos") are
                # matched one token at a time downstream, so split
                # them the same way here.
                for word in value.split():
                    word = word.casefold()
                    if word:
                        batch.append((word,))

            if len(batch) >= 50_000:
                db.executemany(
                    "INSERT OR IGNORE INTO known_names(word) VALUES (?)",
                    batch,
                )
                db.commit()
                batch.clear()

    for word in EXTRA_KNOWN_NAMES:
        batch.append((word.casefold(),))

    if batch:
        db.executemany(
            "INSERT OR IGNORE INTO known_names(word) VALUES (?)",
            batch,
        )

    db.commit()

    count = db.execute(
        "SELECT COUNT(*) FROM known_names"
    ).fetchone()[0]

    print(f"Imported {count:,} known first names.")


# ============================================================
# Learn which capitalized words occur away from sentence start
# ============================================================

def build_noninitial_capitalization_table(db):
    print("Finding capitalized words inside English sentences...")

    # Rebuild it so an old database made with the previous version
    # doesn't retain capitalization evidence from other languages.
    db.execute("DELETE FROM noninitial_caps")
    db.commit()

    cursor = db.execute(
        """
        SELECT id, text
        FROM sentences
        WHERE lang = 'eng'
        ORDER BY id
        """
    )

    batch = []

    for n, (_, text) in enumerate(cursor, 1):
        tokens = tokenize(text)
        first_word = first_word_token_index(tokens)

        for i, token in enumerate(tokens):
            if i == first_word:
                continue
            if capitalized_word(token):
                word = bare_word(token).casefold()
                batch.append((word,))

        if len(batch) >= 50_000:
            db.executemany(
                "INSERT OR IGNORE INTO noninitial_caps(word) VALUES (?)",
                batch,
            )
            db.commit()
            batch.clear()

        if n % 100_000 == 0:
            print(f"\rScanned {n:,} English sentences", end="")

    if batch:
        db.executemany(
            "INSERT OR IGNORE INTO noninitial_caps(word) VALUES (?)",
            batch,
        )

    db.commit()
    print(f"\rScanned {n:,} English sentences")


def known_name_candidate(db, token, is_first_word):
    if not capitalized_word(token):
        return False

    word = bare_word(token).casefold()

    if not word:
        return False

    if not is_first_word:
        return True

    # Sentence-initial capitalization alone tells us almost nothing.
    if word in INITIAL_NON_NAMES:
        return False

    # A sentence-initial word becomes a name candidate if either:
    # (a) the same spelling occurs capitalized somewhere inside
    #     another sentence, or
    # (b) it's a known first name from an external names list.
    # Keeping (a) matters even with (b) present: a names list will
    # never be fully complete, and corpus evidence catches names
    # the list doesn't cover.
    row = db.execute(
        "SELECT 1 FROM noninitial_caps WHERE word = ?",
        (word,),
    ).fetchone()

    if row is not None:
        return True

    row = db.execute(
        "SELECT 1 FROM known_names WHERE word = ?",
        (word,),
    ).fetchone()

    return row is not None


def build_effective_ids(db):
    """
    For every English sentence, effective_id is:

        min(
            its own sentence ID,
            every sentence ID directly linked to it
        )

    Linked sentences may be in ANY language.

    Thus, if English sentence #300 is directly linked to Spanish #100,
    #300 has effective_id 100.
    """

    print("Calculating effective IDs for English sentences...")

    db.execute("DELETE FROM effective_ids")
    db.commit()

    max_id = db.execute(
        "SELECT MAX(id) FROM sentences"
    ).fetchone()[0]

    if max_id is None:
        raise RuntimeError("Sentence database is empty.")

    # An array indexed by sentence ID is much more memory-efficient
    # than a Python dict containing millions of English sentence IDs.
    #
    # 0 = not an English sentence
    # n = current effective ID for English sentence n
    effective = array("I", [0]) * (max_id + 1)

    if effective.itemsize < 4:
        raise RuntimeError(
            "This Python installation's unsigned-int array "
            "is too small for sentence IDs."
        )

    english_count = 0

    for sid, in db.execute(
        "SELECT id FROM sentences WHERE lang = 'eng'"
    ):
        if sid >= len(effective):
            raise RuntimeError(
                f"Sentence ID #{sid} exceeds allocated array."
            )

        effective[sid] = sid
        english_count += 1

    print(f"Loaded {english_count:,} English sentence IDs.")
    print("Scanning all direct Tatoeba links...")

    scanned = 0

    for line in archive_lines(LINKS_ARCHIVE):
        try:
            a, b = line.split("\t", 1)
            a = int(a)
            b = int(b)
        except ValueError:
            continue

        scanned += 1

        # a is English: b can be any language.
        if a < len(effective) and effective[a]:
            if b < effective[a]:
                effective[a] = b

        # b is English: a can be any language.
        if b < len(effective) and effective[b]:
            if a < effective[b]:
                effective[b] = a

        if scanned % 1_000_000 == 0:
            print(
                f"\rScanned {scanned:,} direct links",
                end="",
            )

    print(f"\rScanned {scanned:,} direct links")

    print("Writing effective IDs to database...")

    batch = []

    for sid, value in enumerate(effective):
        if value:
            batch.append((sid, value))

        if len(batch) >= 50_000:
            db.executemany(
                """
                INSERT INTO effective_ids(sentence_id, effective_id)
                VALUES (?, ?)
                """,
                batch,
            )
            db.commit()
            batch.clear()

    if batch:
        db.executemany(
            """
            INSERT INTO effective_ids(sentence_id, effective_id)
            VALUES (?, ?)
            """,
            batch,
        )

    db.commit()

    print("Effective IDs calculated.")

# ============================================================
# Find proper-name-only variants
# ============================================================

def find_name_variants(db):
    print("Searching English sentences for proper-name-only variants...")

    # Clear results made by an earlier version of the algorithm.
    db.execute("DELETE FROM signatures")
    db.execute("DELETE FROM excluded_variants")
    db.commit()

    cursor = db.execute(
        """
        SELECT
            s.id,
            s.lang,
            s.text,
            e.effective_id
        FROM sentences s
        JOIN effective_ids e
            ON e.sentence_id = s.id
        WHERE s.lang = 'eng'
        ORDER BY
            e.effective_id ASC,
            s.id ASC
        """
    )

    found = 0

    for n, (sid, lang, text, effective_id) in enumerate(cursor, 1):
        tokens = tokenize(text)
        first_word = first_word_token_index(tokens)
        spans = find_candidate_spans(
            tokens,
            first_word,
            lambda tok, is_first: known_name_candidate(db, tok, is_first),
        )
        candidates = [
            (make_signature(tokens, start, end), span_value(tokens, start, end))
            for start, end in spans
        ]

        # Check every span before committing any of them. A sentence
        # that loses on ANY span must not leave behind a signature
        # registration from one of its OTHER spans -- otherwise a
        # sentence that ends up excluded can still act as the
        # canonical representative for a family it was never actually
        # kept in, wrongly excluding later, unrelated sentences that
        # happen to share that span's literal context.
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
                "INSERT OR IGNORE INTO excluded_variants(id) VALUES (?)",
                (sid,),
            )
            found += 1
        else:
            # Because the query is ordered by effective_id, the first
            # sentence registered for each signature is the preferred
            # one.
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
                f"excluded {found:,} name variants",
                end="",
            )

    db.commit()

    print(
        f"\rChecked {n:,} English sentences; "
        f"excluded {found:,} name variants"
    )


# ============================================================
# Exclude direct translations/links
# ============================================================

def exclude_linked_sentences(db):
    print("Finding sentences directly linked to excluded variants...")

    db.execute("DELETE FROM excluded_linked")
    db.commit()

    excluded = {
        row[0]
        for row in db.execute("SELECT id FROM excluded_variants")
    }

    print(f"Starting with {len(excluded):,} excluded variants.")

    batch = []
    scanned = 0

    for line in archive_lines(LINKS_ARCHIVE):
        try:
            a, b = line.split("\t", 1)
            a = int(a)
            b = int(b)
        except ValueError:
            continue

        scanned += 1

        if a in excluded:
            batch.append((b,))

        if b in excluded:
            batch.append((a,))

        if len(batch) >= 50_000:
            db.executemany(
                "INSERT OR IGNORE INTO excluded_linked(id) VALUES (?)",
                batch,
            )
            db.commit()
            batch.clear()

        if scanned % 1_000_000 == 0:
            print(
                f"\rScanned {scanned:,} translation links",
                end="",
            )

    if batch:
        db.executemany(
            "INSERT OR IGNORE INTO excluded_linked(id) VALUES (?)",
            batch,
        )

    db.commit()
    print(f"\rScanned {scanned:,} translation links")


# ============================================================
# Produce final local list
# ============================================================

def write_kept_ids(db):
    excluded_variants = db.execute(
        "SELECT COUNT(*) FROM excluded_variants"
    ).fetchone()[0]

    excluded_linked = db.execute(
        """
        SELECT COUNT(*)
        FROM excluded_linked l
        WHERE NOT EXISTS (
            SELECT 1
            FROM excluded_variants v
            WHERE v.id = l.id
        )
        """
    ).fetchone()[0]

    total = db.execute(
        "SELECT COUNT(*) FROM sentences"
    ).fetchone()[0]

    total_english = db.execute(
        "SELECT COUNT(*) FROM sentences WHERE lang = 'eng'"
    ).fetchone()[0]

    print()
    print(f"Total sentences:              {total:,}")
    print(f"Total English sentences:      {total_english:,}")
    print(f"Later proper-name variants:   {excluded_variants:,}")
    print(f"Linked sentences excluded:    {excluded_linked:,}")

    query = """
        SELECT s.id
        FROM sentences s
        WHERE s.lang = 'eng'
        AND NOT EXISTS (
            SELECT 1
            FROM excluded_variants v
            WHERE v.id = s.id
        )
        AND NOT EXISTS (
            SELECT 1
            FROM excluded_linked l
            WHERE l.id = s.id
        )
        ORDER BY s.id
    """

    kept = 0

    with open(OUTPUT_IDS, "w", encoding="utf-8") as f:
        for (sid,) in db.execute(query):
            f.write(f"{sid}\n")
            kept += 1

    print(f"English sentences remaining:  {kept:,}")
    print(f"Wrote IDs to: {OUTPUT_IDS}")

    return kept


# ============================================================
# Create Tatoeba list
# ============================================================

def get_or_create_list(session, list_name):
    """
    Reuse the list from a previous run if one was already created;
    otherwise create a new list and remember its ID for next time.
    """
    if LIST_ID_FILE.exists():
        list_id = int(LIST_ID_FILE.read_text().strip())
        print(f"Resuming existing Tatoeba list #{list_id}.")
        return list_id

    list_id = create_list(session, list_name)
    LIST_ID_FILE.write_text(str(list_id), encoding="utf-8")

    return list_id


# ============================================================
# Add sentences to Tatoeba list
# ============================================================

def upload_ids(session, list_id):
    start_after = 0

    if UPLOAD_PROGRESS.exists():
        try:
            start_after = int(
                UPLOAD_PROGRESS.read_text().strip()
            )
            print(
                f"Resuming after sentence #{start_after}."
            )
        except ValueError:
            start_after = 0

    total = sum(1 for _ in open(OUTPUT_IDS, encoding="utf-8"))
    added = 0
    errors = 0
    skipped = 0
    consecutive_errors = 0

    with open(OUTPUT_IDS, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            sid = int(line.strip())

            if sid <= start_after:
                skipped += 1
                continue

            ok, response = add_one_sentence(session, list_id, sid)

            UPLOAD_PROGRESS.write_text(str(sid), encoding="utf-8")

            if ok:
                added += 1
                consecutive_errors = 0
            else:
                errors += 1
                consecutive_errors += 1
                print(f"\nTatoeba refused sentence #{sid}: {response}")

                if consecutive_errors >= 20:
                    raise RuntimeError(
                        "20 consecutive additions failed. Check that the "
                        "list ID exists and that this account can edit it."
                    )

            if n % 100 == 0:
                print(
                    f"\rProcessed {n:,}/{total:,}; "
                    f"added {added:,}; errors {errors:,}",
                    end="",
                )

            time.sleep(UPLOAD_DELAY)

    print()
    print(
        f"Finished: added {added:,}; errors {errors:,}; "
        f"skipped {skipped:,} already past the resume point."
    )


# ============================================================
# Main
# ============================================================

def main():
    print("Tatoeba sieve - proper-name variant list builder")
    print()

    resuming_list = LIST_ID_FILE.exists()

    username = input("Tatoeba username: ").strip()
    password = getpass.getpass("Tatoeba password: ")

    list_name = ""
    if not resuming_list:
        list_name = input(
            "Name for the new Tatoeba list: "
        ).strip()

    if not username or not password or (not resuming_list and not list_name):
        sys.exit("Username, password, and list name are required.")

    DATA_DIR.mkdir(exist_ok=True)

    # Download the entire corpus, plus a known-first-names list.
    refresh_download(SENTENCES_URL, SENTENCES_ARCHIVE)
    refresh_download(LINKS_URL, LINKS_ARCHIVE)
    refresh_download(NAMES_URL, NAMES_ARCHIVE)

    db = connect_db()

    try:
        import_sentences(db)
        import_known_names(db)
        build_noninitial_capitalization_table(db)
        build_effective_ids(db)
        find_name_variants(db)
        exclude_linked_sentences(db)
        kept = write_kept_ids(db)

    finally:
        db.close()

    print()
    print(f"{kept:,} sentence IDs are ready for the list.")

    # Authentication is deliberately done only after all the
    # expensive local processing has succeeded.
    session = login(username, password, USER_AGENT)

    list_id = get_or_create_list(session, list_name)

    print(
        f"List URL: "
        f"{BASE_URL}/en/sentences_lists/show/{list_id}"
    )

    upload_ids(session, list_id)


if __name__ == "__main__":
    main()
