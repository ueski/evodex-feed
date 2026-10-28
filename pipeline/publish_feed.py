#!/usr/bin/env python3
"""Build the static price feed the Binderly app reads, and decide whether a
new build is due. Layout and client protocol: docs/architecture/price-feed.md.

    # Is there a TCGCSV build newer than the live feed? (writes changed=true|false
    # to $GITHUB_OUTPUT when set)
    python3 tools/pipeline/publish_feed.py check --previous-url https://<user>.github.io/<repo>/ [--force]

    # Fold today's bundle (tools/pipeline/out/bundle, written by run.py) into the
    # previous feed and write a complete deployable directory
    python3 tools/pipeline/publish_feed.py build --bundle tools/pipeline/out/bundle --out feed \\
        [--previous-url URL | --previous-dir DIR] [--history-retention 365] [--daily-retention 90]

The previous state is the currently deployed feed itself: the history files and
daily snapshots are read back from it, today's column is appended, and the whole
site is written again. A 404 for manifest.json means "no feed yet" (bootstrap);
any other network or HTTP error aborts before anything is written, so a flaky
fetch can never publish a feed with its history wiped.

Python 3.9+, standard library only.
"""

import argparse
import datetime
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bundle  # noqa: E402
import history  # noqa: E402
import net  # noqa: E402
import sources  # noqa: E402

FEED_VERSION = 1
PUBLISHER_UA = "binderly-feed-publisher/1 (+github actions)"
HISTORY_TEMPLATE = "history/{setId}.json"
DAILY_TEMPLATE = "daily/{date}.json"


class PublishError(Exception):
    pass


def log(msg):
    sys.stderr.write("[publish_feed] %s\n" % msg)


def sha256(blob):
    return hashlib.sha256(blob).hexdigest()


def parse_stamp(stamp):
    """TCGCSV build stamp ("2026-10-21T20:05:22+0000") -> aware datetime."""
    s = stamp.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.datetime.strptime(s.replace("Z", "+0000"), fmt)
        except ValueError:
            pass
    raise PublishError("unparseable build stamp %r" % stamp)


# ---- previous state ---------------------------------------------------------

class NoPrevious:
    """No deployed feed yet: bootstrap."""
    manifest = None

    def history(self, set_ids):
        return {}

    def daily(self, date):
        return None


class DirPrevious:
    """A previous feed on disk (same layout as the site)."""

    def __init__(self, root):
        self.root = root
        path = os.path.join(root, "manifest.json")
        self.manifest = _load_json_file(path) if os.path.exists(path) else None

    def history(self, set_ids):
        out = {}
        if self.manifest is None:
            return out
        for sid in set_ids:
            path = os.path.join(self.root, HISTORY_TEMPLATE.format(setId=sid))
            if os.path.exists(path):
                out[sid] = _load_json_file(path)
        return out

    def daily(self, date):
        path = os.path.join(self.root, DAILY_TEMPLATE.format(date=date))
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()


class UrlPrevious:
    """The live feed over HTTPS. 404 = absent; every other failure raises."""

    def __init__(self, base_url, workers=8):
        self.base = base_url if base_url.endswith("/") else base_url + "/"
        self.workers = workers
        body = self._get("manifest.json")
        self.manifest = None if body is None else json.loads(body)
        if self.manifest is None:
            log("no live feed at %smanifest.json (404)" % self.base)

    def _get(self, rel):
        url = self.base + urllib.parse.quote(rel)
        try:
            return net.http_get(url, retries=3, headers={"User-Agent": PUBLISHER_UA})
        except net.NotFound:
            return None
        except net.FetchError as e:
            raise PublishError("cannot read previous feed: %s" % e)

    def history(self, set_ids):
        if self.manifest is None:
            return {}

        def one(sid):
            body = self._get(HISTORY_TEMPLATE.format(setId=sid))
            return sid, (None if body is None else json.loads(body))

        out = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for sid, h in pool.map(one, set_ids):
                if h is not None:
                    out[sid] = h
        return out

    def daily(self, date):
        return self._get(DAILY_TEMPLATE.format(date=date))


def _load_json_file(path):
    with open(path, "rb") as f:
        return json.loads(f.read())


def previous_state(args):
    if args.previous_url and args.previous_dir:
        raise PublishError("use --previous-url or --previous-dir, not both")
    if args.previous_url:
        return UrlPrevious(args.previous_url)
    if args.previous_dir:
        return DirPrevious(args.previous_dir)
    return NoPrevious()


# ---- build --------------------------------------------------------------------

