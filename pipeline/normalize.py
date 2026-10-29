"""Pure normalization helpers shared by set mapping and card matching.

Everything here is deliberately regex-light and table-free so it can be ported
1:1 to Swift (see the app repository's docs/architecture/data-sources.md, "Matching algorithm").
"""

import re
import unicodedata
import urllib.parse

_NUM_RE = re.compile(r"^([A-Z]*?)-?0*(\d+)([A-Z]*)$")


def number_key(raw):
    """Canonical collector-number key used to join TCGdex and TCGplayer.

    Rules (in order):
      1. percent-decode, trim, upper-case     ("%3F" -> "?", "sv001" -> "SV001")
      2. keep only the part before "/"        ("025/165" -> "025", "TG01/TG30" -> "TG01")
      3. drop spaces and "#"                  ("PW 1" -> "PW1")
      4. if it looks like PREFIX + DIGITS + SUFFIX, strip leading zeros from
         the digits                           ("SV001" -> "SV1", "007" -> "7",
                                               "SWSH001" -> "SWSH1", "18a" -> "18A")
      5. otherwise return as-is               ("A" -> "A", "!" -> "!", "ONE" -> "ONE")
    """
    if raw is None:
        return ""
    s = urllib.parse.unquote(str(raw)).strip().upper()
    s = s.split("/")[0]
    s = s.replace(" ", "").replace("#", "")
    m = _NUM_RE.match(s)
    if m:
        prefix, digits, suffix = m.groups()
        return "%s%d%s" % (prefix, int(digits), suffix)
    return s


def number_prefix(key):
    """Letter prefix of a number key: "TG1" -> "TG", "25" -> "", "SV1" -> "SV"."""
    m = re.match(r"^([A-Z]*)\d", key)
    return m.group(1) if m else ""


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


_PAREN_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
# " - 025/165", " - SWSH001", " - 18a/147", " - TG01/TG30" appended by TCGplayer
_NUM_SUFFIX_RE = re.compile(r"\s+-\s+[A-Za-z]*-?\d+[A-Za-z]*(/[A-Za-z-]*\d*[A-Za-z-]*)?(?=\s|\(|\[|$)")
# Words TCGplayer appends to every product of some groups.
_NOISE_SUFFIX_RE = re.compile(r"\s+(Classic Collection|Promo)\s*$", re.I)
# Name tokens that describe the card mechanic, not the subject. Two names that
# share only these ("Mew ex" / "Scizor ex") are not similar at all.
MECHANIC_TOKENS = {"ex", "gx", "v", "vmax", "vstar", "vunion", "lv", "x", "break", "prime",
                   "legend", "star", "m", "mega", "tag", "team", "c", "g", "gl", "fb", "4",
                   "e4", "radiant", "shining", "dark", "light", "energy", "s"}


def product_qualifier(name):
    """Text TCGplayer puts in brackets to distinguish printings of one card:
    "Pikachu - 025/165 (Cosmos Holo)" -> "Cosmos Holo";
    "Acerola - 112a/147 (League Challenge) [1st Place]" -> "League Challenge, 1st Place".
    """
    parts = re.findall(r"\(([^)]*)\)|\[([^\]]*)\]", name or "")
    return ", ".join(p[0] or p[1] for p in parts).strip()


