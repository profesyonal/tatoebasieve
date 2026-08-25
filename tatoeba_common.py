"""
Shared code between tatoebalist.py (one-time proper-name-variant list
builder) and tatoeba_sync.py (recurring additive list synchronizer).

Both scripts import from here rather than duplicating this logic, so a
fix or a new known name only has to be made in one place.
"""

import html.parser
import json
import os
import re
import tarfile
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE_URL = "https://tatoeba.org"
EXPORT_BASE = "https://downloads.tatoeba.org/exports"

# Both scripts download the same weekly corpus exports and the same
# names list, so they share one on-disk cache directory and never
# re-download data the other script already fetched.
#
# IMPORTANT: this is a *download cache* only. Each script keeps its
# own separate sqlite database (its own DB_PATH, defined in the
# script itself, not here) -- they must NOT share a database file,
# since each keeps different, incompatible derived-analysis state.
# (An earlier version of these scripts accidentally used the same
# DB_PATH for both; running one after the other in the same directory
# would silently wipe the other's data. Give each script its own
# database filename.)
DATA_DIR = Path("tatoeba_filter_data")
SENTENCES_ARCHIVE = DATA_DIR / "sentences.tar.bz2"
LINKS_ARCHIVE = DATA_DIR / "links.tar.bz2"
NAMES_ARCHIVE = DATA_DIR / "common-forenames-by-country.csv"

SENTENCES_URL = f"{EXPORT_BASE}/sentences.tar.bz2"
LINKS_URL = f"{EXPORT_BASE}/links.tar.bz2"

# CC0 dataset of popular forenames by country:
# https://github.com/sigpwned/popular-names-by-country-dataset
NAMES_URL = (
    "https://raw.githubusercontent.com/sigpwned/"
    "popular-names-by-country-dataset/refs/heads/main/"
    "common-forenames-by-country.csv"
)

# The downloaded names list is only top-N-per-country, so it misses
# some real names outright (e.g. Sudan isn't a covered country, and
# "Sami" doesn't appear in any covered country's top names). Add
# stragglers you notice here -- both scripts pick this up.
EXTRA_KNOWN_NAMES = {
    "sami",
    "fadil",
    "mennad",
    "yanni",
    "skura",
    "rima",
    "layla"
}

# Words like these often occur capitalized only because they begin a
# sentence. We don't want "The" -> "A" to look like a name change.
# Also includes common English words that double as common first
# names (Will, May, ...), which a names list would otherwise flag at
# sentence-initial position ("Will you...", "May I...").
INITIAL_NON_NAMES = {
    "a", "an", "the",
    "i", "you", "he", "she", "it", "we", "they",
    "this", "that", "these", "those",
    "who", "what", "where", "when", "why", "how",
    "yes", "no", "maybe", "perhaps", "one", "two", "three",
    "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
    "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
    # 21-99, matching TOKEN_RE's hyphenated tokenization ("twenty-one"
    # is a single token, so the compound form is what needs to be here).
    "twenty-one", "twenty-two", "twenty-three", "twenty-four", "twenty-five", "twenty-six", "twenty-seven", "twenty-eight", "twenty-nine",
    "thirty-one", "thirty-two", "thirty-three", "thirty-four", "thirty-five", "thirty-six", "thirty-seven", "thirty-eight", "thirty-nine",
    "forty-one", "forty-two", "forty-three", "forty-four", "forty-five", "forty-six", "forty-seven", "forty-eight", "forty-nine",
    "fifty-one", "fifty-two", "fifty-three", "fifty-four", "fifty-five", "fifty-six", "fifty-seven", "fifty-eight", "fifty-nine",
    "sixty-one", "sixty-two", "sixty-three", "sixty-four", "sixty-five", "sixty-six", "sixty-seven", "sixty-eight", "sixty-nine",
    "seventy-one", "seventy-two", "seventy-three", "seventy-four", "seventy-five", "seventy-six", "seventy-seven", "seventy-eight", "seventy-nine",
    "eighty-one", "eighty-two", "eighty-three", "eighty-four", "eighty-five", "eighty-six", "eighty-seven", "eighty-eight", "eighty-nine",
    "ninety-one", "ninety-two", "ninety-three", "ninety-four", "ninety-five", "ninety-six", "ninety-seven", "ninety-eight", "ninety-nine",
    "hundred", "thousand", "million", "billion",
    "hundreds", "thousands", "millions", "billions",
    # Ordinals, same range as the cardinals above. Only the final
    # component of a compound ordinal changes form ("twenty-first",
    # not "twentieth-first"), matching standard English usage.
    # Ordinals, kept short deliberately: past "tenth" they're not
    # realistic sentence-openers, so the compound/magnitude forms
    # that mirrored the cardinals above were cut.
    "first", "second", "third", "fourth", "fifth",
    "sixth", "seventh", "eighth", "ninth", "tenth",
    "love", "hate", "hatred", "dogs", "cats", "people",
    "will", "dawn", "may", "grace", "hope", "faith", "rose",
    "mark", "bill", "grant", "pat", "summer", "rich",
}

