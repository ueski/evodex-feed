"""TCGdex set  ->  TCGplayer (TCGCSV) group(s) mapping.

A TCGdex set maps to an ordered list of groups (usually one):
  * one-to-many: "base1" -> [Base Set, Base Set (Shadowless)] (1st Edition and
    shadowless printings live in the second group), "g1" -> [Generations,
    Generations: Radiant Collection]
  * many-to-one: "tk-ex-latia" and "tk-ex-latio" -> "EX Trainer Kit 1: Latias &
    Latios"; "exu" (Unown Collection) lives inside "EX Unseen Forces".
These shapes are only produced by ``overrides.json``; the automatic pass is a
strict one-to-one assignment so it can never silently merge two sets.

Automatic pass (per unmapped, physical TCGdex set S and unclaimed group G):

    name    = set_name_similarity(S.name, G.name)              0..1
    date    = 1 if |S.releaseDate - G.publishedOn| <= 7 days,
              0.5 if <= 60 days, else 0
    overlap = |numberKeys(S.cards) & numberKeys(G.cardProducts)| / |numberKeys(S.cards)|
    count   = min(|S|, |G|) / max(|S|, |G|)    (cards vs card-products)
    score   = 0.40*name + 0.15*date + 0.35*overlap + 0.10*count

Pairs are taken greedily by descending score; a pair is accepted when both
sides are still free and either
  (a) score >= 0.62 and name >= 0.5, or
  (b) overlap >= 0.9 and date == 1 and count >= 0.8   (same release week and
      the same numbering, e.g. "SVP Black Star Promos" vs "SV: Scarlet &
      Violet Promo Cards", "Sun & Moon" vs "SM Base Set").
Everything else is reported as unmatched for a human to add to overrides.json.
"""

import datetime

from normalize import number_key, set_name_similarity

W_NAME, W_DATE, W_OVERLAP, W_COUNT = 0.40, 0.15, 0.35, 0.10
MIN_SCORE = 0.62


def _date(s):
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def date_score(a, b):
    da, db = _date(a), _date(b)
    if not da or not db:
        return 0.0
    d = abs((da - db).days)
    if d <= 7:
        return 1.0
    if d <= 60:
        return 0.5
    return 0.0


def pair_features(set_name, set_date, set_keys, group_name, group_date, group_keys):
    name = set_name_similarity(set_name, group_name)
    date = date_score(set_date, group_date)
    overlap = len(set_keys & group_keys) / float(len(set_keys)) if set_keys else 0.0
    n1, n2 = len(set_keys), len(group_keys)
    count = min(n1, n2) / float(max(n1, n2)) if n1 and n2 else 0.0
    score = W_NAME * name + W_DATE * date + W_OVERLAP * overlap + W_COUNT * count
    return {"score": round(score, 3), "name": round(name, 3), "date": date,
            "overlap": round(overlap, 3), "count": round(count, 3)}


def accept(f):
    by_name = f["score"] >= MIN_SCORE and f["name"] >= 0.5
    by_numbers = f["overlap"] >= 0.9 and f["date"] == 1.0 and f["count"] >= 0.8
    return by_name or by_numbers


def build_mapping(sets, set_keys, groups, group_keys, overrides):
    """Return (mapping, report).

    sets:       list of TCGdex set dicts (physical only) with id/name/releaseDate
    set_keys:   {setId: set(number keys of its cards)}
    groups:     list of TCGCSV group dicts
    group_keys: {groupId: set(number keys of its card products)}
    overrides:  {setId: {"groups": [gid, ...], "labels": {gid: label}, "note": str}}

    mapping:    {setId: {"groups": [gid...], "labels": {...}, "method": "override"|"auto",
                         "features": {...}}}
    """
    mapping = {}
    claimed = set()
    for s in sets:
        o = overrides.get(s["id"])
        if o is None:
            continue
        gids = [int(g) for g in o.get("groups", [])]
        mapping[s["id"]] = {"groups": gids,
                            "labels": {int(k): v for k, v in o.get("labels", {}).items()},
                            "method": "override", "note": o.get("note", "")}
        claimed.update(gids)

    free_sets = [s for s in sets if s["id"] not in mapping]
    free_groups = [g for g in groups if g["groupId"] not in claimed]
    pairs = []
    for s in free_sets:
        for g in free_groups:
            f = pair_features(s["name"], s.get("releaseDate"), set_keys.get(s["id"], set()),
                              g["name"], g.get("publishedOn"), group_keys.get(g["groupId"], set()))
            if f["score"] >= 0.3:
                pairs.append((f["score"], s["id"], g["groupId"], f))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    best_rejected = {}
    used_s, used_g = set(), set()
    for score, sid, gid, f in pairs:
        if sid in used_s or gid in used_g:
            continue
        if accept(f):
            mapping[sid] = {"groups": [gid], "labels": {}, "method": "auto", "features": f}
            used_s.add(sid)
            used_g.add(gid)
        elif sid not in best_rejected:
            best_rejected[sid] = (gid, f)

    mapped_groups = {g for m in mapping.values() for g in m["groups"]}
    group_by_id = {g["groupId"]: g for g in groups}
    unmatched_sets = []
    for s in sets:
        if s["id"] in mapping and mapping[s["id"]]["groups"]:
            continue
        hint = best_rejected.get(s["id"])
        unmatched_sets.append({
            "setId": s["id"], "name": s["name"], "releaseDate": s.get("releaseDate"),
            "cards": len(set_keys.get(s["id"], ())),
            "reason": (mapping.get(s["id"], {}).get("note") or "no candidate above threshold"),
            "bestCandidate": ({"groupId": hint[0], "name": group_by_id[hint[0]]["name"],
                               "features": hint[1]} if hint else None)})
    unmatched_groups = [
        {"groupId": g["groupId"], "name": g["name"], "publishedOn": g.get("publishedOn", "")[:10],
         "cardProducts": len(group_keys.get(g["groupId"], ()))}
        for g in groups if g["groupId"] not in mapped_groups]
    return mapping, {"unmatchedSets": unmatched_sets, "unmatchedGroups": unmatched_groups}


def keys_for_cards(cards):
    return {number_key(c["localId"]) for c in cards}
