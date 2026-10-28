"""Card <-> TCGplayer product matching inside one mapped set.

Pseudo-code (mirrored in data-sources.md in the Evodex app repository so the app can
port it to Swift):

    pool  = card products of the groups mapped to the set
            (skip sealed + code cards; apply the set's include/exclude filters)
    byKey = pool grouped by number_key(product.Number)

    # pass 1: collector number, name decides between candidates
    for each key, products in byKey:
        cards_k = cards whose number_key(localId) == key
        for each product p in products:
            (sim, card) = best name_similarity(card.name, p.name) over cards_k
            if sim >= 0.5:                                      assign  "number+name"
            elif sim > 0 and |cards_k| == 1 and |products| == 1: assign "number"
            # else ambiguous -> leave unassigned

    foreign = (cards matched in pass 1) < 50% of the set's cards
              # numbering scheme differs wholesale (Classic Collection "CC001"
              # vs TCGplayer "2/102", My First Battle without numbers)

    # pass 2: name fallback for cards still without a product
    for each card without products, in localId order:
        cands = unassigned products with name_similarity >= 0.8, best score only
        unless foreign: drop cands whose number digits differ from the card's
        if |cands| > 1: keep the ones whose digits equal the card's digits
        if |cands| > 1: keep the ones without a bracket qualifier
        if |cands| == 1: assign                                         "name"

    # one primary product per (card, group): unqualified name first, then best
    # name score, then lowest productId. Other products of the same card are
    # alternate printings (stamps, Cosmos Holo, Poke Ball pattern ...).
"""

import re
from collections import defaultdict

from normalize import name_similarity, number_key, product_qualifier

NAME_OK = 0.5
NAME_FALLBACK = 0.8
FOREIGN_NUMBERING = 0.5


def extended(product):
    return {e["name"]: e["value"] for e in product.get("extendedData") or []}


def is_card_product(product):
    """Singles have a card Number, or at least a Rarity that is not 'Code Card'.
    Booster boxes, ETBs, tins, blisters, code cards ... are skipped."""
    ed = extended(product)
    if "Number" in ed:
        return True
    rarity = ed.get("Rarity")
    return rarity is not None and rarity != "Code Card"


def card_products(raw_products):
    out = []
    for p in raw_products:
        if not is_card_product(p):
            continue
        ed = extended(p)
        num = ed.get("Number")
        out.append({
            "productId": p["productId"],
            "groupId": p["groupId"],
            "name": p["name"],
            "number": num,
            "key": number_key(num) if num else "",
            "rarity": ed.get("Rarity"),
            "qualifier": product_qualifier(p["name"]),
        })
    return out


def filter_pool(products, include=None, exclude=None):
    """Per-set pool filters from overrides.json (substring match on the
    product name, case-insensitive). Used where several TCGdex sets share one
    TCGplayer group (trainer kits, Futsal inside 'Miscellaneous')."""
    out = []
    for p in products:
        n = p["name"].lower()
        if include and not any(s.lower() in n for s in include):
            continue
        if exclude and any(s.lower() in n for s in exclude):
            continue
        out.append(p)
    return out


def digits(key):
    m = re.search(r"\d+", key or "")
    return int(m.group(0)) if m else None


def match_set(cards, products, card_overrides=None, name_fallback=True):
    """cards: [{"id", "localId", "name"}]; products: card_products() of the
    set's pool. card_overrides: {cardId: [productId, ...]} applied first.
    name_fallback=False skips pass 2 (used for the shared fallback groups,
    where a loose name match would be a guess).

    Returns (matches, unassigned_products) where
      matches[cardId] = {"method": str, "products": [product + nameScore + primary]}
    """
    card_overrides = card_overrides or {}
    by_key = defaultdict(list)
    for p in products:
        by_key[p["key"]].append(p)
    cards_by_key = defaultdict(list)
    for c in cards:
        cards_by_key[number_key(c["localId"])].append(c)
    card_by_id = {c["id"]: c for c in cards}
    prod_by_id = {p["productId"]: p for p in products}

    assigned = {}
    result = {}

    def assign(card, p, sim, method):
        assigned[p["productId"]] = card["id"]
        r = result.setdefault(card["id"], {"method": method, "products": []})
        r["products"].append(dict(p, nameScore=round(sim, 2)))

    # pass 0: hand overrides
    for cid, pids in card_overrides.items():
        if cid in card_by_id:
            for pid in pids:
                if pid in prod_by_id:
                    assign(card_by_id[cid], prod_by_id[pid], 1.0, "override")

    # pass 1: number (+ name as tie-break)
    for key, prods in by_key.items():
        cands = [c for c in cards_by_key.get(key, []) if c["id"] not in card_overrides]
        if not key or not cands:
            continue
        for p in prods:
            if p["productId"] in assigned:
                continue
            sim, card = max(((name_similarity(c["name"], p["name"]), c) for c in cands),
                            key=lambda t: t[0])
            if sim >= NAME_OK:
                assign(card, p, sim, "number+name")
            elif sim > 0 and len(cands) == 1 and len(prods) == 1:
                assign(card, p, sim, "number")

    foreign = len(result) < FOREIGN_NUMBERING * len(cards)

    # pass 2: name fallback
    for c in cards if name_fallback else []:
        if c["id"] in result:
            continue
        cdig = digits(number_key(c["localId"]))
        scored = []
        for p in products:
            if p["productId"] in assigned:
                continue
            sim = name_similarity(c["name"], p["name"])
            if sim < NAME_FALLBACK:
                continue
            if not foreign and p["key"] and cdig is not None and digits(p["key"]) != cdig:
                continue
            scored.append((sim, p))
        if not scored:
            continue
        best = max(s for s, _ in scored)
        cands = [p for s, p in scored if s == best]
        if len(cands) > 1:
            same = [p for p in cands if cdig is not None and digits(p["key"]) == cdig]
            cands = same or cands
        if len(cands) > 1:
            plain = [p for p in cands if not p["qualifier"]]
            cands = plain or cands
        if len(cands) == 1:
            assign(c, cands[0], best, "name")

    for r in result.values():
        choose_primary(r["products"])
    unassigned = [p for p in products if p["productId"] not in assigned]
    return result, unassigned


def choose_primary(prods):
    """Mark one primary product per group: unqualified name first, then best
    name score, then lowest productId (the original listing)."""
    by_group = defaultdict(list)
    for p in prods:
        by_group[p["groupId"]].append(p)
    for plist in by_group.values():
        plist.sort(key=lambda p: (p["qualifier"] != "", -p["nameScore"], p["productId"]))
        for i, p in enumerate(plist):
            p["primary"] = i == 0