# Consecutive network/response-level failures for a single upload
# request (timeouts, connection errors, non-JSON responses) before
# giving up, separate from Tatoeba's own 429 rate-limit responses,
# which are expected and are instead retried indefinitely with a
# fixed backoff. This exists so a Tatoeba outage or maintenance
# window makes the script stop and say so, rather than hammering the
# server forever.
MAX_NETWORK_RETRIES = 10

TOKEN_RE = re.compile(
    r"""
    [^\W\d_]+(?:['’\-][^\W\d_]+)*
    |
    \d+
    |
    [^\w\s]
    """,
    re.UNICODE | re.VERBOSE,
)


# ============================================================
# HTML form parsing (login form, create-list form)
# ============================================================

class FormParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag == "form":
            self.current = {
                "action": attrs.get("action", ""),
                "method": attrs.get("method", "get").lower(),
                "fields": {},
            }

        elif tag == "input" and self.current is not None:
            name = attrs.get("name")
            if name:
                self.current["fields"][name] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None


def get_forms(text):
    parser = FormParser()
    parser.feed(text)
    return parser.forms


# ============================================================
# Downloads
# ============================================================

def refresh_download(url, path):
    """
    Download a file, using HTTP cache validators (ETag / Last-Modified)
    to skip the download entirely when the remote copy is unchanged.
    Works for the tar.bz2 corpus exports and the plain-CSV names list
    alike.
    """
    meta_path = Path(str(path) + ".http.json")
    headers = {}
    meta = {}

    if path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        except (OSError, json.JSONDecodeError):
            meta = {}

    print(f"Checking {url}")
    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        if r.status_code == 304:
            print(f"Unchanged: {path.name}")
            return

        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        temp = Path(str(path) + ".part")
        downloaded = 0

        with temp.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(
                        f"\r{path.name}: {downloaded / 1024**2:.1f} MB "
                        f"({downloaded * 100 / total:.1f}%)",
                        end="",
                    )
                else:
                    print(
                        f"\r{path.name}: {downloaded / 1024**2:.1f} MB",
                        end="",
                    )

        print()
        os.replace(temp, path)
        meta_path.write_text(
            json.dumps(
                {
                    "etag": r.headers.get("ETag"),
                    "last_modified": r.headers.get("Last-Modified"),
                }
            ),
            encoding="utf-8",
        )


def archive_lines(path):
    """Yield decoded lines from the first regular file in a .tar.bz2 export."""
    with tarfile.open(path, "r:bz2") as tar:
        member = next((m for m in tar if m.isfile()), None)
        if member is None:
            raise RuntimeError(f"No regular file found inside {path}")
        f = tar.extractfile(member)
        if f is None:
            raise RuntimeError(f"Could not extract {member.name} from {path}")
        for raw in f:
            yield raw.decode("utf-8", errors="replace").rstrip("\n")


# ============================================================
# Tokenization / proper-name heuristic
# ============================================================

def tokenize(text):
    return TOKEN_RE.findall(text)


def bare_word(token):
    return token.strip("'’\"“”‘’.,!?;:()[]{}«»‹›")


def has_case(word):
    return word.lower() != word.upper()


def capitalized_word(token):
    """
    Return True when token looks orthographically like a proper-name
    candidate in a language using letter case.
    """
    word = bare_word(token)

    if not word or not any(ch.isalpha() for ch in word):
        return False

    if not has_case(word):
        # Scripts without upper/lowercase cannot be handled by this
        # particular heuristic.
        return False

    if word.isupper() and len(word) > 1:
        return True

    return word[0].isupper() and any(ch.islower() for ch in word[1:])


