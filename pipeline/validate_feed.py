#!/usr/bin/env python3
"""Validate a built feed directory before it is deployed. Exit 0 = deploy, 1 = don't.

    python3 pipeline/validate_feed.py site [--previous state|URL]
        [--allow-shrink] [--min-cards 15000] [--min-products 15000]

It re-reads exactly the bytes that will be uploaded and checks:

* manifest.json: v=1, date = tcgcsvBuild[:10], every listed file present;
* index.json / prices.json / prices-market.json: sha256 and byte size match the
  manifest, the JSON parses, card/set/product/row counts match the manifest, both
  price files carry the manifest's TCGCSV build and the same products;
* daily/: exactly the files manifest.daily.dates lists, ascending, within retention,
  the newest one byte-identical to prices-market.json, each one's build date = its name;
* history/: exactly manifest.history.sets files, setId = file name, ascending dates
  within retention, every series as long as its dates, at least one file ends today;
* sizes: no file over 100 MB, the site under GitHub Pages' 1 GB;
* against the previous feed (the guards that keep a bad day off the air): build not
  older, products >= 90 % (the app rejects anything less), cards >= 95 %, prices.json
  bytes >= 80 %, and history not lost (days, daily dates and history files carried over);
* first run (no previous feed): floors on cards and products, so a partial catalog
  can never be the feed's first day.

Python 3.9+, standard library only.
"""

import argparse
import gzip
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bundle  # noqa: E402
import net  # noqa: E402
import publish_feed  # noqa: E402

ROOT_FILES = {"manifest.json", "index.json", "prices.json", "prices-market.json", ".nojekyll"}
MAX_FILE_BYTES = 100 * 1000 * 1000
MAX_SITE_BYTES = 1000 * 1000 * 1000


class Report:
    def __init__(self):
        self.errors = []

    def fail(self, msg):
        self.errors.append(msg)

    def expect(self, cond, msg):
        if not cond:
            self.fail(msg)
        return cond


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _json(r, blob, name):
    try:
        return json.loads(blob)
    except ValueError as e:
        r.fail("%s is not valid JSON: %s" % (name, e))
        return None