def read_bundle(bundle_dir):
    def raw(name):
        path = os.path.join(bundle_dir, name)
        if not os.path.exists(path):
            raise PublishError("missing %s (run tools/pipeline/run.py first)" % path)
        with open(path, "rb") as f:
            return f.read()

    b = {"manifest": json.loads(raw("manifest.json")), "index": raw("index.json"),
         "prices": raw("prices.json"), "pricesMarket": raw("prices-market.json")}
    m = b["manifest"]
    for key, blob in (("index", b["index"]), ("prices", b["prices"]), ("pricesMarket", b["pricesMarket"])):
        want = (m.get(key) or {}).get("sha256")
        if want and want != sha256(blob):
            raise PublishError("bundle %s does not match its manifest sha256 (stale or partial run?)" % key)
    return b


def check_not_regressing(prev_manifest, today_build, index_obj, prices_blob, allow_shrink):
    if not prev_manifest:
        return
    if prev_manifest.get("v") != FEED_VERSION:
        raise PublishError("previous feed has manifest v=%r; this publisher writes v=%d"
                           % (prev_manifest.get("v"), FEED_VERSION))
    prev_build = prev_manifest.get("tcgcsvBuild")
    if prev_build and parse_stamp(today_build) < parse_stamp(prev_build):
        raise PublishError("bundle build %s is older than the live feed's %s" % (today_build, prev_build))
    if allow_shrink:
        return
    prev_cards = (prev_manifest.get("index") or {}).get("cards") or 0
    if prev_cards and len(index_obj.get("cards", [])) < 0.95 * prev_cards:
        raise PublishError("catalog shrank from %d to %d cards (partial pipeline run?); "
                           "pass --allow-shrink if intended" % (prev_cards, len(index_obj.get("cards", []))))
    prev_bytes = (prev_manifest.get("prices") or {}).get("bytes") or 0
    if prev_bytes and len(prices_blob) < 0.8 * prev_bytes:
        raise PublishError("prices.json shrank from %d to %d bytes; pass --allow-shrink if intended"
                           % (prev_bytes, len(prices_blob)))


def build_feed(bundle_dir, out_dir, previous, history_retention=365, daily_retention=90,
               allow_shrink=False):
    """Write a complete feed directory. Returns a summary dict."""
    b = read_bundle(bundle_dir)
    bm = b["manifest"]
    build_stamp = bm["tcgcsvBuild"]
    date = bm.get("date") or build_stamp[:10]
    index_obj = json.loads(b["index"])
    prices_obj = json.loads(b["prices"])
    prev_manifest = previous.manifest
    if prev_manifest is None:
        log("no previous feed: bootstrapping (history starts at %s)" % date)
    check_not_regressing(prev_manifest, build_stamp, index_obj, b["prices"], allow_shrink)

    # history/<setId>.json
    set_ids = [s["id"] for s in index_obj.get("sets", [])]
    prev_history = previous.history(set_ids)
    today = history.snapshot_values_by_set(index_obj, prices_obj)
    files = {}  # relative path -> bytes
    statuses = {}
    hist_from, hist_days = None, 0
    for sid in set_ids:
        values = today.get(sid, {})
        prev = prev_history.get(sid) or history.empty_history(sid, index_obj.get("subTypes", []))
        if not values and not prev.get("s"):
            continue
        h, status = history.append_column(prev, date, values, history_retention)
        h["setId"] = sid
        statuses[status] = statuses.get(status, 0) + 1
        if not h["s"]:
            continue
        files[HISTORY_TEMPLATE.format(setId=sid)] = bundle.dumps_compact(h)
        hist_days = max(hist_days, len(h["dates"]))
        if h["dates"] and (hist_from is None or h["dates"][0] < hist_from):
            hist_from = h["dates"][0]
    n_history = len(files)

    # daily/<date>.json: today's market snapshot plus the retained previous days
    prev_dates = list(((prev_manifest or {}).get("daily") or {}).get("dates") or [])
    dates = sorted(set(d for d in prev_dates if d < date) | {date})[-daily_retention:]
    kept = []
    for d in dates:
        blob = b["pricesMarket"] if d == date else previous.daily(d)
        if blob is None:
            log("warning: daily/%s.json listed in the previous manifest but missing; dropped" % d)
            continue
        files[DAILY_TEMPLATE.format(date=d)] = blob
        kept.append(d)

    # latest files, byte-for-byte copies of the bundle
    files["index.json"] = b["index"]
    files["prices.json"] = b["prices"]
    files["prices-market.json"] = b["pricesMarket"]

    manifest = {k: v for k, v in bm.items() if k not in ("history", "daily")}
    manifest["v"] = FEED_VERSION
    manifest["date"] = date
    manifest["index"] = dict(bm.get("index") or {}, path="index.json", sha256=sha256(b["index"]),
                             bytes=len(b["index"]), cards=len(index_obj.get("cards", [])),
                             sets=len(index_obj.get("sets", [])))
    manifest["prices"] = {"path": "prices.json", "sha256": sha256(b["prices"]), "bytes": len(b["prices"])}
    manifest["pricesMarket"] = {"path": "prices-market.json", "sha256": sha256(b["pricesMarket"]),
                                "bytes": len(b["pricesMarket"])}
    manifest["history"] = {"pathTemplate": HISTORY_TEMPLATE, "from": hist_from, "to": date if n_history else None,
                           "days": hist_days, "retentionDays": history_retention, "sets": n_history}
    manifest["daily"] = {"pathTemplate": DAILY_TEMPLATE, "dates": kept, "retentionDays": daily_retention}
    files["manifest.json"] = bundle.dumps_compact(manifest)
    files[".nojekyll"] = b""

    write_tree(out_dir, files)
    return summarize(files, manifest, statuses)