def name_key(name):
    """Loose name key for tie-breaking: lower-case ASCII words only.

    "Charizard ex" -> "charizard ex", "Flabébé" -> "flabebe",
    "Chimecho δ" and "Chimecho (Delta Species)" -> "chimecho",
    "Pikachu - 025/165 (Cosmos Holo)" -> "pikachu", "Nidoran♀" -> "nidoran f".
    """
    s = _PAREN_RE.sub(" ", name or "")
    s = _NUM_SUFFIX_RE.sub("", s).strip()
    s = _NOISE_SUFFIX_RE.sub("", s)
    s = s.replace("δ", " ").replace("♀", " f ").replace("♂", " m ")
    s = s.replace("&", " and ").replace("’", "'")
    s = strip_accents(s).lower()
    s = s.replace("'s ", "s ").replace("delta species", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def name_similarity(a, b):
    """1.0 equal keys; 0.8 when one token set contains the other; otherwise
    token Jaccard. 0.0 whenever the names share no subject token (tokens in
    MECHANIC_TOKENS do not count), so "Mew ex" vs "Scizor ex" is 0.0."""
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    ta, tb = set(ka.split()), set(kb.split())
    if not (ta & tb) - MECHANIC_TOKENS and (ta - MECHANIC_TOKENS) and (tb - MECHANIC_TOKENS):
        return 0.0
    if ta <= tb or tb <= ta:
        return 0.8
    return len(ta & tb) / float(len(ta | tb))


# ---------------------------------------------------------------- set names

# TCGplayer group-name prefixes that carry no identity: "SWSH09: ", "SV: ",
# "ME01: ", "XY - ", "SM - ", "SWSH: ", "EX ", "SV01: " ...
_GROUP_PREFIX_RE = re.compile(r"^(?:(?:SWSH|SV|SM|XY|ME|MEE|SVE)\d*\s*[:\-]\s*)+")


def set_name_key(name):
    """Normalize a set/group name for comparison.

    "SWSH09: Brilliant Stars Trainer Gallery" -> "brilliant stars trainer gallery"
    "XY - Flashfire" -> "flashfire",  "EX Ruby and Sapphire" -> "ruby and sapphire",
    "Ruby & Sapphire" -> "ruby and sapphire", "SV: Scarlet & Violet 151" ->
    "scarlet and violet 151", "Pokémon GO" -> "go".
    """
    s = strip_accents(name or "")
    s = _GROUP_PREFIX_RE.sub("", s.strip())
    s = s.replace("&", " and ")
    s = s.lower()
    s = re.sub(r"[^a-z0-9.]+", " ", s)
    words = s.split()
    # drop the leading "EX" era tag TCGplayer uses ("EX Dragon" == "Dragon")
    if len(words) > 1 and words[0] == "ex":
        words = words[1:]
    return " ".join(w for w in words if w not in ("pokemon", "the"))


def set_name_similarity(a, b):
    ka, kb = set_name_key(a), set_name_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    ta, tb = set(ka.split()), set(kb.split())
    jac = len(ta & tb) / float(len(ta | tb))
    if ta <= tb or tb <= ta:
        return max(jac, 0.75)
    return jac


# ---------------------------------------------------------------- card names

# Scripts that never belong in an English card name. Greek letters (δ, α, β, γ)
# and symbols (☆ ★ ◇ ♀ ♂) are part of real English names and are kept.
_FOREIGN_SCRIPTS = ("CJK", "HIRAGANA", "KATAKANA", "HALFWIDTH KATAKANA", "HANGUL",
                    "CYRILLIC", "THAI", "ARABIC", "HEBREW")
_BRACKETED_RE = re.compile(r"[\[(（【]([^\]\)）】]*)[\])）】]")


def is_foreign_char(ch):
    if not unicodedata.category(ch).startswith("L"):
        return False
    return unicodedata.name(ch, "").startswith(_FOREIGN_SCRIPTS)


def has_foreign_script(s):
    return any(is_foreign_char(ch) for ch in s or "")


def clean_card_name(name):
    """English display name for a TCGdex card name (QA-17).

    Only names that contain a non-Latin script are changed:
      1. a bracketed part without foreign script is the English name TCGdex
         appended: "ナッシー[Exeggutor]" -> "Exeggutor",
         "ピカチュウ（Pikachu）" -> "Pikachu";
      2. otherwise the foreign-script runs are removed and leftover empty
         brackets / spaces cleaned: "Pikachu ピカチュウ" -> "Pikachu";
      3. if nothing Latin is left, the name is returned unchanged (that card
         needs an overrides.json "names" entry; run.py reports it).
    Names without foreign script are returned unchanged, so "Unown [A]",
    "Ancient Technical Machine [Ice]", "Chimecho δ" and "Umbreon ☆" stay.
    """
    if not name or not has_foreign_script(name):
        return name
    for inner in _BRACKETED_RE.findall(name):
        inner = " ".join(inner.split())
        if inner and not has_foreign_script(inner) and re.search(r"[A-Za-z]", inner):
            return inner
    s = "".join(" " if is_foreign_char(ch) else ch for ch in name)
    s = re.sub(r"[\[(（【]\s*[\])）】]", " ", s)
    s = " ".join(s.split())
    return s if re.search(r"[A-Za-z]", s) else name
