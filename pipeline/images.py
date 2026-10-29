"""Fallback images for cards that TCGdex has no scan for (pure functions).

TCGdex has no image for about 7.6 % of the catalog (all Trainer Gallery,
Galarian Gallery and Shiny Vault cards, MEP promos, trainer kits ...). For
those cards only, the pipeline looks for another image, first rule that applies:

  1. A TCGdex twin (``twin_images``): TCGdex lists some cards twice, e.g. the
     XY "a" alternates as ``xya-24a`` (no scan) and ``xy4-24a`` (scan). When
     another card has a TCGdex image, the same TCGplayer product, the same
     number and a similar name, its TCGdex image base becomes this card's
     ``"i"`` (still assets.tcgdex.net).
  2. pokemontcg.io ``images.large`` (``https://images.pokemontcg.io/<set>/<n>_hires.png``,
     734 x 1024 PNG; the newest sets use ``https://images.scrydex.com/pokemon/<id>/large``,
     654 x 914 PNG, because pokemontcg.io is run by Scrydex); the card is found
     by set mapping + number + name.
  3. Scrydex under the same pokemontcg.io card id (``scrydex_url``): some
     images.pokemontcg.io files answer 404 (all of McDonald's 2014, 2015, 2017
     and 2018, a few promos) while Scrydex serves the card at
     ``https://images.scrydex.com/pokemon/<pokemontcg.io id>/large``. The card
     was already matched by number and name, so only the file is new.

Until v0.3 TCGplayer's own product photos (its CDN) were a second source. They
were dropped on the client's IP decision (2026-10-29, research.md §3.4 in the
Evodex app repository): TCGplayer's terms bar using its content elsewhere
without written permission, so TCGplayer is credited for prices only. Only
``ALLOWED_HOSTS`` may appear in ``im`` (validate_feed.py rejects a feed with
any other host).

Every chosen URL is HEAD-checked once (run.py, cached in
``cache/images/status.json``); a URL that does not answer 200 is skipped and
the next source is tried. Scrydex answers 200 with a card-back placeholder for
ids it does not know, so a Scrydex HEAD is compared with the HEAD of an id that
cannot exist (``PLACEHOLDER_PROBES``, ``is_placeholder``): same ETag = no image.
A derived Scrydex URL (``VERIFY_SOURCES``) is used only after such a check said
200; a URL from pokemontcg.io's own record is also used while unchecked
(``--no-image-check``, offline runs). The result is the ``im`` field of index.json.

Set mapping (TCGdex set -> pokemontcg.io set), first rule that applies:
  1. ``overrides.json -> ptcgioSets`` (``null`` = known to have no counterpart)
  2. same ``set_id_key`` ("sv03.5" == "sv3pt5", "swsh12.5gg" == "swsh12pt5gg")
     and (similar name or release dates within 120 days)
  3. same ``set_name_key`` and release dates within 60 days
     ("McDonald's Collection 2021": 2021swsh == mcd21, "Best of game": bog == bp)

Card matching inside a mapped set: candidates are the pokemontcg.io cards whose
``number_key`` equals the card's localId key or the key of its TCGplayer
printed number (Classic Collection "CC002" is printed "4/102"); the best one by
``name_similarity`` is taken when it scores >= 0.5 and is unique.
"""

import datetime
import re
import urllib.parse
from collections import defaultdict

from normalize import name_similarity, number_key, set_name_key, set_name_similarity

PTCGIO_IMAGE_HOSTS = ("images.pokemontcg.io", "images.scrydex.com")
ALLOWED_HOSTS = PTCGIO_IMAGE_HOSTS      # the only hosts an "im" URL may use (the app allow-lists the same)
TCGDEX_IMAGE_HOST = "assets.tcgdex.net"  # the only host a custom "i" (TCGdex image base) may use
SOURCES = ("ptcgio", "scrydex")         # "im" sources, in preference order
VERIFY_SOURCES = ("scrydex",)           # guessed URLs: used only after a HEAD check said 200
SCRYDEX_IMAGE = "https://images.scrydex.com/pokemon/%s/large"
# A HEAD of an id that cannot exist; a URL whose HEAD carries the same ETag is that placeholder.
PLACEHOLDER_PROBES = {"images.scrydex.com": SCRYDEX_IMAGE % "evodex-no-such-card"}
MIN_NAME = 0.5