def first_word_token_index(tokens):
    """
    Index of the first token that's actually a word, skipping any
    leading punctuation (a leading quote mark, etc.) that would
    otherwise get mistaken for -- or mask -- the sentence's true first
    word at position 0.
    """
    for i, token in enumerate(tokens):
        word = bare_word(token)
        if word and any(ch.isalpha() for ch in word):
            return i
    return None


def make_signature(tokens, span_start, span_end):
    """
    Replace tokens[span_start:span_end+1] -- a maximal run of probable
    proper-name tokens, e.g. both words of "Mary Ann" or "New York" --
    with a single placeholder. Everything else remains unchanged.
    A single-token span (span_start == span_end) behaves exactly as
    before.
    """
    result = []
    i = 0
    n = len(tokens)

    while i < n:
        if i == span_start:
            result.append("<PROPER_NAME>")
            i = span_end + 1
        else:
            result.append(tokens[i].casefold())
            i += 1

    return "\x1f".join(result)


def span_value(tokens, span_start, span_end):
    """The casefolded, space-joined text of a span, for comparing two
    sentences' name slots (e.g. "mary ann" vs "betty sue")."""
    return " ".join(
        bare_word(tokens[j]).casefold() for j in range(span_start, span_end + 1)
    )


def find_candidate_spans(tokens, first_word, is_candidate):
    """
    Group tokens into maximal runs of consecutive proper-name
    candidates, so a multi-word name ("Mary Ann", "New York", "Von
    Neumann") is treated as one swappable unit rather than N
    independently-swappable single-word slots -- which would
    otherwise miss "Mary Ann is nice." / "Betty Sue is nice." as
    variants of each other, and would wrongly let "Mary" and "Ann" be
    swapped independently of one another.

    is_candidate(token, is_first_word) -> bool is the per-token check
    (a script's own known_name_candidate, DB- or set-backed); this
    function only handles grouping consecutive True tokens together.
    Since TOKEN_RE never emits a token for whitespace, two words
    separated only by a space are already adjacent in `tokens`, so a
    run of consecutive candidate tokens is exactly a run of
    consecutive words -- while punctuation between two capitalized
    words (a comma in a list: "Mary, Ann, and Bob") inserts a
    non-candidate token between them and correctly breaks the run.
    """
    spans = []
    start = None

    for i, token in enumerate(tokens):
        if is_candidate(token, i == first_word):
            if start is None:
                start = i
        elif start is not None:
            spans.append((start, i - 1))
            start = None

    if start is not None:
        spans.append((start, len(tokens) - 1))

    return spans


def known_name_candidate(token, is_first_word, noninitial_caps, known_names):
    """
    Set-based version, for scripts that hold their capitalization
    evidence and names list as in-memory sets (e.g. tatoeba_sync.py,
    which fully rebuilds its analysis every run). A script whose
    evidence instead lives in SQLite tables -- e.g. tatoebalist.py,
    which keeps state across runs -- defines its own DB-query
    equivalent rather than using this one.
    """
    if not capitalized_word(token):
        return False

    word = bare_word(token).casefold()
    if not word:
        return False

    if not is_first_word:
        return True

    if word in INITIAL_NON_NAMES:
        return False

    # Sentence-initial capitalization alone is not evidence of a name.
    # Accept either corpus evidence (the same spelling occurs
    # capitalized later in an English sentence) or a known-names-list
    # match; each catches names the other misses.
    return word in noninitial_caps or word in known_names


# ============================================================
# Tatoeba actions
# ============================================================

def login(username, password, user_agent):
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})

    login_url = f"{BASE_URL}/en/users/login"
    r = session.get(login_url, timeout=60)
    r.raise_for_status()

    login_form = None
    for form in get_forms(r.text):
        if "check_login" in form["action"]:
            login_form = form
            break

    if login_form is None:
        raise RuntimeError("Could not locate Tatoeba's login form.")

    data = dict(login_form["fields"])
    data["username"] = username
    data["password"] = password
    data["rememberMe"] = "0"

    action = urljoin(BASE_URL, login_form["action"])
    r = session.post(action, data=data, timeout=60, allow_redirects=True)
    r.raise_for_status()

    # A failed login normally redirects back to the login page.
    if "/users/login" in r.url and (
        "UserLoginForm" in r.text or "Login failed" in r.text
    ):
        raise RuntimeError("Tatoeba login failed.")

    print("Logged into Tatoeba.")
    return session


