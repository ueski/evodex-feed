"""Fallback images for cards that TCGdex has no scan for (pure functions).

TCGdex has no image for about 7.6 % of the catalog (all Trainer Gallery,
Galarian Gallery and Shiny Vault cards, MEP promos, trainer kits ...). For
those cards only, the pipeline looks for another absolute image URL:

  pokemontcg.io  ``images.large`` (``https://images.pokemontcg.io/<set>/<n>_hires.png``,
     734 x 1024 PNG; the newest sets use ``https://images.scrydex.com/pokemon/<id>/large``,
     654 x 914 PNG, because pokemontcg.io is run by Scrydex); the card is found
     by set mapping + number + name.

Until 2026-10-29 TCGplayer's own product photos (tcgplayer-cdn.tcgplayer.com)
were a second source. They were dropped for IP reasons: TCGplayer's terms bar
using its content elsewhere without written permission, so TCGplayer is
credited for prices only. Only ``ALLOWED_HOSTS`` may appear in ``im``
(validate_feed.py rejects a feed with any other host).

Every chosen URL is HEAD-checked once (run.py, cached in
``cache/images/status.json``); a URL that does not answer 200 is skipped and
the next source is tried. The result is the ``im`` field of index.json.

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
SOURCES = ("ptcgio",)
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
    data-sources.md in the Evodex app repository "Fallback images"); unknown shapes are
    returned unchanged."""
    for large, small in THUMBNAIL_SUFFIXES:
        if url.endswith(large):
            return url[:-len(large)] + small
    return url


def allowed(url):
    """True for an https URL on one of ``ALLOWED_HOSTS``."""
    u = urllib.parse.urlparse(url or "")
    return u.scheme == "https" and u.netloc in ALLOWED_HOSTS


def choose(candidates, status):
    """candidates: [(source, url | None)] in preference order; status(url) ->
    HTTP status, or None when it was never checked. Returns (source, url) of
    the first allowed URL (``allowed``) that is 200 (or unchecked), else (None, None)."""
    for source, url in candidates:
        if not url or not allowed(url):
            continue
        code = status(url)
        if code is None or code == 200:
            return source, url
    return None, None