_PT_RE = re.compile(r"(\d)pt(\d)")
_LEADING_ZEROS_RE = re.compile(r"(?<!\d)0+(?=\d)")


def set_id_key(set_id):
    """Comparable set id: "sv03.5" / "sv3pt5" -> "sv35", "swsh12.5gg" /
    "swsh12pt5gg" -> "swsh125gg", "sm3.5" / "sm35" -> "sm35", "sv01" -> "sv1"."""
    s = _PT_RE.sub(r"\1\2", (set_id or "").lower()).replace(".", "")
    return _LEADING_ZEROS_RE.sub("", s)


def _iso(date):
    """pokemontcg.io dates are "2022/02/25"."""
    return (date or "").replace("/", "-")[:10]


def _days_apart_ok(a, b, days):
    if not a or not b:
        return False
    try:
        da = datetime.date.fromisoformat(_iso(a))
        db = datetime.date.fromisoformat(_iso(b))
    except ValueError:
        return False
    return abs((da - db).days) <= days


def map_sets(dex_sets, ptcg_sets, overrides=None):
    """dex_sets: [{id, name, releaseDate}] (TCGdex), ptcg_sets: [{id, name,
    releaseDate}] (pokemontcg.io). Returns {dexSetId: (ptcgSetId | None, method)}
    for every TCGdex set; method is "override", "id", "name" or "none"."""
    overrides = overrides or {}
    by_id = {p["id"]: p for p in ptcg_sets}
    by_key = defaultdict(list)
    by_name = defaultdict(list)
    for p in ptcg_sets:
        by_key[set_id_key(p["id"])].append(p)
        by_name[set_name_key(p["name"])].append(p)
    out = {}
    for s in dex_sets:
        sid = s["id"]
        if sid in overrides:
            target = overrides[sid]
            out[sid] = (target if target in by_id else None, "override")
            continue
        hit = [p for p in by_key.get(set_id_key(sid), [])
               if set_name_similarity(s["name"], p["name"]) >= 0.5
               or _days_apart_ok(s.get("releaseDate"), p.get("releaseDate"), 120)]
        if len(hit) == 1:
            out[sid] = (hit[0]["id"], "id")
            continue
        hit = [p for p in by_name.get(set_name_key(s["name"]), [])
               if _days_apart_ok(s.get("releaseDate"), p.get("releaseDate"), 60)]
        if len(hit) == 1:
            out[sid] = (hit[0]["id"], "name")
            continue
        out[sid] = (None, "none")
    return out


def large_image(ptcg_card):
    imgs = ptcg_card.get("images") or {}
    return imgs.get("large") or imgs.get("small")


def scrydex_url(ptcg_card):
    """Scrydex's file for a pokemontcg.io card id ("mcd14-2" ->
    https://images.scrydex.com/pokemon/mcd14-2/large), or None without an id."""
    pid = ptcg_card.get("id")
    return SCRYDEX_IMAGE % urllib.parse.quote(pid, safe="") if pid else None


def candidates(ptcg_card):
    """[(source, url)] for a matched pokemontcg.io card, in preference order:
    the record's own image, then the same card id on Scrydex."""
    out = [("ptcgio", large_image(ptcg_card))]
    derived = scrydex_url(ptcg_card)
    if derived and derived != out[0][1]:
        out.append(("scrydex", derived))
    return out


def placeholder_signature(headers):
    """What identifies a host's placeholder: its ETag, else its byte size."""
    etag, length = headers.get("etag"), headers.get("content-length")
    if etag:
        return ("etag", etag)
    return ("content-length", length) if length else None