def validate(site, previous_manifest=None, allow_shrink=False, min_cards=15000, min_products=15000):
    """Returns (errors, stats). ``previous_manifest`` None = first run (bootstrap)."""
    r = Report()
    stats = {"sizes": {}}
    mpath = os.path.join(site, "manifest.json")
    if not r.expect(os.path.isfile(mpath), "manifest.json missing"):
        return r.errors, stats
    m = _json(r, _read(mpath), "manifest.json")
    if not isinstance(m, dict):
        r.fail("manifest.json is not an object")
        return r.errors, stats
    r.expect(m.get("v") == publish_feed.FEED_VERSION, "manifest v=%r, expected 1" % m.get("v"))
    build = m.get("tcgcsvBuild") or ""
    date = m.get("date")
    r.expect(bool(build) and date == build[:10], "manifest date %r does not match tcgcsvBuild %r" % (date, build))

    # ---- latest files: sha256, bytes, counts
    blobs, objs = {}, {}
    for key in ("index", "prices", "pricesMarket"):
        entry = m.get(key) or {}
        rel = entry.get("path")
        if not r.expect(bool(rel) and os.path.isfile(os.path.join(site, rel or "")),
                        "manifest.%s.path %r missing on disk" % (key, rel)):
            continue
        blob = _read(os.path.join(site, rel))
        blobs[key] = blob
        r.expect(entry.get("bytes") == len(blob),
                 "%s: %d bytes, manifest says %r" % (rel, len(blob), entry.get("bytes")))
        r.expect(entry.get("sha256") == hashlib.sha256(blob).hexdigest(), "%s: sha256 does not match the manifest" % rel)
        objs[key] = _json(r, blob, rel)
    index, prices, market = objs.get("index"), objs.get("prices"), objs.get("pricesMarket")
    cards = products = rows = 0
    set_ids = set()
    if isinstance(index, dict):
        cards, sets = len(index.get("cards") or []), index.get("sets") or []
        set_ids = {s.get("id") for s in sets}
        r.expect(index.get("v") == 1, "index.json v=%r" % index.get("v"))
        r.expect(m["index"].get("cards") == cards, "index.json has %d cards, manifest says %r"
                 % (cards, m["index"].get("cards")))
        r.expect(m["index"].get("sets") == len(sets), "index.json has %d sets, manifest says %r"
                 % (len(sets), m["index"].get("sets")))
    if isinstance(prices, dict):
        products, rows = publish_feed.price_counts(prices)
        r.expect(prices.get("tcgcsvBuild") == build, "prices.json build %r != manifest %r"
                 % (prices.get("tcgcsvBuild"), build))
        r.expect(products > 0, "prices.json has no products")
        r.expect(m["prices"].get("products") == products, "prices.json has %d products, manifest says %r"
                 % (products, m["prices"].get("products")))
        r.expect(m["prices"].get("rows") == rows, "prices.json has %d rows, manifest says %r"
                 % (rows, m["prices"].get("rows")))
    if isinstance(market, dict) and isinstance(prices, dict):
        r.expect(market.get("tcgcsvBuild") == build, "prices-market.json build %r != manifest %r"
                 % (market.get("tcgcsvBuild"), build))
        r.expect(set(market.get("p") or {}) == set(prices.get("p") or {}),
                 "prices-market.json and prices.json list different products")
    stats.update(cards=cards, products=products, rows=rows)

    # ---- daily/
    daily = m.get("daily") or {}
    dates = daily.get("dates") or []
    r.expect(dates == sorted(set(dates)), "daily.dates not ascending/unique")
    r.expect(len(dates) <= (daily.get("retentionDays") or 0), "daily.dates exceeds retention")
    r.expect(bool(dates) and dates[-1] == date, "newest daily date %r != manifest date %r"
             % (dates[-1] if dates else None, date))
    ddir = os.path.join(site, "daily")
    on_disk = sorted(f[:-5] for f in os.listdir(ddir) if f.endswith(".json")) if os.path.isdir(ddir) else []
    r.expect(on_disk == sorted(dates), "daily/ files %s != manifest dates %s" % (on_disk[-3:], dates[-3:]))
    for d in dates:
        p = os.path.join(ddir, d + ".json")
        if not os.path.isfile(p):
            continue
        blob = _read(p)
        if d == date and "pricesMarket" in blobs:
            r.expect(blob == blobs["pricesMarket"], "daily/%s.json differs from prices-market.json" % d)
        obj = _json(r, blob, "daily/%s.json" % d)
        if isinstance(obj, dict):
            r.expect((obj.get("tcgcsvBuild") or "")[:10] == d, "daily/%s.json holds build %r"
                     % (d, obj.get("tcgcsvBuild")))

    # ---- history/
    hist = m.get("history") or {}
    hdir = os.path.join(site, "history")
    hfiles = sorted(f for f in os.listdir(hdir) if f.endswith(".json")) if os.path.isdir(hdir) else []
    r.expect(len(hfiles) == hist.get("sets"), "history/ has %d files, manifest says %r"
             % (len(hfiles), hist.get("sets")))
    retention = hist.get("retentionDays") or 0
    max_days, first, ends_today = 0, None, 0
    for f in hfiles:
        sid = f[:-5]
        h = _json(r, _read(os.path.join(hdir, f)), "history/" + f)
        if not isinstance(h, dict):
            continue
        hd = h.get("dates") or []
        bad = (h.get("setId") != sid or hd != sorted(set(hd)) or not hd or len(hd) > retention
               or hd[-1] > (date or "") or (set_ids and sid not in set_ids))
        n_sub = len(h.get("subTypes") or [])
        bad_series = [k for k, v in (h.get("s") or {}).items()
                      if len(v) != len(hd) or int(k.split(".")[1]) >= n_sub]
        r.expect(not bad, "history/%s: bad setId/dates/retention or unknown set" % f)
        r.expect(not bad_series, "history/%s: %d series with a wrong length or subtype" % (f, len(bad_series)))
        r.expect(bool(h.get("s")), "history/%s has no series" % f)
        max_days = max(max_days, len(hd))
        if hd:
            first = hd[0] if first is None else min(first, hd[0])
            ends_today += hd[-1] == date
    if hfiles:
        r.expect(hist.get("days") == max_days, "history.days %r != longest file %d" % (hist.get("days"), max_days))
        r.expect(hist.get("from") == first, "history.from %r != oldest date %r" % (hist.get("from"), first))
        r.expect(ends_today > 0, "no history file has today's column (%s)" % date)
    stats["historyDays"] = max_days

    # ---- everything else at the root, sizes
    for name in os.listdir(site):
        path = os.path.join(site, name)
        if os.path.isdir(path):
            r.expect(name in ("daily", "history"), "unexpected directory %s/" % name)
        else:
            r.expect(name in ROOT_FILES, "unexpected file %s" % name)
    r.expect(os.path.isfile(os.path.join(site, ".nojekyll")), ".nojekyll missing")
    total = 0
    for dirpath, _, names in os.walk(site):
        r.expect(os.stat(dirpath).st_mode & 0o005 == 0o005,
                 "%s/ is not world-readable (GitHub Pages needs 755 directories)" % os.path.relpath(dirpath, site))
        for n in names:
            r.expect(os.stat(os.path.join(dirpath, n)).st_mode & 0o004,
                     "%s is not world-readable (GitHub Pages needs 644 files)" % n)
            p = os.path.join(dirpath, n)
            size = os.path.getsize(p)
            total += size
            r.expect(size <= MAX_FILE_BYTES, "%s is %d bytes (> 100 MB)" % (os.path.relpath(p, site), size))
            rel = os.path.relpath(p, site)
            bucket = rel.split(os.sep)[0] if os.sep in rel else rel
            s = stats["sizes"].setdefault(bucket, {"files": 0, "raw": 0, "gzip": 0})
            s["files"] += 1
            s["raw"] += size
            if size:
                s["gzip"] += len(gzip.compress(_read(p), 6))
    r.expect(total <= MAX_SITE_BYTES, "site is %d bytes (> GitHub Pages' 1 GB)" % total)
    stats["total"] = {"files": sum(s["files"] for s in stats["sizes"].values()), "raw": total,
                      "gzip": sum(s["gzip"] for s in stats["sizes"].values())}

    # ---- against the previous feed
    if previous_manifest is None:
        r.expect(cards >= min_cards, "first run: only %d cards (floor %d)" % (cards, min_cards))
        r.expect(products >= min_products, "first run: only %d products (floor %d)" % (products, min_products))
        r.expect(len(dates) == 1 and max_days == 1, "first run: expected exactly one day of history and daily data")
    else:
        prev_build = previous_manifest.get("tcgcsvBuild")
        if prev_build and build:
            r.expect(publish_feed.parse_stamp(build) >= publish_feed.parse_stamp(prev_build),
                     "build %s is older than the previous feed's %s" % (build, prev_build))
        if not allow_shrink:
            for problem in publish_feed.shrink_problems(previous_manifest, cards, products,
                                                        len(blobs.get("prices", b""))):
                r.fail(problem)
            ph = previous_manifest.get("history") or {}
            prev_days = ph.get("days") or 0
            r.expect(max_days >= min(prev_days, retention),
                     "history shrank from %d to %d days (state lost?)" % (prev_days, max_days))
            prev_sets = ph.get("sets") or 0
            r.expect(len(hfiles) >= prev_sets - max(2, 0.02 * prev_sets),
                     "history files dropped from %d to %d" % (prev_sets, len(hfiles)))
            prev_dates = ((previous_manifest.get("daily") or {}).get("dates") or [])
            older = [d for d in prev_dates if d < (date or "")]
            room = max(0, (daily.get("retentionDays") or 0) - 1)   # today takes one slot
            kept = older[len(older) - room:] if room else []
            lost = sorted(set(kept) - set(dates))
            r.expect(not lost, "daily snapshots lost: %s" % lost[:5])
    return r.errors, stats