def write_tree(out_dir, files):
    """Write everything into a sibling temp dir, then swap it into place."""
    out_dir = os.path.abspath(out_dir)
    parent = os.path.dirname(out_dir)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".feed-", dir=parent)
    try:
        for rel, blob in files.items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(blob)
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)
        os.rename(tmp, out_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def summarize(files, manifest, statuses):
    def bucket(rel):
        if rel.startswith("history/"):
            return "history"
        if rel.startswith("daily/"):
            return "daily"
        return rel

    sizes = {}
    for rel, blob in files.items():
        s = sizes.setdefault(bucket(rel), {"files": 0, "raw": 0, "gzip": 0})
        s["files"] += 1
        s["raw"] += len(blob)
        s["gzip"] += len(gzip.compress(blob, 6)) if blob else 0
    total = {"files": sum(s["files"] for s in sizes.values()),
             "raw": sum(s["raw"] for s in sizes.values()),
             "gzip": sum(s["gzip"] for s in sizes.values())}
    return {"date": manifest["date"], "tcgcsvBuild": manifest["tcgcsvBuild"],
            "historyStatus": statuses, "historyDays": manifest["history"]["days"],
            "dailyDates": len(manifest["daily"]["dates"]), "sizes": sizes, "total": total}


def print_summary(summary):
    print("feed %s (TCGCSV build %s)" % (summary["date"], summary["tcgcsvBuild"]))
    print("  history: %s, up to %d day(s); daily snapshots: %d"
          % (summary["historyStatus"], summary["historyDays"], summary["dailyDates"]))
    for name in sorted(summary["sizes"]):
        s = summary["sizes"][name]
        print("  %-20s %4d file(s)  raw %10s  gzip %10s"
              % (name, s["files"], bundle.human(s["raw"]), bundle.human(s["gzip"])))
    t = summary["total"]
    print("  %-20s %4d file(s)  raw %10s  gzip %10s"
          % ("TOTAL", t["files"], bundle.human(t["raw"]), bundle.human(t["gzip"])))


# ---- check -------------------------------------------------------------------

def tcgcsv_last_updated():
    body = net.http_get(sources.TCGCSV_BASE + "/last-updated.txt", retries=3)
    return body.decode("utf-8").strip()


def check(previous_url, force=False, tcgcsv_stamp=None):
    """Returns (changed, reason, tcgcsv_stamp)."""
    stamp = tcgcsv_stamp if tcgcsv_stamp is not None else tcgcsv_last_updated()
    live = UrlPrevious(previous_url).manifest
    if force:
        return True, "forced", stamp
    if live is None:
        return True, "no live feed yet", stamp
    live_stamp = live.get("tcgcsvBuild")
    if not live_stamp or parse_stamp(stamp) != parse_stamp(live_stamp):
        return True, "TCGCSV build %s differs from the live feed's %s" % (stamp, live_stamp), stamp
    return False, "live feed already has TCGCSV build %s" % stamp, stamp


def write_github_output(pairs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in pairs.items():
            f.write("%s=%s\n" % (k, v))


# ---- CLI ---------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    b = sub.add_parser("build", help="write a deployable feed directory")
    b.add_argument("--bundle", default=os.path.join(HERE, "out", "bundle"))
    b.add_argument("--out", required=True)
    b.add_argument("--previous-url")
    b.add_argument("--previous-dir")
    b.add_argument("--history-retention", type=int, default=365, help="dates kept per history file")
    b.add_argument("--daily-retention", type=int, default=90, help="daily snapshots kept")
    b.add_argument("--allow-shrink", action="store_true",
                   help="publish even if the catalog or price file shrank sharply")

    c = sub.add_parser("check", help="is a new TCGCSV build available?")
    c.add_argument("--previous-url", required=True)
    c.add_argument("--force", action="store_true")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "build":
            summary = build_feed(args.bundle, args.out, previous_state(args),
                                 args.history_retention, args.daily_retention, args.allow_shrink)
            print_summary(summary)
            return 0
        if args.cmd == "check":
            changed, reason, stamp = check(args.previous_url, args.force)
            print("changed=%s (%s)" % ("true" if changed else "false", reason))
            write_github_output({"changed": "true" if changed else "false", "tcgcsvBuild": stamp})
            return 0
        ap.print_help()
        return 2
    except (PublishError, net.FetchError, ValueError) as e:
        log("error: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
