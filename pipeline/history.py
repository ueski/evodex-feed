"""Price-history prototype.

The TCGCSV daily price archive (https://tcgcsv.com/archive/tcgplayer/
prices-YYYY-MM-DD.ppmd.7z, Feb 2024 onwards) answers HTTP 403 with a notice
that it "has been temporarily removed due to rising server costs" (checked
2026-10-22). So history cannot be back-filled from TCGCSV today; it has to be
accumulated going forward from our own daily snapshots.

Every pipeline run leaves the day's raw prices in
cache/tcgcsv/<build-stamp>/prices/<groupId>.json. This module folds all the
builds it finds into one compact, append-only file per TCGplayer group:

    out/history/<groupId>.json
    {"v": 1, "groupId": 23237, "dates": ["2026-10-21", ...],
     "subTypes": ["Normal", ...],
     "s": {"<productId>.<subTypeIdx>": [marketCents | null per date]}}

`project_sizes` estimates what 90/365 days will cost by simulating day-to-day
movement on top of today's real snapshot (clearly labelled as a projection).
"""

import glob
import gzip
import json
import os
import random

import bundle
from variants import SUBTYPES


def builds(cache_dir):
    """[(stamp, prices_dir)] sorted oldest first."""
    out = []
    for d in glob.glob(os.path.join(cache_dir, "tcgcsv", "*", "prices")):
        stamp = os.path.basename(os.path.dirname(d))
        out.append((stamp, d))
    return sorted(out)


def build_group_history(cache_dir, group_id):
    subtypes = list(SUBTYPES)
    s_idx = {s: i for i, s in enumerate(subtypes)}
    dates, series = [], {}
    for n, (stamp, d) in enumerate(builds(cache_dir)):
        path = os.path.join(d, "%d.json" % group_id)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            rows = (json.load(f) or {}).get("results", [])
        dates.append(stamp[:10])
        col = len(dates) - 1
        for r in rows:
            sub = r["subTypeName"]
            if sub not in s_idx:
                s_idx[sub] = len(subtypes)
                subtypes.append(sub)
            key = "%d.%d" % (r["productId"], s_idx[sub])
            arr = series.setdefault(key, [None] * col)
            arr.extend([None] * (col - len(arr)))
            arr.append(bundle.cents(r.get("marketPrice")))
    for arr in series.values():
        arr.extend([None] * (len(dates) - len(arr)))
    return {"v": 1, "groupId": group_id, "dates": dates, "subTypes": subtypes, "s": series}


def write_all(cache_dir, out_dir, group_ids):
    os.makedirs(out_dir, exist_ok=True)
    total_raw = total_gz = 0
    n_dates = 0
    for gid in group_ids:
        h = build_group_history(cache_dir, gid)
        if not h["dates"]:
            continue
        blob = bundle.dumps_compact(h)
        with open(os.path.join(out_dir, "%d.json" % gid), "wb") as f:
            f.write(blob)
        total_raw += len(blob)
        total_gz += len(gzip.compress(blob, 9))
        n_dates = max(n_dates, len(h["dates"]))
    return {"groups": len(group_ids), "days": n_dates, "raw": total_raw, "gzip": total_gz}


def _simulate(today, days, rng, p_change=0.35, max_step=0.04):
    """Random-walk projection: each day a series moves with probability
    p_change by up to +-max_step (relative). Real data is smoother for bulk
    commons and noisier for chase cards; this is an order-of-magnitude tool."""
    out = {}
    for key, v in today.items():
        cur = v
        arr = []
        for _ in range(days):
            if cur is not None and rng.random() < p_change:
                cur = max(1, int(round(cur * (1 + rng.uniform(-max_step, max_step)))))
            arr.append(cur)
        out[key] = arr
    return out


def project_sizes(group_history, days_list=(90, 365), seed=7):
    """Size of one group's history file for N days (raw JSON / gzip), plus
    the per-series cost, using today's real series as the starting point."""
    rng = random.Random(seed)
    today = {k: v[-1] for k, v in group_history["s"].items()}
    res = {"series": len(today)}
    for days in days_list:
        sim = {"v": 1, "groupId": group_history["groupId"],
               "dates": ["2026-01-01"] * days, "subTypes": group_history["subTypes"],
               "s": _simulate(today, days, rng)}
        blob = bundle.dumps_compact(sim)
        res["%dd" % days] = {"raw": len(blob), "gzip": len(gzip.compress(blob, 9))}
    return res


# ---- Incremental fold (feed publishing, see publish_feed.py) ---------------
#
# The published feed keeps one history file per TCGdex set:
#
#     history/<setId>.json
#     {"v": 1, "setId": "base1", "dates": ["2026-10-21", ...],
#      "subTypes": ["Normal", ...],
#      "s": {"<productId>.<subTypeIdx>": [marketCents | null per date]}}
#
# Same shape as the per-group files above, keyed by set because the app looks
# history up by the card's set. The publisher folds one day's snapshot into
# the previous file with `append_column`; the deployed feed itself is the state
# between runs, so these helpers are pure and never touch the TCGCSV cache.