def print_stats(stats):
    print("validated: %d cards, %d products, %d price rows, history up to %d day(s)"
          % (stats.get("cards", 0), stats.get("products", 0), stats.get("rows", 0), stats.get("historyDays", 0)))
    for name in sorted(stats["sizes"]):
        s = stats["sizes"][name]
        print("  %-20s %4d file(s)  raw %10s  gzip %10s"
              % (name, s["files"], bundle.human(s["raw"]), bundle.human(s["gzip"])))
    t = stats.get("total")
    if t:
        print("  %-20s %4d file(s)  raw %10s  gzip %10s"
              % ("TOTAL", t["files"], bundle.human(t["raw"]), bundle.human(t["gzip"])))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("site")
    ap.add_argument("--previous", help="previous feed: URL or directory; empty or omitted = first run")
    ap.add_argument("--previous-url")
    ap.add_argument("--previous-dir")
    ap.add_argument("--allow-shrink", action="store_true")
    ap.add_argument("--min-cards", type=int, default=15000, help="first-run floor (default 15000)")
    ap.add_argument("--min-products", type=int, default=15000, help="first-run floor (default 15000)")
    args = ap.parse_args(argv)
    try:
        previous = publish_feed.previous_state(args).manifest
    except (publish_feed.PublishError, net.FetchError, ValueError) as e:
        sys.stderr.write("[validate_feed] error: cannot read the previous feed: %s\n" % e)
        return 1
    errors, stats = validate(args.site, previous, args.allow_shrink, args.min_cards, args.min_products)
    print_stats(stats)
    if errors:
        for e in errors:
            sys.stderr.write("[validate_feed] FAIL %s\n" % e)
        sys.stderr.write("[validate_feed] %d problem(s): not deploying\n" % len(errors))
        return 1
    print("[validate_feed] OK (%s)" % ("first run" if previous is None else "against the previous feed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
