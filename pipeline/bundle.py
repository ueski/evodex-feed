"""Encoders for what the iOS app bundles / downloads, plus size measurement.

index.json (bundled, ~read-only catalog)
    {
      "v": 1, "imageBase": "https://assets.tcgdex.net/en",
      "rarities": [...], "types": [...], "illustrators": [...],  # string tables
      "tcgGroups": [{"id": groupId, "name", "abbr"?}],  # every TCGplayer group referenced
                                                         # by a set or a product, sorted by id
      "series": [{"id", "name"}],
      "sets":  [{"id", "name", "series", "releaseDate", "official", "total",
                 "abbr"? (TCGdex official abbreviation, e.g. "MEW"),
                 "logo"?, "symbol"?, "groups": [groupId...]}],
      "cards": [{"s": setId, "l": localId, "n": name, "r": rarityIdx?,
                 "t": [typeIdx]?, "c": category letter (P/T/E),
                 "h": hp?, "a": illustratorIdx?,
                 "dx": [nationalDexId, ...]? (Pokémon cards only; see species.py),
                 "i": 0 (no image) | "<full url>" (non-standard path)?,
                 "im": "<absolute image URL>"? (only with "i": 0),
                 "no": "025/165"? (TCGplayer printed number),
                 "p": [{"id": productId, "st": [subTypeIdx...]?, "g": groupLabel?,
                        "q": qualifier?, "gid": groupId?}]?}],
      "subTypes": ["Normal", "Holofoil", ...]
    }
    card id = s + "-" + l;  image = imageBase/<series>/<set>/<localId> unless "i".
    "dx" lists the National Dex species on a Pokémon card (several for tag
    teams and LEGEND halves); species names, generations and types are in
    species.json. Trainer and Energy cards have no "dx".
    "im" is a fallback for cards TCGdex has no image for ("i": 0): one complete
    image file URL (images.pokemontcg.io "_hires.png" or images.scrydex.com
    ".../large"), NOT a TCGdex base to append "/low.webp" to. Decoders
    that do not know "im" keep showing the placeholder. See images.py and
    docs/architecture/data-sources.md "Fallback images" (Evodex app repository).
    "gid" is present only when the product's TCGplayer group differs from its
    set's first group (sets[].groups[0]); otherwise that first group is
    implied. tcgGroups gives the group name / abbreviation (TCGplayer CSV
    "Set" / "Set Code" columns).
    p[0].id is the deep-link product. Each (id, subType) pair is one variant
    whose price is prices.json p[id][subType]; display label =
    [g, subType].join(" ") + (q ? " (q)" : ""). "st" is absent when TCGplayer
    has no price row for the product (label "Unpriced").
    Optional keys are omitted when empty, which is why Swift should decode
    them as optionals.

prices.json (downloaded daily, never bundled except as a first-run seed)
    {"v": 1, "tcgcsvBuild": ISO, "subTypes": [...],
     "p": {"<productId>": [[subTypeIdx, market, low, mid, high, directLow], ...]}}
    prices are integer cents (null when TCGplayer has none).
"""

import gzip
import json
import lzma

CATEGORY_CODES = {"Pokemon": "P", "Trainer": "T", "Energy": "E"}


def dumps_compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")


def sizes(blob):
    return {
        "raw": len(blob),
        "gzip": len(gzip.compress(blob, 9)),
        "xz": len(lzma.compress(blob, preset=9 | lzma.PRESET_EXTREME)),
    }


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def cents(x):
    return None if x is None else int(round(x * 100))


def standard_image(image_base, series_id, set_id, local_id):
    return "%s/%s/%s/%s" % (image_base, series_id, set_id, local_id)


def compact_products(variants, s_idx, first_group=None):
    """Collapse a card's variant list into one entry per product.
    first_group: the set's first TCGplayer group; products in another group
    carry "gid"."""
    out, by_pid = [], {}
    for v in variants:
        e = by_pid.get(v["productId"])
        if e is None:
            e = {"id": v["productId"]}
            if v.get("groupLabel"):
                e["g"] = v["groupLabel"]
            if v.get("qualifier"):
                e["q"] = v["qualifier"]
            if v.get("groupId") is not None and v["groupId"] != first_group:
                e["gid"] = v["groupId"]
            by_pid[v["productId"]] = e
            out.append(e)
        if v["subType"] is not None:
            if v["subType"] not in s_idx:
                s_idx[v["subType"]] = len(s_idx)
            e.setdefault("st", []).append(s_idx[v["subType"]])
    return out