def is_placeholder(headers, signature):
    """True when a 200 HEAD (lower-cased ``headers``) is the placeholder that
    ``signature`` (``placeholder_signature`` of the probe) describes."""
    if not signature:
        return False
    kind, value = signature
    return headers.get(kind) == value


def twin_images(cards):
    """cards: [{id, localId, name, image, products: [productId...]}] (the whole
    catalog). Returns {cardId: (TCGdex image base, twin cardId)} for cards
    without an image that have a twin with one: same TCGplayer product, same
    number, similar name (>= MIN_NAME), and a unique best twin."""
    by_product = defaultdict(list)
    for c in cards:
        if c.get("image"):
            for pid in c.get("products") or ():
                by_product[pid].append(c)
    out = {}
    for c in cards:
        if c.get("image"):
            continue
        key = number_key(c["localId"])
        seen, scored = set(), []
        for pid in c.get("products") or ():
            for t in by_product.get(pid, ()):
                if t["id"] in seen or number_key(t["localId"]) != key:
                    continue
                seen.add(t["id"])
                scored.append((name_similarity(c["name"], t["name"]), t))
        scored.sort(key=lambda st: -st[0])
        if not scored or scored[0][0] < MIN_NAME:
            continue
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            continue   # ambiguous: two twins fit equally well
        out[c["id"]] = (scored[0][1]["image"], scored[0][1]["id"])
    return out


def match_cards(cards, ptcg_cards):
    """cards: [{id, localId, name, printedNumber?}] of one TCGdex set;
    ptcg_cards: the mapped pokemontcg.io set. Returns {cardId: ptcgCard}."""
    by_key = defaultdict(list)
    for p in ptcg_cards:
        by_key[number_key(p.get("number"))].append(p)
    out = {}
    for c in cards:
        keys = [number_key(c["localId"])]
        if c.get("printedNumber"):
            keys.append(number_key(c["printedNumber"]))
        cands, seen = [], set()
        for k in keys:
            for p in by_key.get(k, []):
                if p["id"] not in seen:
                    seen.add(p["id"])
                    cands.append(p)
        scored = sorted(((name_similarity(c["name"], p["name"]), p) for p in cands),
                        key=lambda sp: -sp[0])
        if not scored or scored[0][0] < MIN_NAME:
            continue
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            continue   # ambiguous: two pokemontcg.io cards fit equally well
        out[c["id"]] = scored[0][1]
    return out


THUMBNAIL_SUFFIXES = (("_hires.png", ".png"),               # images.pokemontcg.io, 245 x 342
                      ("/large", "/small"))                  # images.scrydex.com, 245 x 342


def thumbnail_url(url):
    """Smaller variant of a fallback URL for grids (the rule the app applies,
    docs/architecture/data-sources.md "Fallback images" in the Evodex app
    repository); unknown shapes are returned unchanged."""
    for large, small in THUMBNAIL_SUFFIXES:
        if url.endswith(large):
            return url[:-len(large)] + small
    return url


def allowed(url):
    """True for an https URL on one of ``ALLOWED_HOSTS``."""
    u = urllib.parse.urlparse(url or "")
    return u.scheme == "https" and u.netloc in ALLOWED_HOSTS


def allowed_tcgdex(url):
    """True for an https TCGdex image base (a custom ``"i"`` in index.json)."""
    u = urllib.parse.urlparse(url or "")
    return u.scheme == "https" and u.netloc == TCGDEX_IMAGE_HOST


def choose(candidates, status):
    """candidates: [(source, url | None)] in preference order; status(url) ->
    HTTP status, or None when it was never checked. Returns (source, url) of
    the first allowed URL (``allowed``) that is 200, or unchecked when its
    source is not in ``VERIFY_SOURCES``; else (None, None)."""
    for source, url in candidates:
        if not url or not allowed(url):
            continue
        code = status(url)
        if code == 200 or (code is None and source not in VERIFY_SOURCES):
            return source, url
    return None, None
