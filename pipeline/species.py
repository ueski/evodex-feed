"""Species list (National Dex) and card -> species mapping (pure functions).

species.json (bundled, small, see the app repository's docs/architecture/data-sources.md "Species / Dex")
    {"v": 1,
     "generations": [{"g": 1, "n": "I", "region": "Kanto", "from": 1, "to": 151}, ...],
     "types": ["Bug", "Dark", ...],                  # game types, string table
     "species": [{"id": 1, "n": "Bulbasaur", "g": 1, "t": [typeIdx, typeIdx?],
                  "rc": "<cardId>"?}, ...]}           # sorted by id
Source: PokéAPI's static CSV export (English names, generation, the default
form's types). "rc" is the representative card (``representative_cards``).

Card -> species ("dx" on Pokémon cards in index.json), first rule that applies:
  1. overrides.json "dex" pin            {"<cardId>": [ids]}
  2. name correction                     the name rule finds species and none of
                                         them is in TCGdex's dexId: TCGdex is wrong
                                         ("Chimchar" listed as Monferno 391)
  3. TCGdex ``dexId``                    every Pokémon card had one on 2026-10-23;
                                         it also knows species a name cannot show
                                         ("Buried Fossil", "Terapagos & Friends")
  4. name rule (``SpeciesIndex.find``)   gaps: species names found in the card name
Trainer and Energy cards get no "dx".

Name rule: the card name is folded with normalize.name_key (accents, ♀/♂,
"&", possessives, brackets and δ) and scanned left to right for the longest
species name at each position; every species found is kept in order. Prefixes
and suffixes that are not species names ("Dark", "Team Rocket's", "Radiant",
"Alolan", "Mega"/"M", "ex", "GX", "VMAX", "LV.X", "BREAK", "LEGEND", "◇", "☆",
"X"/"Y" ...) are simply skipped, so "Mega Charizard X ex" -> [6] and
"Charizard & Braixen-GX" -> [6, 654].
"""

import re
from collections import defaultdict

from normalize import name_key

# generation: (number, roman, main region, first dex id, last dex id)
GENERATIONS = [
    (1, "I", "Kanto", 1, 151),
    (2, "II", "Johto", 152, 251),
    (3, "III", "Hoenn", 252, 386),
    (4, "IV", "Sinnoh", 387, 493),
    (5, "V", "Unova", 494, 649),
    (6, "VI", "Kalos", 650, 721),
    (7, "VII", "Alola", 722, 809),
    (8, "VIII", "Galar", 810, 905),
    (9, "IX", "Paldea", 906, 1025),
]

ENGLISH = "9"   # PokéAPI local_language_id


def generation_for_dex(dex_id):
    """Generation number (1..9) of a National Dex id, or None outside 1..1025.
    Victini (494) opens Gen V, Meltan/Melmetal (808/809) close Gen VII and
    the Hisuian species (899-905) close Gen VIII."""
    for g, _, _, first, last in GENERATIONS:
        if first <= dex_id <= last:
            return g
    return None


def type_label(identifier):
    return identifier[:1].upper() + identifier[1:]


def build_species(tables):
    """tables: PokéAPI CSV rows by file name (sources.PokeAPI.tables()).
    Returns ([{id, name, gen, types}], problems). Types are the default
    form's current types (slot order). problems lists rows where PokéAPI's
    generation disagrees with GENERATIONS or a name/type is missing."""
    names = {r["pokemon_species_id"]: r["name"] for r in tables["pokemon_species_names"]
             if r["local_language_id"] == ENGLISH}
    default_form = {r["species_id"]: r["id"] for r in tables["pokemon"] if r["is_default"] == "1"}
    type_name = {r["id"]: type_label(r["identifier"]) for r in tables["types"]}
    form_types = defaultdict(list)
    for r in sorted(tables["pokemon_types"], key=lambda r: int(r["slot"])):
        form_types[r["pokemon_id"]].append(type_name[r["type_id"]])
    out, problems = [], []
    for r in sorted(tables["pokemon_species"], key=lambda r: int(r["id"])):
        sid = int(r["id"])
        rec = {"id": sid, "name": names.get(r["id"]), "gen": int(r["generation_id"]),
               "types": form_types.get(default_form.get(r["id"]), [])}
        if rec["gen"] != generation_for_dex(sid):
            problems.append("%d: PokéAPI generation %d, expected %s" % (sid, rec["gen"], generation_for_dex(sid)))
        if not rec["name"] or not rec["types"]:
            problems.append("%d: missing name or types" % sid)
        out.append(rec)
    return out, problems


