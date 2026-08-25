# Tatoeba Sieve

Filters out proper-name-only duplicate sentences from Tatoeba and
keeps a Tatoeba list in sync with the result. Two scripts, plus a
shared module they both import from.

Tatoeba's English sentences include a lot of structurally identical
"template" sentences that differ only in which character name was
used -- `Tom is happy.` / `Mary is happy.` / `Sami is happy.` and so
on. These scripts detect that pattern and keep only one representative
sentence per template, preferring whichever version has been in the
corpus the longest (see [How it decides what to
keep](#how-it-decides-what-to-keep) below).

## Files

- **`tatoeba_common.py`** -- shared code: HTTP/download helpers,
  tokenizing and proper-name-detection logic, and the Tatoeba list
  actions (login, add/remove a sentence, create a list). Both scripts
  below import from this; it isn't run directly.
- **`tatoebalist.py`** -- one-time list builder. Downloads the full
  Tatoeba corpus, filters it down to non-redundant English sentences,
  and creates (or resumes adding to) a Tatoeba list from the result.
  Interactive only.
- **`tatoeba_sync.py`** -- recurring additive synchronizer. Re-runs
  the same filtering logic against the *current* corpus export and
  adds any new qualifying sentences to an *existing* list. Never
  deletes anything from the list unless `ENABLE_REMOVALS` is turned on
  (see [Removal](#removal-enable_removals) below). Supports
  unattended/cron use via environment variables.

## Setup

Requires Python 3.8+.

```bash
pip install -r requirements.txt
```

## How it decides what to keep

For each English sentence, capitalized words are treated as probable
proper-name candidates -- consecutive capitalized words ("Mary Ann",
"New York") are grouped into a single unit rather than compared word
by word. A word at the very start of a sentence needs corroborating
evidence (it's on a known first-names list, or the same spelling shows
up capitalized elsewhere in the corpus) before it counts, since every
sentence starts with a capital letter regardless of whether the first
word is a name.

Sentences are then compared by their "signature" -- the sentence with
the name-like span replaced by a placeholder. Two sentences with the
same signature but a different name are variants of each other, and
only one is kept: whichever is linked (directly or via translation
chains) to the oldest sentence ID in the corpus, so translating an old
sentence into English doesn't lose out to an unrelated, newer English
near-duplicate.

`tatoeba_common.py`'s `INITIAL_NON_NAMES` set is a hand-maintained list
of common English words that are also common first names (`Will`,
`May`, `Grace`, ...) or that would otherwise look like sentence-initial
name candidates (number words, `Love`, `People`, ...) -- extend it if
you spot a new false positive.

**Known limitation:** consecutive-capitalized-word merging only
merges *capitalized* words. A name with a lowercase connector, like
"Von der Leyen", still splits into two spans around "der".

## `tatoebalist.py`

```bash
python3 tatoebalist.py
```

Prompts for your Tatoeba username, password, and (on first run) a name
for the new list. Downloads the corpus, runs the filtering, and
uploads the result. Safe to interrupt and rerun -- it resumes both the
analysis and the upload from where it left off, and reuses the same
list on subsequent runs rather than creating a new one each time.

## `tatoeba_sync.py`

```bash
python3 tatoeba_sync.py
```

Prompts for your Tatoeba username, password, and an *existing* list
ID. For unattended use (e.g. cron), set these environment variables
instead and it will skip the prompts:

```bash
TATOEBA_USERNAME=youruser
TATOEBA_PASSWORD=yourpass
TATOEBA_LIST_ID=12345
```

If neither the environment variables nor a terminal are available, it
exits immediately with an explanation rather than hanging on `input()`.

### Removal (`ENABLE_REMOVALS`)

Occasionally a sentence already on the list is later found to be a
name-only variant of something else -- typically because a new
translation elsewhere in the corpus now outranks it. `tatoeba_sync.py`
detects this and always *reports* it (see `to_remove_sentence_ids.txt`
after a run), but only actually removes it from Tatoeba when
`ENABLE_REMOVALS = True` near the top of the file.

This defaults to `False` on purpose: unlike the add-sentence endpoint,
which was confirmed directly from Tatoeba's own HTML, the
remove-sentence endpoint's URL was inferred from that naming
convention and has not been independently confirmed. Before turning
this on, verify the URL yourself -- log into Tatoeba, remove one
sentence from a list via the website, and check your browser's network
tab for the actual request -- and fix `remove_one_sentence()` in
`tatoeba_common.py` if it doesn't match. See that function's docstring
for details.

### Example cron entry

Runs every Saturday at noon Eastern, adjusting automatically across
DST:

```
TZ=America/New_York
0 12 * * 6 TATOEBA_USERNAME=youruser TATOEBA_PASSWORD=yourpass TATOEBA_LIST_ID=12345 sh -c 'cd /path/to/scripts && /usr/bin/python3 tatoeba_sync.py >> tatoeba_sync.log 2>&1'
```

## Rate limiting and retries

Both scripts self-throttle between requests (`UPLOAD_DELAY` near the
top of each file) and back off indefinitely on Tatoeba's own 429
rate-limit responses, since that's expected and self-resolving.
Network/response-level failures (timeouts, connection errors, a
non-JSON response such as a maintenance page) are treated differently
and capped at `MAX_NETWORK_RETRIES` (in `tatoeba_common.py`) before
raising, so a genuine Tatoeba outage stops the script instead of
retrying forever.
