#!/usr/bin/env python3
"""Regenerate every Evodex data output into out/ next to this file.

The pipeline lives in two repositories with identical files: the Evodex app
(tools/pipeline/, the source of truth) and ueski/evodex-feed (pipeline/).

    python3 tools/pipeline/run.py              # normal run (uses + refreshes cache)
    python3 tools/pipeline/run.py --offline    # cache only, no network
    python3 tools/pipeline/run.py --sets sv03.5,base1   # quick partial run
    python3 tools/pipeline/run.py --offline --online-aux
        # catalog + prices from the cache; auxiliary sources (pokemontcg.io,
        # image HEAD checks, PokéAPI) refreshed. --online-images is an alias.

Steps: TCGdex catalog -> name fixes -> TCGCSV groups/products/prices -> set
mapping -> card matching -> variants + prices -> validation against TCGdex's
own TCGplayer ids -> fallback images -> species (dx, species.json) ->
coverage report -> bundle/price files + sizes -> history.
"""

import argparse
import csv
import datetime
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bundle  # noqa: E402
import history  # noqa: E402
import images  # noqa: E402
import matching  # noqa: E402
import net  # noqa: E402
import normalize  # noqa: E402
import setmap  # noqa: E402
import sources  # noqa: E402
import species  # noqa: E402
import variants  # noqa: E402

ERAS = [
    ("WotC (1999-2003)", ["base", "gym", "neo", "lc", "ecard"]),
    ("EX (2003-2007)", ["ex"]),
    ("DP / Platinum / HGSS (2007-2011)", ["dp", "pl", "hgss", "col"]),
    ("Black & White (2011-2013)", ["bw"]),
    ("XY (2013-2016)", ["xy"]),
    ("Sun & Moon (2017-2019)", ["sm"]),
    ("Sword & Shield (2020-2023)", ["swsh"]),
    ("Scarlet & Violet (2023-2025)", ["sv"]),
    ("Mega Evolution (2025-)", ["me"]),
    ("Other: POP, trainer kits, McDonald's, misc", ["pop", "tk", "mc", "misc"]),
]
ERA_OF = {s: name for name, ss in ERAS for s in ss}


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))


