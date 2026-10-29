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

  3. Curated (v0.3.6, ``same-art-curated.json``, ``load_curated``): reviewed pairs for cards the rules above
     can't match, mostly trainer-kit reprints for which TCGdex lists no illustrator. Each pair was compared by
     eye with a public scan of the card itself (the note says which and when); only the illustrator / HP /
     attack-shape match is waived. Name, category, no chains and the era rule for Trainer and Energy cards still
     apply, unless an entry says ``"crossEra": true`` (with its own by-eye proof). A curated entry comes first.

No chains: a source has its own TCGdex image (no ``"i": 0``, so no ``im`` and no ``ia``). A TCGplayer
``im`` wins over an alias (it is a photo of this very printing); the alias is only for cards with neither.
``alias_problems`` is the check publish_feed.py and validate_feed.py run on the published index.json.
"""

import json
import os
import re

ERA_SERIES = (("tk-xy-", "xy"), ("tk-sm-", "sm"), ("tk-hs-", "hgss"), ("tk-bw-", "bw"), ("tk-dp-", "dp"))
ENERGY_ERAS = ("xy", "sm")      # eras whose kits print the main sets' basic Energy (see rule 1)
BASIC_ENERGY = re.compile(r"(Grass|Fire|Water|Lightning|Psychic|Fighting|Darkness|Metal|Fairy) Energy")
_NUMBER_RE = re.compile(r"(\d+)")
CURATED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "same-art-curated.json")
_CURATED_KEYS = {"source", "note", "crossEra"}


def load_curated(path=CURATED):
    """{cardId: {"source", "note", "crossEra"?}} from same-art-curated.json ({} without the file). Every entry needs a
    source card id and a note; anything else is an error, so a typo stops the run instead of shipping."""
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    entries = doc.get("cards") if isinstance(doc, dict) else None
    if not isinstance(entries, dict):
        raise ValueError("%s: expected {\"cards\": {cardId: {source, note}}}" % path)
    for cid, e in entries.items():
        if not isinstance(e, dict) or set(e) - _CURATED_KEYS or not isinstance(e.get("source"), str) \
                or not e["source"] or not isinstance(e.get("note"), str) or not e["note"].strip() \
                or not isinstance(e.get("crossEra", False), bool) or e["source"] == cid:
            raise ValueError("%s: bad entry for %s: %r (needs source and note; crossEra true/false)" % (path, cid, e))
    return entries


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


def curated_problem(card, source, set_meta, entry):
    """Why a curated pair can't be used, or None: the source must exist with its own TCGdex image and the same name
    and category; a Trainer or Energy source must be of the card's era unless the entry says crossEra."""
    if source is None:
        return "source %s is not in the catalog" % entry["source"]
    if not source.get("ownImage"):
        return "source %s has no TCGdex image of its own" % entry["source"]
    if source.get("name") != card.get("name") or source.get("category") != card.get("category"):
        return "source %s is %r (%s), not %r (%s)" % (entry["source"], source.get("name"), source.get("category"),
                                                    card.get("name"), card.get("category"))
    era = era_series(card["setId"], (set_meta.get(card["setId"]) or {}).get("series"))
    if card.get("category") != "Pokemon" and not entry.get("crossEra") \
            and (set_meta.get(source["setId"]) or {}).get("series") != era:
        return "source %s is of another era (needs crossEra with its own proof)" % entry["source"]
    return None


def aliases(cards, set_meta, curated=None, problems=None):
    """cards: [{id, setId, localId, name, category, hp, illustrator, rarity, ownImage (its own TCGdex image),
    needsImage (no image from any source), attackShape}] (the whole catalog); set_meta: {setId: {series,
    releaseDate}}; curated: ``load_curated()``. Returns {cardId: (sourceId, "curated" | "energy" | "reprint")}
    for cards with ``needsImage``. A curated pair that fails ``curated_problem`` is not used; ``problems`` (a dict)
    collects why, by card id."""
    curated = curated or {}
    by_id = {c["id"]: c for c in cards}
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
        if c["id"] in curated:
            entry = curated[c["id"]]
            problem = curated_problem(c, by_id.get(entry["source"]), set_meta, entry)
            if problem is None:
                out[c["id"]] = (entry["source"], "curated")
                continue
            if problems is not None:
                problems[c["id"]] = problem
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


def alias_problems(index_obj, curated=None):
    """Why index.json's ``ia`` fields can't be published, as messages (empty = fine): every alias is on a card
    with ``"i": 0`` and no ``im``; its source exists, has its own TCGdex image (no ``"i": 0``, so no chain),
    and has the same name and category; a Trainer or Energy alias stays within its era (``era_series``) unless
    ``curated`` (``load_curated()``) has this very pair with ``"crossEra": true``."""
    curated = curated or {}
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
        waived = (curated.get(cid) or {}).get("crossEra") is True and curated[cid].get("source") == c["ia"]
        if c.get("c") != "P" and not waived \
                and series.get(source.get("s")) != era_series(c.get("s"), series.get(c.get("s"))):
            bad["Trainer or Energy from another era"].append(cid)
    return ["%d image alias(es) (ia) %s: %s" % (len(ids), why, ", ".join(ids[:5]) + (" ..." if len(ids) > 5 else ""))
            for why, ids in bad.items() if ids]
