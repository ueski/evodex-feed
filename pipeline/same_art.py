"""Same artwork from another printing (index.json "ia", image alias) for cards no image source covers (pure).

The last image step (v0.3.5), after TCGdex, a TCGdex twin, pokemontcg.io / Scrydex and TCGplayer
(images.py): a card that still has no image gets a reference to another card of the same catalog whose
TCGdex scan shows the same artwork. index.json carries the source card's id, never a URL, so the alias is
TCGdex-only and ships in the app bundle; the app draws the source's TCGdex image and labels it
"Same artwork · <set> printing" on the card screen. First rule that applies:

  1. Basic Energy of a trainer kit (``c`` = E, name "<Type> Energy"): the TCGdex-imaged basic Energy of the
     same type from the kit's era (``ERA_SERIES``), for the eras in ``ENERGY_ERAS`` only. A kit prints its
     era's regular Energy, so the source is the era's first main set's Common basic Energy of that type
     (earliest release, then set id, then number): xy1 132-140, sm1 164-172. Checked by eye 2026-10-29:
     tk-xy-p's Lightning Energy (TCGplayer photo) is xy1 135. Not HGSS: hgss1's basic Energy shows a
     Lugia / Ho-Oh silhouette, the HS kits' Energy is plain (tk-hs-g Water Energy vs hgss1 117).
  2. Exact reprint: a card with its own TCGdex image and the same name, HP, category and illustrator
     (never when the illustrator is unknown), and the same attack shape (number of attacks and their
     damage, number of abilities) as a guard against two different drawings by one artist. A Trainer or
     Energy has no attacks to compare, and one artist redraws them across eras (HS kit Bill vs Base Set
     Bill, BW kit Energy Search vs Majestic Dawn's, two Paradise Resort promos), so their source must be
     of the card's own era series. Preferred: the same series (a kit's era series), then the earliest
     release, then the card id.

No chains: a source has its own TCGdex image (no ``"i": 0``, so no ``im`` and no ``ia``). A TCGplayer
``im`` wins over an alias (it is a photo of this very printing); the alias is only for cards with neither.
``alias_problems`` is the check publish_feed.py and validate_feed.py run on the published index.json.
"""

import re

ERA_SERIES = (("tk-xy-", "xy"), ("tk-sm-", "sm"), ("tk-hs-", "hgss"), ("tk-bw-", "bw"), ("tk-dp-", "dp"))
ENERGY_ERAS = ("xy", "sm")      # eras whose kits print the main sets' basic Energy (see rule 1)
BASIC_ENERGY = re.compile(r"(Grass|Fire|Water|Lightning|Psychic|Fighting|Darkness|Metal|Fairy) Energy")
_NUMBER_RE = re.compile(r"(\d+)")


def era_series(set_id, series):
    """The series whose artwork a set reprints: a trainer kit's era (``ERA_SERIES``), else its own series."""
    for prefix, era in ERA_SERIES:
        if (set_id or "").startswith(prefix):
            return era
    return series


def is_kit(set_id):
    return any((set_id or "").startswith(prefix) for prefix, _ in ERA_SERIES)


def _natural(local_id):
    return [int(t) if i % 2 else t for i, t in enumerate(_NUMBER_RE.split(local_id or ""))]


def energy_sources(cards, set_meta):
    """{(series, "<Type> Energy"): cardId}: per era series and type, the Common basic Energy with its own TCGdex
    image from the earliest-released set of that series (then set id, then number)."""
    eras = set(ENERGY_ERAS)
    best = {}
    for c in cards:
        meta = set_meta.get(c["setId"]) or {}
        series = meta.get("series")
        if (series not in eras or not c.get("ownImage") or c.get("category") != "Energy"
                or not BASIC_ENERGY.fullmatch(c.get("name") or "") or c.get("rarity") != "Common"):
            continue
        key = (meta.get("releaseDate") or "9999", c["setId"], _natural(c["localId"]))
        slot = (series, c["name"])
        if slot not in best or key < best[slot][0]:
            best[slot] = (key, c["id"])
    return {slot: cid for slot, (_, cid) in best.items()}


def aliases(cards, set_meta):
    """cards: [{id, setId, localId, name, category, hp, illustrator, rarity, ownImage (its own TCGdex image),
    needsImage (no image from any source), attackShape}] (the whole catalog); set_meta: {setId: {series,
    releaseDate}}. Returns {cardId: (sourceId, "energy" | "reprint")} for cards with ``needsImage``."""
    energy = energy_sources(cards, set_meta)
    by_key = {}
    for c in cards:
        if c.get("ownImage") and c.get("illustrator"):
            key = (c["name"], c.get("hp"), c.get("category"), c["illustrator"], c.get("attackShape"))
            by_key.setdefault(key, []).append(c)
    out = {}
    for c in cards:
        if not c.get("needsImage"):
            continue
        meta = set_meta.get(c["setId"]) or {}
        era = era_series(c["setId"], meta.get("series"))
        if c.get("category") == "Energy" and BASIC_ENERGY.fullmatch(c.get("name") or "") and is_kit(c["setId"]):
            source = energy.get((era, c["name"]))
            if source:
                out[c["id"]] = (source, "energy")
            continue
        if not c.get("illustrator"):
            continue
        candidates = by_key.get((c["name"], c.get("hp"), c.get("category"), c["illustrator"], c.get("attackShape")), [])
        if c.get("category") != "Pokemon":
            candidates = [s for s in candidates if (set_meta.get(s["setId"]) or {}).get("series") == era]
        if not candidates:
            continue

        def rank(s):
            sm = set_meta.get(s["setId"]) or {}
            return (sm.get("series") != era, sm.get("releaseDate") or "9999", s["id"])
        out[c["id"]] = (min(candidates, key=rank)["id"], "reprint")
    return out


def alias_problems(index_obj):
    """Why index.json's ``ia`` fields can't be published, as messages (empty = fine): every alias is on a card
    with ``"i": 0`` and no ``im``; its source exists, has its own TCGdex image (no ``"i": 0``, so no chain),
    and has the same name and category; a Trainer or Energy alias stays within its era (``era_series``)."""
    cards = index_obj.get("cards") or []
    series = {s.get("id"): s.get("series") for s in index_obj.get("sets") or []}
    by_id = {"%s-%s" % (c.get("s"), c.get("l")): c for c in cards}
    bad = {"not on an imageless card": [], "source missing": [], "source without its own TCGdex image": [],
           "another name or category": [], "Trainer or Energy from another era": []}
    for c in cards:
        if "ia" not in c:
            continue
        cid = "%s-%s" % (c.get("s"), c.get("l"))
        source = by_id.get(c["ia"]) if isinstance(c["ia"], str) else None
        if c.get("i") != 0 or c.get("im"):
            bad["not on an imageless card"].append(cid)
        if source is None:
            bad["source missing"].append(cid)
            continue
        if source.get("i") == 0 or "ia" in source or "im" in source:
            bad["source without its own TCGdex image"].append(cid)
        if source.get("n") != c.get("n") or source.get("c") != c.get("c"):
            bad["another name or category"].append(cid)
        if c.get("c") != "P" and series.get(source.get("s")) != era_series(c.get("s"), series.get(c.get("s"))):
            bad["Trainer or Energy from another era"].append(cid)
    return ["%d image alias(es) (ia) %s: %s" % (len(ids), why, ", ".join(ids[:5]) + (" ..." if len(ids) > 5 else ""))
            for why, ids in bad.items() if ids]