def create_list(session, list_name):
    r = session.get(f"{BASE_URL}/en/sentences_lists/index", timeout=60)
    r.raise_for_status()

    list_form = None
    for form in get_forms(r.text):
        if re.search(r"/sentences_lists/add/?$", form["action"]):
            list_form = form
            break

    if list_form is None:
        raise RuntimeError("Could not find Tatoeba's create-list form.")

    data = dict(list_form["fields"])
    data["name"] = list_name

    action = urljoin(BASE_URL, list_form["action"])
    r = session.post(action, data=data, timeout=60, allow_redirects=True)
    r.raise_for_status()

    m = re.search(r"/sentences_lists/show/(\d+)", r.url)
    if not m:
        raise RuntimeError(
            "The list may have been created, but its ID could not be "
            "determined."
        )

    list_id = int(m.group(1))
    print(f"Created Tatoeba list #{list_id}: {list_name}")
    return list_id


def _sentence_list_action(session, action, list_id, sentence_id):
    """
    Shared GET-with-retry logic for add_sentence_to_list and
    remove_sentence_from_list. See add_one_sentence/remove_one_sentence
    docstrings for the retry/backoff policy this implements.
    """
    url = f"{BASE_URL}/en/sentences_lists/{action}/{sentence_id}/{list_id}"

    network_failures = 0

    while True:
        try:
            r = session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=60,
            )

            if r.status_code == 429:
                print(
                    "\nTatoeba rate-limited the script; "
                    "retrying after 60 seconds."
                )
                time.sleep(60)
                network_failures = 0
                continue

            r.raise_for_status()

            try:
                data = r.json()
            except ValueError as e:
                # A JSONDecodeError alone ("Expecting value: line 1
                # column 1 (char 0)") doesn't distinguish an empty
                # body from an HTML page landing where JSON was
                # expected -- surface what was actually returned so
                # this is self-diagnosing instead of a mystery.
                snippet = r.text[:200].replace("\n", " ")
                raise ValueError(
                    f"non-JSON response, status {r.status_code}, "
                    f"body: {snippet!r}"
                ) from e

            return data.get("result") != "error", data

        except (requests.RequestException, ValueError) as e:
            network_failures += 1
            print(
                f"\nNetwork/response error during {action} for sentence "
                f"#{sentence_id} ({network_failures}/{MAX_NETWORK_RETRIES}): {e}"
            )

            if network_failures >= MAX_NETWORK_RETRIES:
                raise RuntimeError(
                    f"{MAX_NETWORK_RETRIES} consecutive network/response "
                    f"errors during {action} for sentence #{sentence_id}. "
                    "Tatoeba may be down or under maintenance -- or, if this "
                    "is remove_sentence_from_list, the URL may simply be "
                    "wrong (see remove_one_sentence's docstring). Stopping "
                    "instead of retrying indefinitely."
                ) from e

            print("Retrying after 30 seconds.")
            time.sleep(30)


def add_one_sentence(session, list_id, sentence_id):
    """
    Add one sentence to a Tatoeba list.

    Tatoeba's own rate limiting (HTTP 429) is expected and
    self-resolving, so it's retried indefinitely with a fixed 60s
    backoff. Network/response-level failures (timeouts, connection
    errors, a non-JSON response such as a maintenance page) are
    different: they may mean Tatoeba is down, so they're capped at
    MAX_NETWORK_RETRIES before this raises, instead of retrying
    forever against a site that isn't coming back soon.

    This URL was confirmed directly from Tatoeba's own HTML (the
    add-to-list link on a sentence page uses this exact path).
    """
    return _sentence_list_action(
        session, "add_sentence_to_list", list_id, sentence_id
    )


def remove_one_sentence(session, list_id, sentence_id):
    """
    Remove one sentence from a Tatoeba list. Same retry/backoff policy
    as add_one_sentence.

    UNLIKE add_one_sentence, this URL is NOT independently confirmed --
    it's inferred from add_sentence_to_list's naming convention, since
    the removal control on Tatoeba's site requires an authenticated
    page view to inspect and wasn't reachable to verify directly.
    Tatoeba's own docs/blog confirm the *feature* exists, just not the
    exact route used here. Before relying on this in an unattended
    script: log into Tatoeba, open a list you don't mind experimenting
    on, remove one sentence via the website's UI, and check your
    browser's network tab for the actual request URL -- confirm it
    matches (or fix it here) before turning on ENABLE_REMOVALS in
    tatoeba_sync.py.
    """
    return _sentence_list_action(
        session, "remove_sentence_from_list", list_id, sentence_id
    )
