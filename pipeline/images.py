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

  4. TCGplayer's own product photo of the card's deep-link product (p[0].id),
     ``https://tcgplayer-cdn.tcgplayer.com/product/<id>_in_1000x1000.jpg``
     (``tcgplayer_url``), while the feed's switch is on (``images.json``
     ``{"tcgplayerFallback": true}``, evodex-feed only; ``load_config``).
     v0.3 dropped this source (TCGplayer's terms, research.md §3.4 in the
     Evodex app repository); client decision 2026-10-29: TCGplayer photos are
     used for every card with no TCGdex or pokemontcg.io/Scrydex image (those
     stay first: better scans), the client is joining the TCGplayer affiliate
     program, and the switch is insurance only. The app shows these URLs only
     while the manifest says ``images.tcgplayerFallback: true``.

``ALLOWED_HOSTS`` may always appear in ``im``; TCGplayer only in the exact
shape above, only on a card without a TCGdex image and only while the switch
is on (``tcgplayer_image_problems``; publish_feed.py and validate_feed.py
refuse anything else).

Every chosen URL is HEAD-checked once (run.py, cached in
``cache/images/status.json``); a URL that does not answer 200 is skipped and
the next source is tried. Scrydex answers 200 with a card-back placeholder for
ids it does not know, so a Scrydex HEAD is compared with the HEAD of an id that
cannot exist (``PLACEHOLDER_PROBES``, ``is_placeholder``): same ETag = no image.
A derived Scrydex URL (``VERIFY_SOURCES``) is used only after such a check said
200; a URL from pokemontcg.io's own record is also used while unchecked
(``--no-image-check``, offline runs). The result is the ``im`` field of index.json.
TCGplayer has no 200 placeholder for a missing photo: its CDN answers 403 for a
product without one (checked 2026-10-29: 160 of the 674 candidates, all trainer
kit cards, and an id that cannot exist). A stock picture filed under several
products would still answer 200, so a TCGplayer HEAD also records the ETag, and
a file whose ETag products of *differently named* cards share counts as a
placeholder (``stock_etags``). Byte-identical photos of the same card are
normal: a trainer kit lists its Lightning Energy at ten numbers. TCGplayer URLs,
too, are used only after a check said 200.

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
import json
import os
import re
import urllib.parse
from collections import defaultdict

from normalize import name_key, name_similarity, number_key, set_name_key, set_name_similarity

PTCGIO_IMAGE_HOSTS = ("images.pokemontcg.io", "images.scrydex.com")
ALLOWED_HOSTS = PTCGIO_IMAGE_HOSTS      # the hosts an "im" URL may always use (the app allow-lists the same)
TCGDEX_IMAGE_HOST = "assets.tcgdex.net"  # the only host a custom "i" (TCGdex image base) may use
# TCGplayer's product photos: "im" only while the switch is on (images.json), in exactly this shape.
TCGPLAYER_IMAGE_HOST = "tcgplayer-cdn.tcgplayer.com"
TCGPLAYER_IMAGE = "https://tcgplayer-cdn.tcgplayer.com/product/%d_in_1000x1000.jpg"
_TCGPLAYER_IMAGE_RE = re.compile(r"https://tcgplayer-cdn\.tcgplayer\.com/product/([1-9][0-9]{0,9})_in_1000x1000\.jpg")
SOURCES = ("ptcgio", "scrydex", "tcgplayer")   # "im" sources, in preference order
VERIFY_SOURCES = ("scrydex", "tcgplayer")      # used only after a HEAD check said 200
SCRYDEX_IMAGE = "https://images.scrydex.com/pokemon/%s/large"
# A HEAD of an id that cannot exist; a URL whose HEAD carries the same ETag is that placeholder.
PLACEHOLDER_PROBES = {"images.scrydex.com": SCRYDEX_IMAGE % "evodex-no-such-card"}
# Hosts whose HEAD ETag is recorded (status.json entry [code, time, note, etag]) for stock_etags.
ETAG_HOSTS = (TCGPLAYER_IMAGE_HOST,)
# Seconds between HEAD requests per host (default 0.1, i.e. at most 10/s).
HEAD_INTERVAL = {TCGPLAYER_IMAGE_HOST: 0.25}
MIN_NAME = 0.5
# The TCGplayer switch (evodex-feed only; feed_sync.py lists it as FEED_ONLY): {"tcgplayerFallback": true}.
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images.json")

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
                      ("/large", "/small"),                  # images.scrydex.com, 245 x 342
                      ("_in_1000x1000.jpg", "_in_400x400.jpg"))  # TCGplayer: inside 400 x 400, never enlarged


def thumbnail_url(url):
    """Smaller variant of a fallback URL for grids (the rule the app applies,
    docs/architecture/data-sources.md "Fallback images" in the Evodex app
    repository); unknown shapes are returned unchanged."""
    for large, small in THUMBNAIL_SUFFIXES:
        if url.endswith(large):
            return url[:-len(large)] + small
    return url


def allowed(url, tcgplayer=False):
    """True for an https URL on one of ``ALLOWED_HOSTS``; with ``tcgplayer`` (the switch is on) also for a
    TCGplayer product photo in exactly the ``TCGPLAYER_IMAGE`` shape (``tcgplayer_product``)."""
    u = urllib.parse.urlparse(url or "")
    if u.scheme == "https" and u.netloc in ALLOWED_HOSTS:
        return True
    return bool(tcgplayer) and tcgplayer_product(url) is not None