def compact_index(series, sets_out, cards_out, subtypes,
                  image_base="https://assets.tcgdex.net/en", groups=None):
    """groups: TCGCSV group objects ({groupId, name, abbreviation}) for tcgGroups."""
    rarities = sorted({c["rarity"] for c in cards_out if c.get("rarity")})
    types = sorted({t for c in cards_out for t in (c.get("types") or [])})
    illustrators = sorted({c["illustrator"] for c in cards_out if c.get("illustrator")})
    r_idx = {r: i for i, r in enumerate(rarities)}
    t_idx = {t: i for i, t in enumerate(types)}
    a_idx = {a: i for i, a in enumerate(illustrators)}
    set_series = {s["id"]: s["series"] for s in sets_out}
    first_group = {s["id"]: (s.get("groups") or [None])[0] for s in sets_out}
    s_idx = {st: i for i, st in enumerate(subtypes)}

    cards = []
    for c in cards_out:
        e = {"s": c["setId"], "l": c["localId"], "n": c["name"]}
        if c.get("rarity"):
            e["r"] = r_idx[c["rarity"]]
        if c.get("types"):
            e["t"] = [t_idx[t] for t in c["types"]]
        if c.get("category") in CATEGORY_CODES:
            e["c"] = CATEGORY_CODES[c["category"]]
        if isinstance(c.get("hp"), int) and c["hp"] > 0:
            e["h"] = c["hp"]
        if c.get("illustrator"):
            e["a"] = a_idx[c["illustrator"]]
        if c.get("dx"):
            e["dx"] = list(c["dx"])
        img = c.get("image")
        if not img:
            e["i"] = 0
            fb = c.get("imageFallback")
            if fb and fb.get("url"):
                e["im"] = fb["url"]
        elif img != standard_image(image_base, set_series[c["setId"]], c["setId"], c["localId"]):
            e["i"] = img
        tp = c.get("tcgplayer")
        if tp:
            if tp.get("number"):
                e["no"] = tp["number"]
            e["p"] = compact_products(tp["variants"], s_idx, first_group.get(c["setId"]))
        cards.append(e)

    sets = []
    for s in sets_out:
        d = {"id": s["id"], "name": s["name"], "series": s["series"],
             "releaseDate": s.get("releaseDate"), "official": s.get("official"),
             "total": s.get("total"), "groups": s.get("groups", [])}
        if s.get("abbreviation"):
            d["abbr"] = s["abbreviation"]
        for k in ("logo", "symbol"):
            if s.get(k):
                d[k] = s[k]
        sets.append(d)
    referenced = {g for s in sets for g in s["groups"]}
    referenced |= {v["groupId"] for c in cards_out if c.get("tcgplayer")
                   for v in c["tcgplayer"]["variants"] if v.get("groupId") is not None}
    by_id = {g["groupId"]: g for g in (groups or [])}
    tcg_groups = []
    for gid in sorted(referenced):
        g = by_id.get(gid)
        if g is None:
            continue
        entry = {"id": gid, "name": g["name"]}
        if g.get("abbreviation"):
            entry["abbr"] = g["abbreviation"]
        tcg_groups.append(entry)
    # no timestamps inside: the file's sha256 (manifest.json) only changes
    # when the catalog or the product mapping really changes
    return {"v": 1, "imageBase": image_base, "rarities": rarities, "types": types,
            "illustrators": illustrators,
            "tcgGroups": tcg_groups,
            "subTypes": [st for st, _ in sorted(s_idx.items(), key=lambda kv: kv[1])],
            "series": [{"id": x["id"], "name": x["name"]} for x in series],
            "sets": sets, "cards": cards}


def verbose_index(cards_out):
    """Same information with readable keys, for comparing encodings only."""
    out = []
    for c in cards_out:
        tp = c.get("tcgplayer") or {}
        out.append({"id": c["id"], "setId": c["setId"], "localId": c["localId"],
                    "name": c["name"], "number": tp.get("number"), "rarity": c.get("rarity"),
                    "types": c.get("types"), "image": c.get("image"),
                    "productIds": {v["label"]: v["productId"] for v in tp.get("variants", [])}})
    return out


def price_snapshot(prices_raw, product_ids, tcgcsv_build, subtypes):
    s_idx = {s: i for i, s in enumerate(subtypes)}
    p = {}
    for rows in prices_raw.values():
        for r in rows:
            if r["productId"] not in product_ids:
                continue
            sub = r["subTypeName"]
            if sub not in s_idx:
                s_idx[sub] = len(subtypes)
                subtypes.append(sub)
            p.setdefault(str(r["productId"]), []).append([
                s_idx[sub], cents(r.get("marketPrice")), cents(r.get("lowPrice")),
                cents(r.get("midPrice")), cents(r.get("highPrice")),
                cents(r.get("directLowPrice"))])
    return {"v": 1, "tcgcsvBuild": tcgcsv_build, "subTypes": subtypes, "p": p}


def market_only(snapshot):
    """Smallest useful snapshot: market price only (cents)."""
    return {"v": 1, "tcgcsvBuild": snapshot["tcgcsvBuild"], "subTypes": snapshot["subTypes"],
            "p": {pid: [[r[0], r[1]] for r in rows] for pid, rows in snapshot["p"].items()}}