class SpeciesIndex:
    """Finds species names inside card names (see module docstring)."""

    def __init__(self, species):
        self.max_id = max((s["id"] for s in species), default=0)
        by_first = defaultdict(list)
        for s in species:
            toks = tuple(name_key(s["name"]).split())
            if toks:
                by_first[toks[0]].append((toks, s["id"]))
        for v in by_first.values():
            v.sort(key=lambda ts: -len(ts[0]))        # longest name first: "porygon z" before "porygon"
        self.by_first = dict(by_first)

    def find(self, card_name):
        toks = name_key(card_name).split()
        out, i = [], 0
        while i < len(toks):
            for seq, sid in self.by_first.get(toks[i], ()):
                if tuple(toks[i:i + len(seq)]) == seq:
                    if sid not in out:
                        out.append(sid)
                    i += len(seq)
                    break
            else:
                i += 1
        return out


def card_species(card_id, name, dex_ids, index, overrides=None):
    """(ids, method) for one Pokémon card; method is "override",
    "name-corrected", "tcgdex", "name" or "none" (see module docstring).
    dex_ids is TCGdex's dexId (may be None); ids outside 1..index.max_id and
    duplicates are dropped."""
    if overrides and card_id in overrides:
        return list(overrides[card_id]), "override"
    valid = []
    for x in dex_ids or []:
        if isinstance(x, int) and 1 <= x <= index.max_id and x not in valid:
            valid.append(x)
    found = index.find(name)
    if valid and found and not set(found) & set(valid):
        return found, "name-corrected"
    if valid:
        return valid, "tcgdex"
    if found:
        return found, "name"
    return [], "none"


# ------------------------------------------------------------ representative card

# Rarities whose cards use the standard frame with a full-width art window.
REGULAR_RARITIES = {"Common", "Uncommon", "Rare", "Holo Rare", "Rare Holo", "Promo", "None"}
_DIGITS_RE = re.compile(r"^0*(\d+)$")


def _main_number(local_id):
    m = _DIGITS_RE.match(local_id or "")
    return int(m.group(1)) if m else None


def representative_key(card, set_meta, species_name):
    """Sort key (higher is better) for choosing a species' representative
    card. Criteria in order:
      1. has a TCGdex image (the crop source; fallback "im" art ranks lower)
      2. regular rarity (REGULAR_RARITIES: standard frame, not full art)
      3. numbered inside the set's official count (excludes secret rares and
         TG/GG/SV/CC sub-numbering)
      4. plain name: the card name is exactly the species name (not "Dark",
         "Alolan", "ex", "V" ...)
      5. newest set release date, then lowest collector number, then id."""
    num = _main_number(card["localId"])
    official = set_meta.get("official") or 0
    in_main = num is not None and (not official or num <= official)
    plain = name_key(card["name"]) == name_key(species_name)
    return (bool(card.get("image")), card.get("rarity") in REGULAR_RARITIES, in_main, plain,
            set_meta.get("releaseDate") or "", -(num if num is not None else 10 ** 6), card["id"])


def representative_cards(cards, set_meta, species):
    """cards: [{id, setId, localId, name, rarity, image, dx}] (Pokémon only);
    set_meta: {setId: {releaseDate, official}}. Only single-species cards
    (dx == [id]) are candidates. Returns {speciesId: cardId}."""
    names = {s["id"]: s["name"] for s in species}
    best = {}
    for c in cards:
        dx = c.get("dx") or []
        if len(dx) != 1 or dx[0] not in names:
            continue
        sid = dx[0]
        key = representative_key(c, set_meta.get(c["setId"], {}), names[sid])
        if sid not in best or key > best[sid][0]:
            best[sid] = (key, c["id"])
    return {sid: cid for sid, (key, cid) in best.items()}


def species_file(species, rc=None):
    """The compact species.json document."""
    types = sorted({t for s in species for t in s["types"]})
    t_idx = {t: i for i, t in enumerate(types)}
    rows = []
    for s in species:
        e = {"id": s["id"], "n": s["name"], "g": s["gen"], "t": [t_idx[t] for t in s["types"]]}
        if rc and s["id"] in rc:
            e["rc"] = rc[s["id"]]
        rows.append(e)
    return {"v": 1,
            "generations": [{"g": g, "n": roman, "region": region, "from": a, "to": b}
                            for g, roman, region, a, b in GENERATIONS],
            "types": types, "species": rows}