def tcgplayer_url(product_id):
    """The TCGplayer CDN photo of a product (``TCGPLAYER_IMAGE``), or None without a positive id."""
    ok = isinstance(product_id, int) and not isinstance(product_id, bool) and product_id > 0
    return TCGPLAYER_IMAGE % product_id if ok else None


def tcgplayer_product(url):
    """The product id of a URL in exactly the ``TCGPLAYER_IMAGE`` shape, else None (http, another host or
    path, user info, a port, a query, a fragment, a leading zero ...)."""
    m = _TCGPLAYER_IMAGE_RE.fullmatch(url) if isinstance(url, str) else None
    return int(m.group(1)) if m else None


def is_tcgplayer_host(url):
    """True for any URL on TCGplayer's image CDN, whatever its shape (what the switch governs)."""
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "") == TCGPLAYER_IMAGE_HOST
    except ValueError:
        return False


def load_config(path=CONFIG):
    """The TCGplayer switch from ``images.json``: True / False, or None when the file is missing (= off).
    Keys starting with "_" are comments; any other key, or a value that isn't true/false, is an error."""
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError("%s must hold a JSON object" % path)
    unknown = sorted(k for k in doc if not k.startswith("_") and k != "tcgplayerFallback")
    if unknown:
        raise ValueError("%s: unknown key(s) %s (expected \"tcgplayerFallback\")" % (path, ", ".join(unknown)))
    value = doc.get("tcgplayerFallback", False)
    if not isinstance(value, bool):
        raise ValueError("%s: tcgplayerFallback must be true or false, not %r" % (path, value))
    return value


def stock_etags(status_cache, names):
    """ETags of TCGplayer stock pictures: one file (``status_cache`` {url: [code, time, note?, etag?]})
    that products of differently named cards share (``names`` {productId: card name}, compared by
    ``name_key``). A URL whose recorded ETag is in this set counts as a placeholder. The same card's
    photo under several products (a kit's ten Lightning Energy) is not one."""
    keys = {}
    for url, entry in status_cache.items():
        pid = tcgplayer_product(url)
        if pid is not None and pid in names and len(entry) > 3 and entry[0] == 200 and entry[3]:
            keys.setdefault(entry[3], set()).add(name_key(names[pid]))
    return {etag for etag, ks in keys.items() if len(ks) > 1}


def tcgplayer_image_problems(cards, enabled):
    """Why the index.json ``cards`` can't be published with the TCGplayer switch ``enabled``, as messages
    (empty = fine). Every ``im`` on TCGplayer's CDN needs the switch on, a card without a TCGdex image
    (``"i": 0``), exactly the ``TCGPLAYER_IMAGE`` shape and the card's deep-link product (``p[0].id``)."""
    found = [c for c in cards if is_tcgplayer_host(c.get("im"))]
    if not found:
        return []

    def card_id(c):
        return "%s-%s" % (c.get("s"), c.get("l"))

    def listing(cs):
        return ", ".join(card_id(c) for c in cs[:5]) + (" ..." if len(cs) > 5 else "")

    out = []
    if not enabled:
        out.append("%d card image(s) (im) on %s while the TCGplayer image switch is off (images.json "
                   "tcgplayerFallback): %s" % (len(found), TCGPLAYER_IMAGE_HOST, listing(found)))
    with_i = [c for c in found if c.get("i") != 0]
    if with_i:
        out.append("%d TCGplayer image(s) (im) on cards that have a TCGdex image (\"i\" is not 0): %s"
                   % (len(with_i), listing(with_i)))
    shape = [c for c in found if tcgplayer_product(c["im"]) is None]
    if shape:
        out.append("%d TCGplayer image URL(s) (im) not of the form %s, e.g. %r"
                   % (len(shape), TCGPLAYER_IMAGE % 0, shape[0]["im"]))
    product = [c for c in found if tcgplayer_product(c["im"]) is not None
               and tcgplayer_product(c["im"]) != ((c.get("p") or [{}])[0] or {}).get("id")]
    if product:
        out.append("%d TCGplayer image(s) (im) not of the card's first product (p[0].id): %s"
                   % (len(product), listing(product)))
    return out


def allowed_tcgdex(url):
    """True for an https TCGdex image base (a custom ``"i"`` in index.json)."""
    u = urllib.parse.urlparse(url or "")
    return u.scheme == "https" and u.netloc == TCGDEX_IMAGE_HOST


def choose(candidates, status, tcgplayer=False):
    """candidates: [(source, url | None)] in preference order; status(url) ->
    HTTP status, or None when it was never checked. Returns (source, url) of
    the first allowed URL (``allowed``, TCGplayer only with ``tcgplayer``) that
    is 200, or unchecked when its source is not in ``VERIFY_SOURCES``; else
    (None, None)."""
    for source, url in candidates:
        if not url or not allowed(url, tcgplayer):
            continue
        code = status(url)
        if code == 200 or (code is None and source not in VERIFY_SOURCES):
            return source, url
    return None, None