def write_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        if compact:
            f.write(bundle.dumps_compact(obj))
        else:
            f.write(json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"))


def write_csv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


# --------------------------------------------------------------------- build

def load_overrides():
    with open(os.path.join(HERE, "overrides.json"), encoding="utf-8") as f:
        return json.load(f)


def tcgdex_ids(card):
    ids = set()
    for vd in card.get("variants_detailed") or []:
        tp = (vd.get("thirdParty") or {}).get("tcgplayer")
        if tp:
            ids.add(int(tp))
    return ids


def fix_names(sets, card_details, name_overrides):
    """English names for the catalog (QA-17), applied to the set briefs (used
    by matching) and the card details (used for the records) alike:
    overrides.json "names" first, then normalize.clean_card_name.
    Returns [cardId, old, new, method] rows; method "override", "rule" or
    "unresolved" (still has foreign script: add an override)."""
    rows = []
    for s in sets:
        if s.get("digital"):
            continue
        for brief in s.get("cards", []):
            cid = brief["id"]
            d = card_details.get(cid) or {}
            old = d.get("name", brief["name"])
            if cid in name_overrides:
                new, method = name_overrides[cid], "override"
            else:
                new = normalize.clean_card_name(old)
                method = "rule"
            if normalize.has_foreign_script(new):
                rows.append([cid, old, new, "unresolved"])
                continue
            if new != old:
                brief["name"] = new
                if d:
                    d["name"] = new
                rows.append([cid, old, new, method])
            elif normalize.has_foreign_script(brief["name"]):
                brief["name"] = new   # only the set brief (used by matching) was foreign
    return rows


def load_image_status(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def check_image_urls(urls, status_cache, offline, workers=4):
    """HEAD every URL whose cached status is missing or stale (200: 90 days,
    anything else: 7 days). Offline: cache only. Returns the number checked.

    A host with a placeholder probe (images.PLACEHOLDER_PROBES: Scrydex answers
    200 with a card back for unknown ids) is probed once first; a 200 with the
    probe's ETag is recorded as 404 ("placeholder"). If the probe itself fails,
    that host's URLs are not checked this run (their cached status stays)."""
    now = time.time()

    def fresh(u):
        e = status_cache.get(u)
        if not e:
            return False
        return offline or now - e[1] <= (90 if e[0] == 200 else 7) * 86400

    todo = sorted({u for u in urls if u and not fresh(u)})
    if offline or not todo:
        return 0
    # <= 10 HEAD/s per host; built up front so the worker threads only read it
    hosts = {urllib.parse.urlparse(u).netloc for u in todo}
    limiters = {host: net.RateLimiter(0.1) for host in hosts}
    signatures = {}
    for host in sorted(hosts & set(images.PLACEHOLDER_PROBES)):
        code, headers = net.http_head(images.PLACEHOLDER_PROBES[host], limiter=limiters[host])
        signatures[host] = images.placeholder_signature(headers) if code == 200 else None
        if signatures[host] is None:
            log("  ! %s placeholder probe failed (HTTP %s); its URLs are not checked this run" % (host, code))
            todo = [u for u in todo if urllib.parse.urlparse(u).netloc != host]

    def head(u):
        host = urllib.parse.urlparse(u).netloc
        code, headers = net.http_head(u, limiter=limiters[host])
        if code == 200 and images.is_placeholder(headers, signatures.get(host)):
            return [404, int(now), "placeholder"]
        return [code, int(now)]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (u, entry) in enumerate(zip(todo, pool.map(head, todo)), 1):
            status_cache[u] = entry
            if i % 250 == 0 or i == len(todo):
                log("  image HEAD %d/%d" % (i, len(todo)))
    return len(todo)


def add_image_fallbacks(args, overrides, physical, cards_out):
    """For cards without a TCGdex image (images.py): a TCGdex twin's image
    (c["image"], c["imageTwin"]), else c["imageFallback"] = {"source", "url"}
    from pokemontcg.io / Scrydex. Returns the report block for summary.json."""
    need = [c for c in cards_out if not c.get("image")]
    twins = images.twin_images([
        {"id": c["id"], "localId": c["localId"], "name": c["name"], "image": c.get("image"),
         "products": [v["productId"] for v in (c.get("tcgplayer") or {}).get("variants", [])]}
        for c in cards_out])
    twin_of = {}
    for c in need:
        img, twin = twins.get(c["id"], (None, None))
        if img and images.allowed_tcgdex(img):
            c["image"] = img
            c["imageTwin"] = twin_of[c["id"]] = twin
    still = [c for c in need if not c.get("image")]

    # --offline --online-aux: catalog and prices from the cache, image sources live
    offline = args.offline and not args.online_aux
    ptcg = sources.PokemonTCGio(args.cache, offline=offline)
    try:
        ptcg_sets = ptcg.sets()
    except net.FetchError as e:
        log("  ! pokemontcg.io set list unavailable: %s" % e)
        ptcg_sets = []
    set_map = images.map_sets(
        [{"id": s["id"], "name": s["name"], "releaseDate": s.get("releaseDate")} for s in physical],
        [{"id": p["id"], "name": p["name"], "releaseDate": p.get("releaseDate")} for p in ptcg_sets],
        {k: v for k, v in overrides.get("ptcgioSets", {}).items() if not k.startswith("_")})

    by_set = defaultdict(list)
    for c in still:
        by_set[c["setId"]].append(c)
    ptcg_hit = {}
    for sid in sorted(by_set):
        target = set_map.get(sid, (None, "none"))[0]
        if not target:
            continue
        try:
            pcards = ptcg.cards(target)
        except net.FetchError as e:
            log("  ! pokemontcg.io %s unavailable: %s" % (target, e))
            continue
        briefs = [{"id": c["id"], "localId": c["localId"], "name": c["name"],
                   "printedNumber": (c.get("tcgplayer") or {}).get("number")} for c in by_set[sid]]
        ptcg_hit.update(images.match_cards(briefs, pcards))

    candidates = {c["id"]: images.candidates(ptcg_hit[c["id"]]) if c["id"] in ptcg_hit else []
                  for c in still}

    status_path = os.path.join(args.cache, "images", "status.json")
    status_cache = load_image_status(status_path)
    checked = 0
    if not args.no_image_check:
        # rank by rank: the next choice is checked only for the cards whose earlier choices all failed
        settled = set()
        for rank in range(max((len(cs) for cs in candidates.values()), default=0)):
            todo = [cs[rank][1] for cid, cs in candidates.items() if cid not in settled and rank < len(cs)]
            checked += check_image_urls([u for u in todo if images.allowed(u)], status_cache, offline)
            settled |= {cid for cid, cs in candidates.items()
                        if rank < len(cs) and cs[rank][1] and images.allowed(cs[rank][1])
                        and (status_cache.get(cs[rank][1]) or [200])[0] == 200}
        if checked:
            net._atomic_write(status_path, json.dumps(status_cache, sort_keys=True, indent=0).encode("utf-8"))

    def status(url):
        e = status_cache.get(url)
        return e[0] if e else None

    counts = defaultdict(int)
    failed = defaultdict(int)       # candidates rejected by the HEAD check, per source
    placeholders = 0                # ... of which Scrydex card-back placeholders
    unchecked = defaultdict(int)    # chosen without a recorded status (offline, never checked)
    uncovered = defaultdict(list)
    for c in still:
        for source, url in candidates[c["id"]]:
            if url and status(url) not in (None, 200):
                failed[source] += 1
                placeholders += len(status_cache[url]) > 2 and status_cache[url][2] == "placeholder"
        source, url = images.choose(candidates[c["id"]], status)
        if url:
            assert images.allowed(url), url
            c["imageFallback"] = {"source": source, "url": url}
            counts[source] += 1
            if status(url) is None:
                unchecked[source] += 1
        else:
            counts["none"] += 1
            uncovered[c["setId"]].append(c["localId"])
    need_by_set = defaultdict(list)
    for c in need:
        need_by_set[c["setId"]].append(c)
    total = len(cards_out)
    return {
        "cards": total, "tcgdex": total - len(need), "withoutTcgdex": len(need),
        "tcgdexTwin": len(twin_of), "twins": dict(sorted(twin_of.items())),
        "fallback": {s: counts[s] for s in images.SOURCES}, "none": counts["none"],
        "coveredPct": pct(total - counts["none"], total),
        "headChecked": checked, "rejectedByHead": dict(failed), "placeholders": placeholders,
        "chosenUnchecked": dict(unchecked),
        "ptcgioSources": dict(sorted(ptcg.sources.items())),
        "setMap": {sid: {"ptcgioSet": set_map.get(sid, (None, "none"))[0],
                         "method": set_map.get(sid, (None, "none"))[1],
                         "cards": len(cs),
                         "twin": sum(1 for c in cs if c.get("imageTwin")),
                         "ptcgio": sum(1 for c in cs if (c.get("imageFallback") or {}).get("source") == "ptcgio"),
                         "scrydex": sum(1 for c in cs
                                        if (c.get("imageFallback") or {}).get("source") == "scrydex"),
                         "none": len(uncovered.get(sid, []))}
                   for sid, cs in sorted(need_by_set.items())},
        "uncovered": {sid: ids for sid, ids in sorted(uncovered.items())},
    }


def add_species(args, overrides, cards_out, set_meta):
    """Sets c["dx"] on every Pokémon card (species.card_species) and returns
    (species.json document, report block for summary.json)."""
    api = sources.PokeAPI(args.cache, offline=args.offline and not args.online_aux)
    species_list, problems = species.build_species(api.tables())
    index = species.SpeciesIndex(species_list)
    pins = {k: v for k, v in overrides.get("dex", {}).items() if not k.startswith("_")}
    methods = defaultdict(int)
    validation = defaultdict(int)       # name rule vs TCGdex dexId, where TCGdex has one
    corrected, unmatched, name_only = [], [], []
    pokemon = [c for c in cards_out if c.get("category") == "Pokemon"]
    for c in pokemon:
        ids, method = species.card_species(c["id"], c["name"], c.get("dexId"), index, pins)
        methods[method] += 1
        if ids:
            c["dx"] = ids
        found = index.find(c["name"])
        if c.get("dexId"):
            dex = set(c["dexId"])
            validation["same" if set(found) == dex else "nameEmpty" if not found
                       else "overlap" if set(found) & dex else "disjoint"] += 1
        else:
            validation["noDexId"] += 1
        if method == "name-corrected":
            corrected.append([c["id"], c["name"], c.get("dexId"), ids])
        elif method == "name":
            name_only.append([c["id"], c["name"], ids])
        elif method == "none":
            unmatched.append([c["id"], c["name"]])
        if not found:
            validation["nameRuleAloneFails"] += 1
    rc = species.representative_cards(
        [dict(c, dx=c.get("dx")) for c in pokemon],
        {sid: {"releaseDate": m.get("releaseDate"), "official": m.get("official")} for sid, m in set_meta.items()},
        species_list)
    with_cards = {i for c in pokemon for i in c.get("dx", [])}
    doc = species.species_file(species_list, rc)
    report = {
        "species": len(species_list), "problems": problems,
        "byGeneration": {g: sum(1 for s in species_list if s["gen"] == g) for g, *_ in species.GENERATIONS},
        "pokemonCards": len(pokemon), "withSpecies": sum(1 for c in pokemon if c.get("dx")),
        "multiSpecies": sum(1 for c in pokemon if len(c.get("dx") or []) > 1),
        "methods": dict(methods), "nameRuleVsTcgdex": dict(validation),
        "corrected": corrected, "nameOnly": name_only, "unmatched": unmatched,
        "speciesWithoutCards": [s["id"] for s in species_list if s["id"] not in with_cards],
        "representative": len(rc),
        "otherCategoriesWithDexId": sum(1 for c in cards_out
                                        if c.get("category") != "Pokemon" and c.get("dexId")),
    }
    return doc, report


def build(args):
    t0 = time.time()
    overrides = load_overrides()
    only = set(args.sets.split(",")) if args.sets else None

    log("TCGdex catalog ...")
    dex = sources.TCGdex(args.cache, workers=args.workers, card_ttl=args.card_ttl_days * 86400,
                         offline=args.offline)
    series, sets, card_details = dex.fetch_catalog(only)
    log("  %d sets, %d card details  (%s)" % (len(sets), len(card_details), net.STATS))
    name_fixes = fix_names(sets, card_details,
                           {k: v for k, v in overrides.get("names", {}).items() if not k.startswith("_")})
    log("  names: %d fixed, %d unresolved" % (
        sum(1 for r in name_fixes if r[3] != "unresolved"), sum(1 for r in name_fixes if r[3] == "unresolved")))

    log("TCGCSV groups/products/prices ...")
    csv_src = sources.TCGCSV(args.cache, offline=args.offline)
    groups, products_raw, prices_raw = csv_src.fetch_all()
    build_stamp = csv_src.last_updated_raw()
    log("  build %s, %d groups  (%s)" % (build_stamp, len(groups), net.STATS))

    series_name = {s["id"]: s["name"] for s in series}
    physical = [s for s in sets if not s["digital"]]
    digital = [s for s in sets if s["digital"]]

    # ---- set mapping
    gprods = {gid: matching.card_products(ps) for gid, ps in products_raw.items()}
    set_cards = {s["id"]: s.get("cards", []) for s in physical}
    set_keys = {sid: setmap.keys_for_cards(cs) for sid, cs in set_cards.items()}
    group_keys = {gid: {p["key"] for p in ps if p["key"]} for gid, ps in gprods.items()}
    set_ov = {k: v for k, v in overrides["sets"].items()}
    mapping, map_report = setmap.build_mapping(physical, set_keys, groups, group_keys, set_ov)
    tcgplayer_only = {int(k): v for k, v in overrides.get("tcgplayerOnlyGroups", {}).items()}
    today = datetime.date.today().isoformat()
    map_report["unmatchedGroups"] = [
        dict(g, knownTcgplayerOnly=g["groupId"] in tcgplayer_only,
             upcoming=g["publishedOn"] > today and g["cardProducts"] == 0)
        for g in map_report["unmatchedGroups"]]

    # ---- matching
    prices_by_pid = defaultdict(list)
    for rows in prices_raw.values():
        for r in rows:
            prices_by_pid[r["productId"]].append(r)
    product_index = {p["productId"]: p for ps in gprods.values() for p in ps}
    card_ov = {cid: pids for cid, pids in overrides.get("cards", {}).items()}

    matches = {}
    for s in physical:
        m = mapping.get(s["id"])
        if not m or not m["groups"]:
            continue
        pool = [p for gid in m["groups"] for p in gprods.get(gid, [])]
        o = set_ov.get(s["id"], {})
        pool = matching.filter_pool(pool, o.get("include"), o.get("exclude"))
        res, _ = matching.match_set(set_cards[s["id"]], pool, card_ov)
        matches.update(res)

    # cards still unmatched get one more number+name pass against the shared
    # fallback groups (e.g. 'Alternate Art Promos' holds the XY "a" alternates
    # of a dozen different sets). Products already taken stay eligible because
    # TCGdex lists some alternates twice (xya-24a and xy4-24a are one card).
    fallback_pool = [p for gid in overrides.get("fallbackGroups", {})
                     for p in gprods.get(int(gid), [])]
    if fallback_pool:
        left = [c for s in physical for c in set_cards[s["id"]] if c["id"] not in matches]
        res, _ = matching.match_set(left, fallback_pool, name_fallback=False)
        for cid, r in res.items():
            r["method"] = "fallback-group"
        matches.update(res)

    # same product claimed by cards of two sets (shared groups) -> report
    claims = defaultdict(list)
    for cid, r in matches.items():
        for p in r["products"]:
            claims[p["productId"]].append(cid)
    conflicts = {pid: cids for pid, cids in claims.items() if len(cids) > 1}

    # TCGdex's own TCGplayer ids: validation + optional fallback
    fallback_used = 0
    if not args.no_tcgdex_fallback:
        for s in physical:
            for c in set_cards[s["id"]]:
                if c["id"] in matches:
                    continue
                d = card_details.get(c["id"]) or {}
                ids = [i for i in sorted(tcgdex_ids(d)) if i in product_index and i not in claims]
                if not ids:
                    continue
                prods = [dict(product_index[i], nameScore=1.0) for i in ids]
                matching.choose_primary(prods)
                matches[c["id"]] = {"method": "tcgdex-id", "products": prods}
                for i in ids:
                    claims[i].append(c["id"])
                fallback_used += 1

    # singles in mapped groups that no card claimed (TCGplayer-only printings,
    # TCGdex gaps, or matcher misses)
    set_of_group = defaultdict(list)
    for sid, m in mapping.items():
        for gid in m["groups"]:
            set_of_group[gid].append(sid)
    unassigned_products = [dict(p, setId=",".join(set_of_group[gid]))
                           for gid in sorted(set_of_group) for p in gprods.get(gid, [])
                           if p["productId"] not in claims]

    # ---- card records
    set_meta = {}
    for s in physical:
        cc = s.get("cardCount", {})
        set_meta[s["id"]] = {
            "id": s["id"], "name": s["name"], "series": s["serie"]["id"],
            "seriesName": s["serie"]["name"], "releaseDate": s.get("releaseDate"),
            "official": cc.get("official"), "total": cc.get("total"),
            "logo": s.get("logo"), "symbol": s.get("symbol"),
            "abbreviation": (s.get("abbreviation") or {}).get("official"),
            "groups": (mapping.get(s["id"]) or {}).get("groups", []),
            "mapping": (mapping.get(s["id"]) or {}).get("method"),
        }
    cards_out = []
    validation = {"tcgdexHasId": 0, "bothHave": 0, "primaryAgrees": 0, "anyAgrees": 0,
                  "disagree": [], "weMiss": 0}
    tcgdex_price_stats = {"cards": 0, "tcgplayerPricing": 0, "cardmarketPricing": 0,
                          "cardmarketAvg30": 0}
    for s in physical:
        labels = (mapping.get(s["id"]) or {}).get("labels", {})
        for brief in set_cards[s["id"]]:
            cid = brief["id"]
            d = card_details.get(cid) or {}
            rec = {
                "id": cid, "setId": s["id"], "localId": brief["localId"],
                "name": d.get("name", brief["name"]), "category": d.get("category"),
                "rarity": d.get("rarity"), "types": d.get("types"), "hp": d.get("hp"),
                "illustrator": d.get("illustrator"), "image": d.get("image", brief.get("image")),
                "dexId": d.get("dexId"),
                "tcgdexVariants": d.get("variants"), "tcgplayer": None,
            }
            r = matches.get(cid)
            if r:
                prods = r["products"]
                vs = variants.build_variants(prods, prices_by_pid, labels,
                                             (mapping.get(s["id"]) or {}).get("groups", []))
                main = next(p for p in prods if p["productId"] == vs[0]["productId"])
                rec["tcgplayer"] = {
                    "productId": main["productId"], "groupId": main["groupId"],
                    "number": main.get("number"), "method": r["method"],
                    "url": "https://www.tcgplayer.com/product/%d" % main["productId"],
                    "variants": vs,
                    "alternates": [{"productId": p["productId"], "name": p["name"]}
                                   for p in prods if not p.get("primary")],
                }
            # validation against TCGdex variants_detailed[].thirdParty.tcgplayer
            ids = tcgdex_ids(d)
            if ids:
                validation["tcgdexHasId"] += 1
                if r and r["method"] != "tcgdex-id":
                    ours = {p["productId"] for p in r["products"]}
                    validation["bothHave"] += 1
                    if rec["tcgplayer"]["productId"] in ids:
                        validation["primaryAgrees"] += 1
                    if ours & ids:
                        validation["anyAgrees"] += 1
                    else:
                        validation["disagree"].append(
                            [cid, rec["name"], brief["localId"], sorted(ours), sorted(ids),
                             [product_index[i]["name"] + " #" + str(product_index[i]["number"])
                              for i in sorted(ids) if i in product_index]])
                elif not r:
                    validation["weMiss"] += 1
            if d:
                tcgdex_price_stats["cards"] += 1
                pr = d.get("pricing") or {}
                if pr.get("tcgplayer"):
                    tcgdex_price_stats["tcgplayerPricing"] += 1
                if pr.get("cardmarket"):
                    tcgdex_price_stats["cardmarketPricing"] += 1
                    if pr["cardmarket"].get("avg30") is not None:
                        tcgdex_price_stats["cardmarketAvg30"] += 1
            cards_out.append(rec)

    # ---- fallback images for cards TCGdex has no scan for (images.py)
    log("Fallback images ...")
    image_report = add_image_fallbacks(args, overrides, physical, cards_out)
    log("  %d without TCGdex image: TCGdex twin %d, %s, none %d  (%s)" % (
        image_report["withoutTcgdex"], image_report["tcgdexTwin"], image_report["fallback"],
        image_report["none"], net.STATS))

    # ---- species: dx on Pokémon cards + species.json (species.py)
    log("Species ...")
    species_doc, species_report = add_species(args, overrides, cards_out, set_meta)
    log("  %d species; Pokémon cards with dx %d/%d; methods %s" % (
        species_report["species"], species_report["withSpecies"], species_report["pokemonCards"],
        species_report["methods"]))

    out = args.out
    gen_at = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    # ---- coverage
    cov = coverage(physical, cards_out, set_meta, series_name)

    # ---- bundle + sizes
    sets_out = [set_meta[s["id"]] for s in physical]
    idx = bundle.compact_index(series, sets_out, cards_out, list(variants.SUBTYPES), groups=groups)
    idx_blob = bundle.dumps_compact(idx)
    verbose_blob = bundle.dumps_compact(bundle.verbose_index(cards_out))
    pids = {v["productId"] for c in cards_out if c["tcgplayer"] for v in c["tcgplayer"]["variants"]}
    snap = bundle.price_snapshot(prices_raw, pids, build_stamp, list(variants.SUBTYPES))
    snap_blob = bundle.dumps_compact(snap)
    snap_mkt_blob = bundle.dumps_compact(bundle.market_only(snap))
    all_pids = {r["productId"] for rows in prices_raw.values() for r in rows}
    snap_all_blob = bundle.dumps_compact(
        bundle.price_snapshot(prices_raw, all_pids, build_stamp, list(variants.SUBTYPES)))
    full_blob = bundle.dumps_compact({"sets": sets_out, "cards": cards_out})

    # per-set price files (on-demand download granularity)
    per_set_sizes = []
    for s in physical:
        spids = {v["productId"] for c in cards_out if c["setId"] == s["id"] and c["tcgplayer"]
                 for v in c["tcgplayer"]["variants"]}
        if not spids:
            continue
        b = bundle.dumps_compact(bundle.price_snapshot(prices_raw, spids, build_stamp,
                                                       list(variants.SUBTYPES)))
        per_set_sizes.append((s["id"], len(b), len(gzip.compress(b, 9))))
        write_json(os.path.join(out, "bundle", "prices-by-set", s["id"] + ".json"),
                   json.loads(b), compact=True)

    size_report = {
        "index (compact, bundled)": bundle.sizes(idx_blob),
        "index (verbose keys, for comparison)": bundle.sizes(verbose_blob),
        "prices snapshot (matched products, 5 fields)": bundle.sizes(snap_blob),
        "prices snapshot (matched products, market only)": bundle.sizes(snap_mkt_blob),
        "prices snapshot (all Pokemon products, 5 fields)": bundle.sizes(snap_all_blob),
        "catalog_full.json (debug/server)": bundle.sizes(full_blob),
    }
    if per_set_sizes:
        gz = sorted(x[2] for x in per_set_sizes)
        size_report["per-set price file gzip (median / max)"] = {
            "median": gz[len(gz) // 2], "max": gz[-1]}

    # ---- history
    hist_groups = sorted({g for m in mapping.values() for g in m["groups"]})
    hist = history.write_all(args.cache, os.path.join(out, "history"), hist_groups)
    sample_gid = args.history_sample_group
    sample = history.build_group_history(args.cache, sample_gid)
    projection = history.project_sizes(sample) if sample["dates"] else None
    all_series = sum(len(history.build_group_history(args.cache, g)["s"]) for g in hist_groups)

    # ---- write everything
    write_json(os.path.join(out, "bundle", "index.json"), idx, compact=True)
    species_blob = bundle.dumps_compact(species_doc)
    species_report["sizes"] = bundle.sizes(species_blob)
    write_json(os.path.join(out, "bundle", "species.json"), species_doc, compact=True)
    write_json(os.path.join(out, "bundle", "prices.json"), snap, compact=True)
    write_json(os.path.join(out, "bundle", "prices-market.json"), json.loads(snap_mkt_blob), compact=True)
    write_json(os.path.join(out, "catalog_full.json"), {"sets": sets_out, "cards": cards_out}, compact=True)
    # manifest.json: the one small file the app polls (ETag / If-None-Match)
    write_json(os.path.join(out, "bundle", "manifest.json"), {
        "v": 1, "generatedAt": gen_at, "tcgcsvBuild": build_stamp, "date": build_stamp[:10],
        "index": {"path": "index.json", "sha256": hashlib.sha256(idx_blob).hexdigest(),
                  "bytes": len(idx_blob), "cards": len(cards_out), "sets": len(sets_out)},
        "prices": {"path": "prices.json", "sha256": hashlib.sha256(snap_blob).hexdigest(),
                   "bytes": len(snap_blob)},
        "pricesMarket": {"path": "prices-market.json", "sha256": hashlib.sha256(snap_mkt_blob).hexdigest(),
                         "bytes": len(snap_mkt_blob)},
        "history": {"pathTemplate": "history/{groupId}.json", "days": hist["days"]},
    })
    write_json(os.path.join(out, "set_mapping.json"), {
        "mapping": {sid: dict(m, groupNames=[g["name"] for g in groups if g["groupId"] in m["groups"]])
                    for sid, m in sorted(mapping.items())},
        "digitalSetsExcluded": [{"id": s["id"], "name": s["name"]} for s in digital],
        **map_report})
    write_json(os.path.join(out, "coverage.json"), cov)
    write_csv(os.path.join(out, "unmatched_cards.csv"), ["cardId", "setId", "localId", "name", "reason"],
              [[c["id"], c["setId"], c["localId"], c["name"],
                "set not mapped" if not set_meta[c["setId"]]["groups"] else "no product"]
               for c in cards_out if not c["tcgplayer"]])
    write_csv(os.path.join(out, "unmatched_products.csv"),
              ["setId", "groupId", "productId", "number", "name", "rarity"],
              [[p["setId"], p["groupId"], p["productId"], p["number"], p["name"], p["rarity"]]
               for p in unassigned_products])
    write_csv(os.path.join(out, "review_matches.csv"),
              ["cardId", "name", "method", "productId", "productName", "number", "nameScore"],
              [[c["id"], c["name"], c["tcgplayer"]["method"], p["productId"], p["name"], p["number"],
                p.get("nameScore")]
               for c in cards_out if c["tcgplayer"] and c["tcgplayer"]["method"] not in ("number+name",)
               for p in matches[c["id"]]["products"]])
    write_csv(os.path.join(out, "validation_disagreements.csv"),
              ["cardId", "name", "localId", "ourProductIds", "tcgdexProductIds", "tcgdexProductNames"],
              [[a, b, c, " ".join(map(str, d)), " ".join(map(str, e)), " | ".join(f)]
               for a, b, c, d, e, f in validation["disagree"]])
    write_json(os.path.join(out, "conflicts.json"),
               {str(pid): cids for pid, cids in sorted(conflicts.items())})

    summary = {
        "generatedAt": gen_at, "tcgcsvBuild": build_stamp,
        "tcgdex": {"sets": len(sets), "physicalSets": len(physical), "digitalSets": len(digital),
                   "cards": len(cards_out), "cardDetails": len(card_details)},
        "tcgcsv": {"groups": len(groups),
                   "products": sum(len(v) for v in products_raw.values()),
                   "cardProducts": sum(len(v) for v in gprods.values()),
                   "priceRows": sum(len(v) for v in prices_raw.values())},
        "mapping": {"mappedSets": sum(1 for m in mapping.values() if m["groups"]),
                    "auto": sum(1 for m in mapping.values() if m["method"] == "auto"),
                    "override": sum(1 for m in mapping.values() if m["method"] == "override" and m["groups"]),
                    "unmatchedSets": len(map_report["unmatchedSets"]),
                    "unmatchedGroups": len(map_report["unmatchedGroups"]),
                    "unexpectedUnmatchedGroups": sum(
                        1 for g in map_report["unmatchedGroups"]
                        if not g["knownTcgplayerOnly"] and not g["upcoming"])},
        "matchMethods": count_methods(cards_out),
        "tcgdexFallbackUsed": fallback_used,
        "conflicts": len(conflicts),
        "validation": {k: (len(v) if isinstance(v, list) else v) for k, v in validation.items()},
        "tcgdexPricing": tcgdex_price_stats,
        "images": image_report,
        "species": species_report,
        "names": {"fixed": sum(1 for r in name_fixes if r[3] != "unresolved"),
                  "unresolved": sum(1 for r in name_fixes if r[3] == "unresolved"),
                  "changes": name_fixes},
        "sizes": size_report,
        "history": {"builds": len(history.builds(args.cache)), "written": hist,
                    "sampleGroup": sample_gid, "projection": projection,
                    "seriesAllMappedGroups": all_series},
        "requests": repr(net.STATS), "seconds": round(time.time() - t0, 1),
    }
    write_json(os.path.join(out, "summary.json"), summary)
    with open(os.path.join(out, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write(render_report(summary, cov, map_report, set_meta))
    log("done in %.0fs -> %s" % (time.time() - t0, out))
    print_summary(summary, cov, out)
    return summary


def count_methods(cards_out):
    out = defaultdict(int)
    for c in cards_out:
        out[c["tcgplayer"]["method"] if c["tcgplayer"] else "unmatched"] += 1
    return dict(out)


def coverage(physical, cards_out, set_meta, series_name):
    def blank():
        return {"cards": 0, "productId": 0, "market": 0, "variants": 0, "variantsMarket": 0}

    def add(b, c):
        b["cards"] += 1
        tp = c["tcgplayer"]
        if tp:
            b["productId"] += 1
            if any(v["marketPrice"] is not None for v in tp["variants"]):
                b["market"] += 1
            b["variants"] += len(tp["variants"])
            b["variantsMarket"] += sum(1 for v in tp["variants"] if v["marketPrice"] is not None)

    # TCGdex says which printings exist (variants.normal/holo/reverse/firstEdition);
    # how many of those have a TCGplayer price row on the card's primary products?
    flags = {f: {"expected": 0, "found": 0} for f in variants.TCGDEX_TO_TCGPLAYER}
    for c in cards_out:
        tp, tv = c["tcgplayer"], c.get("tcgdexVariants") or {}
        if not tp:
            continue
        subs = {v["subType"] for v in tp["variants"] if v["primary"] and v["subType"]}
        for f, names in variants.TCGDEX_TO_TCGPLAYER.items():
            if tv.get(f):
                flags[f]["expected"] += 1
                if subs & set(names):
                    flags[f]["found"] += 1

    overall, mapped_only = blank(), blank()
    by_series, by_era, by_set = defaultdict(blank), defaultdict(blank), defaultdict(blank)
    for c in cards_out:
        sm = set_meta[c["setId"]]
        add(overall, c)
        if sm["groups"]:
            add(mapped_only, c)
        add(by_series[sm["series"]], c)
        add(by_era[ERA_OF.get(sm["series"], "Other")], c)
        add(by_set[c["setId"]], c)

    def fmt(b):
        return dict(b, productIdPct=pct(b["productId"], b["cards"]), marketPct=pct(b["market"], b["cards"]),
                    variantsMarketPct=pct(b["variantsMarket"], b["variants"]))

    first_date = {}
    for s in physical:
        sid = s["serie"]["id"]
        d = s.get("releaseDate") or "9999"
        first_date[sid] = min(first_date.get(sid, "9999"), d)
    sets_rows = []
    for s in physical:
        b = fmt(by_set[s["id"]])
        b.update({"setId": s["id"], "name": s["name"], "series": s["serie"]["id"],
                  "releaseDate": s.get("releaseDate"), "groups": set_meta[s["id"]]["groups"]})
        sets_rows.append(b)
    worst = sorted([r for r in sets_rows if r["cards"] >= 5],
                   key=lambda r: (r["productIdPct"], r["marketPct"], -r["cards"]))[:20]
    worst_market = sorted([r for r in sets_rows if r["cards"] >= 5 and r["productIdPct"] >= 90],
                          key=lambda r: (r["marketPct"], -r["cards"]))[:10]
    return {
        "overall": fmt(overall), "mappedSetsOnly": fmt(mapped_only),
        "byEra": [dict(fmt(by_era[name]), era=name) for name, _ in ERAS if by_era[name]["cards"]],
        "bySeries": [dict(fmt(by_series[sid]), series=sid, name=series_name.get(sid, sid))
                     for sid in sorted(by_series, key=lambda x: first_date.get(x, "9999"))],
        "bySet": sets_rows, "worstSets": worst, "worstMarketAmongMatchedSets": worst_market,
        "tcgdexVariantFlags": {f: dict(v, pct=pct(v["found"], v["expected"])) for f, v in flags.items()},
    }


# -------------------------------------------------------------------- report

def render_report(summary, cov, map_report, set_meta):
    L = []
    a = L.append
    a("# Evodex data pipeline report\n")
    a("Generated %s from TCGCSV build %s.\n" % (summary["generatedAt"], summary["tcgcsvBuild"]))
    t, c = summary["tcgdex"], summary["tcgcsv"]
    a("TCGdex: %d sets (%d physical, %d TCG Pocket excluded), %d cards. "
      "TCGCSV: %d groups, %d products (%d singles), %d price rows.\n" % (
          t["sets"], t["physicalSets"], t["digitalSets"], t["cards"], c["groups"], c["products"],
          c["cardProducts"], c["priceRows"]))
    o, mo = cov["overall"], cov["mappedSetsOnly"]
    a("## Coverage\n")
    a("| Scope | Cards | With productId | With market price | Variants with market |")
    a("|---|---:|---:|---:|---:|")
    a("| **All physical cards** | %d | %d (%.1f%%) | %d (%.1f%%) | %d/%d (%.1f%%) |" % (
        o["cards"], o["productId"], o["productIdPct"], o["market"], o["marketPct"],
        o["variantsMarket"], o["variants"], o["variantsMarketPct"]))
    a("| Cards in mapped sets | %d | %d (%.1f%%) | %d (%.1f%%) | %.1f%% |" % (
        mo["cards"], mo["productId"], mo["productIdPct"], mo["market"], mo["marketPct"],
        mo["variantsMarketPct"]))
    for e in cov["byEra"]:
        a("| %s | %d | %.1f%% | %.1f%% | %.1f%% |" % (e["era"], e["cards"], e["productIdPct"],
                                                     e["marketPct"], e["variantsMarketPct"]))
    a("\nMatch methods: %s. TCGdex-id fallback used for %d cards. Product conflicts: %d.\n" % (
        ", ".join("%s %d" % kv for kv in sorted(summary["matchMethods"].items())),
        summary["tcgdexFallbackUsed"], summary["conflicts"]))
    v = summary["validation"]
    a("Validation against TCGdex `variants_detailed[].thirdParty.tcgplayer`: TCGdex has an id for "
      "%d cards; both have one for %d; our primary productId agrees for %d (%.1f%%), any of our "
      "products agrees for %d (%.1f%%); %d disagreements (see validation_disagreements.csv); "
      "TCGdex has an id where our matcher found nothing for %d.\n" % (
          v["tcgdexHasId"], v["bothHave"], v["primaryAgrees"], pct(v["primaryAgrees"], v["bothHave"]),
          v["anyAgrees"], pct(v["anyAgrees"], v["bothHave"]), v["disagree"], v["weMiss"]))
    a("TCGdex variant flags with a matching TCGplayer printing on the primary product: %s.\n" % (
        ", ".join("%s %d/%d (%.1f%%)" % (f, v["found"], v["expected"], v["pct"])
                  for f, v in cov["tcgdexVariantFlags"].items())))
    a("## Worst sets (by productId coverage, >= 5 cards)\n")
    a("| Set | Name | Cards | productId | market | groups |")
    a("|---|---|---:|---:|---:|---|")
    for r in cov["worstSets"]:
        a("| %s | %s | %d | %.1f%% | %.1f%% | %s |" % (
            r["setId"], r["name"], r["cards"], r["productIdPct"], r["marketPct"], r["groups"]))
    a("\n## Lowest market-price coverage among well-matched sets\n")
    a("| Set | Name | Cards | productId | market |")
    a("|---|---|---:|---:|---:|")
    for r in cov["worstMarketAmongMatchedSets"]:
        a("| %s | %s | %d | %.1f%% | %.1f%% |" % (r["setId"], r["name"], r["cards"],
                                                   r["productIdPct"], r["marketPct"]))
    a("\n## By series\n")
    a("| Series | Cards | productId | market |")
    a("|---|---:|---:|---:|")
    for r in cov["bySeries"]:
        a("| %s (%s) | %d | %.1f%% | %.1f%% |" % (r["name"], r["series"], r["cards"],
                                                  r["productIdPct"], r["marketPct"]))
    a("\n## Unmatched TCGdex sets\n")
    for u in map_report["unmatchedSets"]:
        a("- `%s` %s (%s, %d cards): %s" % (u["setId"], u["name"], u["releaseDate"], u["cards"],
                                            u["reason"]))
    a("\n## Unmatched TCGplayer groups\n")
    for g in map_report["unmatchedGroups"]:
        tag = ("" if g["knownTcgplayerOnly"] else
               "  (upcoming, not on TCGdex yet)" if g["upcoming"] else "  **unexpected**")
        a("- %d %s (%s, %d singles)%s" % (g["groupId"], g["name"], g["publishedOn"], g["cardProducts"], tag))
    a("\n## Sizes\n")
    a("| Artifact | Raw | gzip -9 | xz -9e |")
    a("|---|---:|---:|---:|")
    for k, s in summary["sizes"].items():
        if "raw" in s:
            a("| %s | %s | %s | %s |" % (k, bundle.human(s["raw"]), bundle.human(s["gzip"]),
                                         bundle.human(s["xz"])))
        else:
            a("| %s | median %s | max %s | |" % (k, bundle.human(s["median"]), bundle.human(s["max"])))
    h = summary["history"]
    a("\n## Price history\n")
    a("Daily builds in cache: %d. History files written for %d groups (%s raw / %s gzip)." % (
        h["builds"], h["written"]["groups"], bundle.human(h["written"]["raw"]),
        bundle.human(h["written"]["gzip"])))
    if h["projection"]:
        p = h["projection"]
        a(" Projection for group %d (%d series, simulated movement): 90 days %s raw / %s gzip; "
          "365 days %s raw / %s gzip. Series across all mapped groups: %d." % (
              h["sampleGroup"], p["series"], bundle.human(p["90d"]["raw"]),
              bundle.human(p["90d"]["gzip"]), bundle.human(p["365d"]["raw"]),
              bundle.human(p["365d"]["gzip"]), h["seriesAllMappedGroups"]))
    tp = summary["tcgdexPricing"]
    a("\n\nTCGdex pricing fields in this fetch: tcgplayer present on %d/%d cards (%.1f%%), "
      "cardmarket on %d (%.1f%%), cardmarket avg30 on %d (%.1f%%).\n" % (
          tp["tcgplayerPricing"], tp["cards"], pct(tp["tcgplayerPricing"], tp["cards"]),
          tp["cardmarketPricing"], pct(tp["cardmarketPricing"], tp["cards"]),
          tp["cardmarketAvg30"], pct(tp["cardmarketAvg30"], tp["cards"])))
    im = summary["images"]
    a("## Images\n")
    a("TCGdex image: %d of %d cards. Without one: %d; a TCGdex twin's image %d, fallback from "
      "pokemontcg.io %d, from Scrydex (same pokemontcg.io id) %d, none %d. Cards with some image: %.1f%%. "
      "HEAD checks this run: %d; candidates rejected by the check: %s (Scrydex placeholders: %d); chosen "
      "without a recorded check: %s.\n" % (
          im["tcgdex"], im["cards"], im["withoutTcgdex"], im["tcgdexTwin"], im["fallback"]["ptcgio"],
          im["fallback"]["scrydex"], im["none"], im["coveredPct"], im["headChecked"],
          im["rejectedByHead"] or "none", im["placeholders"], im["chosenUnchecked"] or "none"))
    a("| Set | No TCGdex image | pokemontcg.io set | TCGdex twin | pokemontcg.io | Scrydex | none |")
    a("|---|---:|---|---:|---:|---:|---:|")
    for sid, r in im["setMap"].items():
        a("| %s | %d | %s | %d | %d | %d | %d |" % (sid, r["cards"], r["ptcgioSet"] or "-", r["twin"],
                                                   r["ptcgio"], r["scrydex"], r["none"]))
    sp = summary["species"]
    a("\n## Species\n")
    a("%d species (PokéAPI; by generation %s). Pokémon cards with dx: %d of %d (%d with several species). "
      "Methods: %s. Name rule vs TCGdex dexId: %s. Representative cards: %d species. Species without "
      "any card: %s. species.json %s raw / %s gzip.\n" % (
          sp["species"], sp["byGeneration"], sp["withSpecies"], sp["pokemonCards"], sp["multiSpecies"],
          sp["methods"], sp["nameRuleVsTcgdex"], sp["representative"], sp["speciesWithoutCards"] or "none",
          bundle.human(sp["sizes"]["raw"]), bundle.human(sp["sizes"]["gzip"])))
    for cid, name, dex, ids in sp["corrected"]:
        a("- `%s` %s: TCGdex dexId %s corrected by name to %s" % (cid, name, dex, ids))
    for cid, name, ids in sp["nameOnly"]:
        a("- `%s` %s: no dexId, name rule %s" % (cid, name, ids))
    for cid, name in sp["unmatched"]:
        a("- `%s` %s: **no species**" % (cid, name))
    nm = summary["names"]
    a("\n## Names\n")
    a("Fixed %d card names (overrides.json `names`, then `normalize.clean_card_name`); %d still contain "
      "a non-Latin script and need an override.\n" % (nm["fixed"], nm["unresolved"]))
    for cid, old, new, method in nm["changes"]:
        a("- `%s` %s -> %s (%s)" % (cid, old, new, method))
    a("\nRequests: %s; %ss.\n" % (summary["requests"], summary["seconds"]))
    return "\n".join(L) + "\n"


def print_summary(summary, cov, out):
    o = cov["overall"]
    print("cards %d | productId %.1f%% | market %.1f%% | methods %s" % (
        o["cards"], o["productIdPct"], o["marketPct"], summary["matchMethods"]))
    for e in cov["byEra"]:
        print("  %-45s %6d  pid %5.1f%%  mkt %5.1f%%" % (e["era"], e["cards"], e["productIdPct"],
                                                         e["marketPct"]))
    print("validation", summary["validation"])
    print("REPORT: %s" % os.path.join(out, "REPORT.md"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="never touch the network; cache only")
    ap.add_argument("--sets", help="comma-separated TCGdex set ids (partial dev run)")
    ap.add_argument("--workers", type=int, default=4, help="TCGdex concurrency (default 4)")
    ap.add_argument("--card-ttl-days", type=float, default=14.0,
                    help="re-fetch TCGdex card details older than this (default 14)")
    ap.add_argument("--online-aux", "--online-images", dest="online_aux", action="store_true",
                    help="with --offline: still use the auxiliary sources (pokemontcg.io, image HEAD "
                         "checks, PokéAPI); TCGdex and TCGCSV stay cache-only")
    ap.add_argument("--no-image-check", action="store_true",
                    help="do not HEAD-check fallback image URLs (use cached statuses only)")
    ap.add_argument("--no-tcgdex-fallback", action="store_true",
                    help="do not fill gaps with TCGdex's own TCGplayer ids")
    ap.add_argument("--history-sample-group", type=int, default=23237,
                    help="TCGplayer group used for the history size projection (default: 151)")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache"))
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    args = ap.parse_args(argv)
    build(args)


if __name__ == "__main__":
    main()