def empty_history(set_id, subtypes=()):
    return {"v": 1, "setId": set_id, "dates": [], "subTypes": list(subtypes), "s": {}}


def snapshot_values_by_set(index, prices):
    """{setId: {(productId, subTypeName): marketCents | None}} for one day.

    Every (product, printing) a set's cards reference in index.json is listed,
    with today's market price from prices.json (looked up by printing NAME,
    because index.json and prices.json each carry their own subTypes table).
    Cards without products contribute nothing.
    """
    price_names = prices.get("subTypes", [])
    market = {}
    for pid, rows in prices.get("p", {}).items():
        for row in rows:
            if row and 0 <= row[0] < len(price_names):
                market[(int(pid), price_names[row[0]])] = row[1] if len(row) > 1 else None
    index_names = index.get("subTypes", [])
    out = {}
    for card in index.get("cards", []):
        products = card.get("p")
        if not products:
            continue
        values = out.setdefault(card["s"], {})
        for p in products:
            for st in p.get("st", []):
                key = (int(p["id"]), index_names[st])
                values[key] = market.get(key)
    return out


def append_column(hist, date, values, retention_days=365):
    """Fold one day into a history dict. Pure: returns (new_hist, status).

    ``values`` maps (productId, subTypeName) -> marketCents | None.
    status is "appended", "replaced" (same date re-run: the column is rebuilt)
    or "skipped" (``date`` is older than the file's last date; nothing changes).
    New series are back-filled with null, series missing today get null, the
    subTypes table is a union (new names appended, indexes remapped by name),
    only the last ``retention_days`` dates are kept, and series that are
    entirely null afterwards are dropped.
    """
    dates = list(hist.get("dates", []))
    if dates and date < dates[-1]:
        return hist, "skipped"
    subtypes = list(hist.get("subTypes", []))
    st_idx = {name: i for i, name in enumerate(subtypes)}
    series = {k: list(v) for k, v in hist.get("s", {}).items()}

    if dates and date == dates[-1]:
        status, col = "replaced", len(dates) - 1
        for arr in series.values():
            arr[col] = None
    else:
        status, col = "appended", len(dates)
        dates.append(date)
        for arr in series.values():
            arr.append(None)

    for (pid, name), cents in values.items():
        if name not in st_idx:
            st_idx[name] = len(subtypes)
            subtypes.append(name)
        key = "%d.%d" % (pid, st_idx[name])
        arr = series.get(key)
        if arr is None:
            arr = series[key] = [None] * len(dates)
        arr[col] = cents

    if retention_days and len(dates) > retention_days:
        cut = len(dates) - retention_days
        dates = dates[cut:]
        series = {k: v[cut:] for k, v in series.items()}
    series = {k: v for k, v in series.items() if any(x is not None for x in v)}

    def order(key):
        pid, st = key.split(".")
        return int(pid), int(st)

    out = dict(hist)
    out.update({"v": 1, "dates": dates, "subTypes": subtypes,
                "s": {k: series[k] for k in sorted(series, key=order)}})
    return out, status


def insert_column(hist, date, values, retention_days=365):
    """Fold a PAST day into a history dict (a one-time seed, see publish_feed.py
    "history seeds"). Pure: returns (new_hist, status).

    status is "inserted" (the column now sits at its sorted position), "present"
    (the file already has ``date``: real data always wins, nothing changes),
    "empty" (``values`` has no price: no column is added, so no date without
    data appears) or "skipped" (``date`` is newer than the file's last date, which
    is append_column's job, or the file already keeps ``retention_days`` dates
    that are all newer). Like append_column: new series are back-filled with null,
    the subTypes table only grows, all-null series are dropped. Dates are never
    filled in between: a gap stays a gap.
    """
    dates = list(hist.get("dates", []))
    if date in dates:
        return hist, "present"
    if not any(v is not None for v in values.values()):
        return hist, "empty"
    if dates and date > dates[-1]:
        return hist, "skipped"
    if retention_days and len(dates) >= retention_days and (not dates or date < dates[0]):
        return hist, "skipped"
    subtypes = list(hist.get("subTypes", []))
    st_idx = {name: i for i, name in enumerate(subtypes)}
    col = sum(1 for d in dates if d < date)
    dates.insert(col, date)
    series = {k: v[:col] + [None] + v[col:] for k, v in hist.get("s", {}).items()}
    for (pid, name), cents in values.items():
        if cents is None:
            continue
        if name not in st_idx:
            st_idx[name] = len(subtypes)
            subtypes.append(name)
        key = "%d.%d" % (pid, st_idx[name])
        arr = series.get(key)
        if arr is None:
            arr = series[key] = [None] * len(dates)
        arr[col] = cents
    if retention_days and len(dates) > retention_days:
        cut = len(dates) - retention_days
        dates = dates[cut:]
        series = {k: v[cut:] for k, v in series.items()}
    series = {k: v for k, v in series.items() if any(x is not None for x in v)}

    def order(key):
        pid, st = key.split(".")
        return int(pid), int(st)

    out = dict(hist)
    out.update({"v": 1, "dates": dates, "subTypes": subtypes,
                "s": {k: series[k] for k in sorted(series, key=order)}})
    return out, "inserted"
