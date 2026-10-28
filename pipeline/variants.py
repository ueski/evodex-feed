"""TCGplayer printing (subTypeName) handling.

TCGCSV /prices returns one row per (productId, subTypeName). Pokemon uses
exactly these subTypeNames (observed 2026-10-21 across all 220 groups):

    Normal, Holofoil, Reverse Holofoil,
    1st Edition, 1st Edition Holofoil, Unlimited, Unlimited Holofoil

A card's variant list is built from every product matched to it:
  * primary product of the main group   -> label = subTypeName
  * primary product of a labelled group -> label = "<group label> <subTypeName>"
                                           e.g. "Shadowless 1st Edition Holofoil"
  * alternate product (stamp, pattern)  -> label = "<subTypeName> (<qualifier>)"
                                           e.g. "Reverse Holofoil (Poke Ball Pattern)"
  * product without any price row       -> one entry with subType = None
"""

SUBTYPES = ["Normal", "Holofoil", "Reverse Holofoil", "1st Edition", "1st Edition Holofoil",
            "Unlimited", "Unlimited Holofoil"]

# TCGdex `variants` flags -> TCGplayer subTypeNames that represent them.
TCGDEX_TO_TCGPLAYER = {
    "normal": ["Normal", "Unlimited"],
    "holo": ["Holofoil", "Unlimited Holofoil"],
    "reverse": ["Reverse Holofoil"],
    "firstEdition": ["1st Edition", "1st Edition Holofoil"],
}

PRICE_FIELDS = ("marketPrice", "lowPrice", "midPrice", "highPrice", "directLowPrice")


def variant_label(sub_type, group_label="", qualifier="", primary=True):
    base = sub_type or "Unpriced"
    if group_label:
        base = "%s %s" % (group_label, base)
    if qualifier and not primary:
        base = "%s (%s)" % (base, qualifier)
    elif qualifier and sub_type is None:
        base = "%s (%s)" % (base, qualifier)
    return base


def build_variants(products, prices_by_pid, group_labels, group_order=()):
    """products: matched products of one card (with 'primary', 'qualifier',
    'groupId'); prices_by_pid: {productId: [price rows]};
    group_labels: {groupId: label}. Returns a list of variant dicts ordered
    primary-first (main group first), then by SUBTYPES order."""
    out = []
    order = {s: i for i, s in enumerate(SUBTYPES)}
    rank = {g: i for i, g in enumerate(group_order)}
    for p in sorted(products, key=lambda p: (not p.get("primary"), rank.get(p["groupId"], 99),
                                             p["productId"])):
        rows = prices_by_pid.get(p["productId"]) or [None]
        rows = sorted(rows, key=lambda r: order.get(r["subTypeName"], 99) if r else 0)
        for r in rows:
            sub = r["subTypeName"] if r else None
            glabel = group_labels.get(p["groupId"], "")
            v = {
                "label": variant_label(sub, glabel, p.get("qualifier", ""), p.get("primary", True)),
                "subType": sub,
                "productId": p["productId"],
                "groupId": p.get("groupId"),
                "primary": bool(p.get("primary")),
                "groupLabel": glabel,
                "qualifier": "" if p.get("primary") else p.get("qualifier", ""),
            }
            for f in PRICE_FIELDS:
                v[f] = r.get(f) if r else None
            out.append(v)
    # disambiguate accidental duplicate labels (two unlabelled primaries in
    # two groups with the same subtype) by appending the productId
    seen = {}
    for v in out:
        seen[v["label"]] = seen.get(v["label"], 0) + 1
    for v in out:
        if seen[v["label"]] > 1:
            v["label"] = "%s #%d" % (v["label"], v["productId"])
    return out
