#!/usr/bin/env python3
"""Build the static price feed the Evodex app reads, and decide whether a
new build is due. Layout: evodex-feed README.md "Feed layout"; client protocol:
docs/architecture/price-feed.md in the Evodex app repository. Paths below are
evodex-feed's (pipeline/); the app repository keeps the same file in tools/pipeline/.

    # Is there a TCGCSV build newer than the previous feed? (writes
    # changed=true|false to $GITHUB_OUTPUT when set). --wait-minutes polls
    # TCGCSV's last-updated.txt while today's build has not appeared yet.
    python3 pipeline/publish_feed.py check [--previous DIR|URL] [--force] [--wait-minutes 120 --poll-minutes 15]

    # Fold today's bundle (pipeline/out/bundle, written by run.py) into the
    # previous feed and write a complete deployable directory
    python3 pipeline/publish_feed.py build --bundle pipeline/out/bundle --out site \\
        [--previous DIR|URL] [--history-retention 365] [--daily-retention 90] [--seed-dir DIR]

    # Add every card of an index.json (e.g. a shipped app bundle) to catalog-floor.json
    python3 pipeline/publish_feed.py floor --add path/to/index.json --label "app 0.3.0 bundle"

The previous state is a feed directory with the same layout (the `data` branch
checked out by scripts/state.sh, see README.md "History and state"), or a live
feed over HTTPS (--previous takes either; --previous-dir / --previous-url are the
explicit forms). Its history files and daily snapshots are read back, today's
column is appended, and the whole site is written again. Without any of them
there is no previous feed (bootstrap). Over HTTPS a 404 for manifest.json means
"no feed yet"; any other network or HTTP error aborts before anything is
written, so a flaky fetch can never publish a feed with its history wiped.

Cards are only ever added. Every card of the previous feed's index.json and of
catalog-floor.json (every catalog an app version ships with) must still be in
today's index.json, or the build stops; --allow-shrink does not override this.
A card that must really go is listed in overrides.json "removedCards".

Affiliate links (links.json next to this file, evodex-feed only): {"tcgplayerAffiliate": null}
is the switch for the app's TCGplayer affiliate links. A string is written to the manifest as
links.tcgplayerAffiliate (checked like the app does: https, "{url}" exactly once, host
tcgplayer.pxf.io / www.tcgplayer.com / tcgplayer.com); "" is written too and means "off"; null
or no file writes no links block (the app's default: plain links). An invalid value stops the build.

TCGplayer images (images.json next to this file, evodex-feed only; images.load_config):
{"tcgplayerFallback": true} is the kill switch for TCGplayer product photos as the last-resort fallback
image. The manifest gets images.tcgplayerFallback (true or false) whenever the file exists; the app shows
tcgplayer-cdn.tcgplayer.com images only while the last good manifest says true, so false (or no file)
switches them off in every app at its next feed check. The build stops when index.json carries a
TCGplayer image the switch doesn't allow (images.tcgplayer_image_problems).

History seeds (history-seed/<YYYY-MM-DD>.json, a prices-market.json of an
earlier TCGCSV build we downloaded ourselves): each build folds a seed into
every history file and daily/ that lacks that date (history.insert_column), so a
day the feed never published still shows on the charts. Once the date is in the
state it is "present" and the seed is a no-op; dates are never interpolated.

Python 3.9+, standard library only.
"""

import argparse
import datetime
import gzip
import hashlib
import json
import os
import re
import shutil
import string
import sys
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bundle  # noqa: E402
import history  # noqa: E402
import images  # noqa: E402
import net  # noqa: E402
import same_art  # noqa: E402
import sources  # noqa: E402

FEED_VERSION = 1
PUBLISHER_UA = "EvodexFeedPublisher/1.0 (+https://github.com/ueski/evodex-feed)"
HISTORY_TEMPLATE = "history/{setId}.json"
DAILY_TEMPLATE = "daily/{date}.json"
# Mirrors the app's guard (PriceStore: a snapshot with < 90 % of the active snapshot's
# products is rejected), so the feed never publishes prices every app would refuse.
MIN_PRODUCTS_RATIO = 0.90
MIN_CARDS_RATIO = 0.95
MIN_PRICES_BYTES_RATIO = 0.80
CATALOG_FLOOR = os.path.join(HERE, "catalog-floor.json")
OVERRIDES = os.path.join(HERE, "overrides.json")
SEED_DIR = os.path.join(HERE, "history-seed")
LINKS = os.path.join(HERE, "links.json")
# The app's TCGplayerAffiliate rules (Binderly/Core/Links/TCGplayerAffiliate.swift); keep both in step.
AFFILIATE_HOSTS = frozenset(("tcgplayer.pxf.io", "www.tcgplayer.com", "tcgplayer.com"))
AFFILIATE_PLACEHOLDER = "{url}"
AFFILIATE_MAX_TEMPLATE = 1024
AFFILIATE_MAX_URL = 4096
AFFILIATE_SAMPLE = "https://www.tcgplayer.com/product/42382"
# Characters a URL may contain as written (RFC 3986): no space, quote, backslash, braces or non-ASCII.
_URL_CHARS = frozenset(string.ascii_letters + string.digits + "-._~:/?#[]@!$&'()*+,;=%")


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

    def index(self):
        return None


class DirPrevious:
    """A previous feed on disk (same layout as the site)."""

    def __init__(self, root):
        self.root = root
        path = os.path.join(root, "manifest.json")
        if not os.path.isfile(path):
            # Never treat a wrong or empty directory as "first run": that would wipe the history.
            raise PublishError("previous feed directory %s has no manifest.json "
                               "(omit --previous for a first run)" % root)
        self.manifest = _load_json_file(path)

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

    def index(self):
        """The previous feed's index.json, or None (a state from before the data
        branch carried it)."""
        path = os.path.join(self.root, "index.json")
        return _load_json_file(path) if os.path.isfile(path) else None


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

    def index(self):
        if self.manifest is None:
            return None
        body = self._get("index.json")
        return None if body is None else json.loads(body)


def _load_json_file(path):
    with open(path, "rb") as f:
        return json.loads(f.read())


def previous_state(args):
    """--previous SPEC (an http(s) URL, a directory, or empty = first run; this is what
    scripts/state.sh pull prints), or the explicit --previous-url / --previous-dir."""
    spec = getattr(args, "previous", None) or None
    url, folder = getattr(args, "previous_url", None), getattr(args, "previous_dir", None)
    if sum(bool(x) for x in (spec, url, folder)) > 1:
        raise PublishError("give one of --previous, --previous-url, --previous-dir")
    if spec:
        url, folder = (spec, None) if spec.startswith(("https://", "http://")) else (None, spec)
    if url:
        return UrlPrevious(url)
    if folder:
        return DirPrevious(folder)
    return NoPrevious()


# ---- build --------------------------------------------------------------------

def read_bundle(bundle_dir):
    def raw(name):
        path = os.path.join(bundle_dir, name)
        if not os.path.exists(path):
            raise PublishError("missing %s (run pipeline/run.py first)" % path)
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


def price_counts(prices_obj):
    """(products, rows) of a prices.json / prices-market.json object."""
    p = prices_obj.get("p") or {}
    return len(p), sum(len(rows) for rows in p.values())


def shrink_problems(prev_manifest, cards, products, prices_bytes):
    """Sharp drops against the previous manifest (a partial upstream outage or a
    partial pipeline run). Returns a list of messages; empty means fine."""
    problems = []
    prev_cards = (prev_manifest.get("index") or {}).get("cards") or 0
    if prev_cards and cards < MIN_CARDS_RATIO * prev_cards:
        problems.append("catalog shrank from %d to %d cards (< %d %%)"
                        % (prev_cards, cards, round(MIN_CARDS_RATIO * 100)))
    prev_products = (prev_manifest.get("prices") or {}).get("products") or 0
    if prev_products and products < MIN_PRODUCTS_RATIO * prev_products:
        problems.append("prices.json lists %d products, previous feed %d (< %d %%: the app would reject it)"
                        % (products, prev_products, round(MIN_PRODUCTS_RATIO * 100)))
    prev_bytes = (prev_manifest.get("prices") or {}).get("bytes") or 0
    if prev_bytes and prices_bytes < MIN_PRICES_BYTES_RATIO * prev_bytes:
        problems.append("prices.json shrank from %d to %d bytes (< %d %%)"
                        % (prev_bytes, prices_bytes, round(MIN_PRICES_BYTES_RATIO * 100)))
    return problems


# ---- cards are only ever added ----------------------------------------------------

def card_ids(index_obj):
    """Card ids ("<setId>-<localId>") of an index.json object."""
    return {"%s-%s" % (c["s"], c["l"]) for c in (index_obj or {}).get("cards") or []}


def load_floor(path=CATALOG_FLOOR):
    """Card ids in catalog-floor.json ({"sets": {setId: [localId, ...]}}); empty when absent."""
    if not os.path.isfile(path):
        return set()
    doc = _load_json_file(path)
    return {"%s-%s" % (sid, lid) for sid, lids in (doc.get("sets") or {}).items() for lid in lids}


def load_removed(path=OVERRIDES):
    """overrides.json "removedCards": {cardId: reason}, the only cards allowed to leave the catalog."""
    if not os.path.isfile(path):
        return {}
    removed = _load_json_file(path).get("removedCards") or {}
    return {k: v for k, v in removed.items() if not k.startswith("_")}


def required_cards(previous_index, floor=(), removed=()):
    """Every card id today's index.json must still contain: the previous feed's cards and
    the floor's (load_floor), minus the allowed removals (load_removed)."""
    return (set(floor) | card_ids(previous_index)) - set(removed)


def missing_cards_problem(required, index_obj):
    """A message naming the required cards today's index lacks, or None."""
    missing = sorted(set(required) - card_ids(index_obj))
    if not missing:
        return None
    return ("%d card(s) of the previous feed or catalog-floor.json are missing from index.json (%s); cards are "
            "only ever added: if a removal is intended, list it in overrides.json \"removedCards\""
            % (len(missing), ", ".join(missing[:25]) + (" ..." if len(missing) > 25 else "")))


# ---- affiliate links ----------------------------------------------------------------

def _affiliate_url_problem(url):
    bad = sorted({c for c in url if c not in _URL_CHARS})
    if bad:
        return "contains characters a URL can't (%s)" % ", ".join(repr(c) for c in bad[:5])
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        return "must be https, not %r" % parts.scheme
    if "@" in parts.netloc:
        return "must not carry user info (%r)" % parts.netloc
    try:
        port = parts.port
    except ValueError:
        return "has an invalid port (%r)" % parts.netloc
    if port not in (None, 443):
        return "must not use port %d" % port
    host = parts.hostname or ""
    if host not in AFFILIATE_HOSTS:
        return "host %r is not one of %s" % (host, ", ".join(sorted(AFFILIATE_HOSTS)))
    return None


def affiliate_template_problem(template):
    """Why ``template`` can't be links.tcgplayerAffiliate, or None. "" (off) passes. Same rules as the
    app: https, "{url}" exactly once, no user info, no port but 443, host in AFFILIATE_HOSTS, at most
    1,024 bytes; also checked with a product URL in place of {url} (the app re-checks every link)."""
    if not isinstance(template, str):
        return "must be a string or null, not %s" % type(template).__name__
    if template == "":
        return None
    if template != template.strip():
        return "has leading or trailing whitespace"
    size = len(template.encode("utf-8"))
    if size > AFFILIATE_MAX_TEMPLATE:
        return "is %d bytes (at most %d)" % (size, AFFILIATE_MAX_TEMPLATE)
    count = template.count(AFFILIATE_PLACEHOLDER)
    if count != 1:
        return "must contain {url} exactly once (found %d)" % count
    problem = _affiliate_url_problem(template.replace(AFFILIATE_PLACEHOLDER, "x"))
    if problem:
        return problem
    wrapped = template.replace(AFFILIATE_PLACEHOLDER, urllib.parse.quote(AFFILIATE_SAMPLE, safe=""))
    if len(wrapped) > AFFILIATE_MAX_URL:
        return "makes links longer than %d characters" % AFFILIATE_MAX_URL
    return _affiliate_url_problem(wrapped)


def load_links(path=LINKS):
    """The manifest's "links" block from links.json, or None (no file, or tcgplayerAffiliate null).
    Keys starting with "_" are comments; any other unknown key, or an invalid template, is an error."""
    if not path or not os.path.isfile(path):
        return None
    doc = _load_json_file(path)
    if not isinstance(doc, dict):
        raise PublishError("%s must hold a JSON object" % path)
    unknown = sorted(k for k in doc if not k.startswith("_") and k != "tcgplayerAffiliate")
    if unknown:
        raise PublishError("%s: unknown key(s) %s (expected \"tcgplayerAffiliate\")" % (path, ", ".join(unknown)))
    template = doc.get("tcgplayerAffiliate")
    if template is None:
        return None
    problem = affiliate_template_problem(template)
    if problem:
        raise PublishError("%s: tcgplayerAffiliate %s" % (path, problem))
    return {"tcgplayerAffiliate": template}


# ---- history seeds -----------------------------------------------------------------

def load_seeds(seed_dir=SEED_DIR):
    """[(date, prices-market.json bytes)] from <seed_dir>/<YYYY-MM-DD>.json, oldest first.
    A seed whose tcgcsvBuild is not from its file name's day is an error."""
    if not seed_dir or not os.path.isdir(seed_dir):
        return []
    out = []
    for name in sorted(os.listdir(seed_dir)):
        if not name.endswith(".json"):
            continue
        date = name[:-5]
        with open(os.path.join(seed_dir, name), "rb") as f:
            blob = f.read()
        build = (json.loads(blob).get("tcgcsvBuild") or "")[:10]
        if build != date:
            raise PublishError("history seed %s holds TCGCSV build %r" % (name, build))
        out.append((date, blob))
    return out


def check_not_regressing(prev_manifest, today_build, index_obj, prices_obj, prices_blob, allow_shrink):
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
    problems = shrink_problems(prev_manifest, len(index_obj.get("cards", [])), price_counts(prices_obj)[0],
                               len(prices_blob))
    if problems:
        raise PublishError("; ".join(problems) + " (partial upstream outage or pipeline run?); "
                           "pass --allow-shrink if intended")


def build_feed(bundle_dir, out_dir, previous, history_retention=365, daily_retention=90,
               allow_shrink=False, seeds=(), floor=(), removed=(), links=None, tcgplayer_images=None):
    """Write a complete feed directory. Returns a summary dict.

    ``seeds``: [(date, prices-market.json bytes)] (load_seeds); ``floor`` / ``removed``:
    catalog-floor.json card ids (load_floor) and overrides.json "removedCards" (load_removed);
    ``links``: the manifest's "links" block (load_links), None = none; ``tcgplayer_images``: the
    images.json switch (images.load_config): True / False is written as images.tcgplayerFallback,
    None (no file) writes no images block. The previous feed's own index.json is always enforced."""
    b = read_bundle(bundle_dir)
    bm = b["manifest"]
    build_stamp = bm["tcgcsvBuild"]
    date = bm.get("date") or build_stamp[:10]
    index_obj = json.loads(b["index"])
    prices_obj = json.loads(b["prices"])
    prev_manifest = previous.manifest
    if prev_manifest is None:
        log("no previous feed: bootstrapping (history starts at %s)" % date)
    check_not_regressing(prev_manifest, build_stamp, index_obj, prices_obj, b["prices"], allow_shrink)
    problem = missing_cards_problem(required_cards(previous.index(), floor, removed), index_obj)
    if problem:
        raise PublishError(problem)
    problems = images.tcgplayer_image_problems(index_obj.get("cards") or [], bool(tcgplayer_images))
    problems += same_art.alias_problems(index_obj, same_art.load_curated())
    if problems:
        raise PublishError("; ".join(problems))
    seeds = [(d, blob) for d, blob in seeds if d < date]

    # history/<setId>.json
    set_ids = [s["id"] for s in index_obj.get("sets", [])]
    prev_history = previous.history(set_ids)
    # A history file the previous feed had but we could not read would silently restart
    # that set's chart. Allow a couple of sets to leave the catalog, nothing more.
    prev_sets = ((prev_manifest or {}).get("history") or {}).get("sets") or 0
    missing = prev_sets - len(prev_history)
    if missing > max(2, 0.02 * prev_sets) and not allow_shrink:
        raise PublishError("previous feed has %d history files but only %d were found for today's sets; "
                           "pass --allow-shrink if intended" % (prev_sets, len(prev_history)))
    today = history.snapshot_values_by_set(index_obj, prices_obj)
    seed_values = [(d, history.snapshot_values_by_set(index_obj, json.loads(blob))) for d, blob in seeds]
    files = {}  # relative path -> bytes
    statuses = {}
    hist_from, hist_days = None, 0
    for sid in set_ids:
        values = today.get(sid, {})
        prev = prev_history.get(sid) or history.empty_history(sid, index_obj.get("subTypes", []))
        if not values and not prev.get("s") and not any(sv.get(sid) for _, sv in seed_values):
            continue
        h, status = history.append_column(prev, date, values, history_retention)
        statuses[status] = statuses.get(status, 0) + 1
        for d, sv in seed_values:
            h, seed_status = history.insert_column(h, d, sv.get(sid, {}), history_retention)
            key = "seed %s %s" % (d, seed_status)
            statuses[key] = statuses.get(key, 0) + 1
        h["setId"] = sid
        if not h["s"]:
            continue
        files[HISTORY_TEMPLATE.format(setId=sid)] = bundle.dumps_compact(h)
        hist_days = max(hist_days, len(h["dates"]))
        if h["dates"] and (hist_from is None or h["dates"][0] < hist_from):
            hist_from = h["dates"][0]
    n_history = len(files)

    # daily/<date>.json: today's market snapshot plus the retained previous days (and seeds)
    prev_dates = list(((prev_manifest or {}).get("daily") or {}).get("dates") or [])
    seed_blobs = dict(seeds)
    dates = sorted(set(d for d in prev_dates if d < date) | set(seed_blobs) | {date})[-daily_retention:]
    kept = []
    for d in dates:
        if d == date:
            blob = b["pricesMarket"]
        else:
            blob = (previous.daily(d) if d in prev_dates else None) or seed_blobs.get(d)
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
    products, rows = price_counts(prices_obj)
    manifest["prices"] = {"path": "prices.json", "sha256": sha256(b["prices"]), "bytes": len(b["prices"]),
                          "products": products, "rows": rows}
    manifest["pricesMarket"] = {"path": "prices-market.json", "sha256": sha256(b["pricesMarket"]),
                                "bytes": len(b["pricesMarket"])}
    manifest["history"] = {"pathTemplate": HISTORY_TEMPLATE, "from": hist_from, "to": date if n_history else None,
                           "days": hist_days, "retentionDays": history_retention, "sets": n_history}
    manifest["daily"] = {"pathTemplate": DAILY_TEMPLATE, "dates": kept, "retentionDays": daily_retention}
    if links:
        manifest["links"] = dict(links)
    if tcgplayer_images is not None:
        manifest["images"] = {"tcgplayerFallback": bool(tcgplayer_images)}
    files["manifest.json"] = bundle.dumps_compact(manifest)
    files[".nojekyll"] = b""

    write_tree(out_dir, files)
    summary = summarize(files, manifest, statuses)
    summary["tcgplayerImages"] = sum(1 for c in index_obj.get("cards") or [] if images.is_tcgplayer_host(c.get("im")))
    return summary


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
            os.chmod(path, 0o644)
        # GitHub Pages needs world-readable files and directories; mkdtemp makes the root 0700
        # and the umask decides the rest, so set them explicitly.
        for dirpath, _, _ in os.walk(tmp):
            os.chmod(dirpath, 0o755)
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
            "dailyDates": len(manifest["daily"]["dates"]), "sizes": sizes, "total": total,
            "affiliate": (manifest.get("links") or {}).get("tcgplayerAffiliate"),
            "tcgplayerSwitch": (manifest.get("images") or {}).get("tcgplayerFallback")}


def print_summary(summary):
    print("feed %s (TCGCSV build %s)" % (summary["date"], summary["tcgcsvBuild"]))
    print("  history: %s, up to %d day(s); daily snapshots: %d"
          % (summary["historyStatus"], summary["historyDays"], summary["dailyDates"]))
    affiliate = summary.get("affiliate")
    print("  affiliate links: %s" % ("on, " + affiliate if affiliate else
                                     "off (links.tcgplayerAffiliate \"\")" if affiliate == "" else "off (no links block)"))
    switch = summary.get("tcgplayerSwitch")
    print("  TCGplayer images: %s" % ("on, %d card(s)" % summary.get("tcgplayerImages", 0) if switch else
                                      "off (images.tcgplayerFallback false)" if switch is False else
                                      "off (no images block)"))
    for name in sorted(summary["sizes"]):
        s = summary["sizes"][name]
        print("  %-20s %4d file(s)  raw %10s  gzip %10s"
              % (name, s["files"], bundle.human(s["raw"]), bundle.human(s["gzip"])))
    t = summary["total"]
    print("  %-20s %4d file(s)  raw %10s  gzip %10s"
          % ("TOTAL", t["files"], bundle.human(t["raw"]), bundle.human(t["gzip"])))


# ---- floor ---------------------------------------------------------------------

def _natural(local_id):
    """"2" before "10", "SV9" before "SV10"."""
    return [int(t) if i % 2 else t for i, t in enumerate(re.split(r"(\d+)", local_id))]


def add_to_floor(index_paths, label, path=CATALOG_FLOOR):
    """Union the cards of ``index_paths`` into catalog-floor.json (never removes).
    Returns (cards before, cards after)."""
    doc = _load_json_file(path) if os.path.isfile(path) else {}
    sets = {sid: set(lids) for sid, lids in (doc.get("sets") or {}).items()}
    before = sum(len(v) for v in sets.values())
    for p in index_paths:
        for c in _load_json_file(p).get("cards") or []:
            sets.setdefault(c["s"], set()).add(c["l"])
    after = sum(len(v) for v in sets.values())
    sources = list(doc.get("sources") or [])
    if after > before:
        sources.append("%s: +%d cards, %s" % (label or ", ".join(index_paths), after - before,
                                              datetime.date.today().isoformat()))
    doc = {"_doc": "Every card a shipped Evodex catalog contains (setId -> localIds). publish_feed.py build "
                   "and validate_feed.py fail when today's index.json lacks one, unless overrides.json "
                   "\"removedCards\" lists it. Only grows: publish_feed.py floor --add <index.json>.",
           "sources": sources, "cards": after,
           "sets": {sid: sorted(sets[sid], key=_natural) for sid in sorted(sets)}}
    lines = ["{"]
    for k in ("_doc", "sources", "cards"):
        lines.append("  %s: %s," % (json.dumps(k), json.dumps(doc[k], ensure_ascii=False)))
    lines.append('  "sets": {')
    for i, sid in enumerate(doc["sets"]):
        comma = "," if i < len(doc["sets"]) - 1 else ""
        lines.append("    %s: %s%s" % (json.dumps(sid), json.dumps(doc["sets"][sid], ensure_ascii=False), comma))
    lines.append("  }")
    lines.append("}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return before, after


# ---- check -------------------------------------------------------------------

def tcgcsv_last_updated():
    body = net.http_get(sources.TCGCSV_BASE + "/last-updated.txt", retries=3)
    return body.decode("utf-8").strip()


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def check(previous_manifest, force=False, tcgcsv_stamp=None, wait_seconds=0, poll_seconds=900,
          stamp_fn=None, sleep=time.sleep, now=utcnow):
    """Returns (changed, reason, tcgcsv_stamp).

    ``previous_manifest`` is the previous feed's manifest (None = no feed yet).
    When TCGCSV still serves the build the feed already has and that build is
    from an earlier UTC day (today's ~20:05 UTC build is late), last-updated.txt
    is polled every ``poll_seconds`` for up to ``wait_seconds``: one tiny
    request per poll, so the single daily schedule still catches a late build.
    """
    stamp_fn = stamp_fn or tcgcsv_last_updated
    stamp = tcgcsv_stamp if tcgcsv_stamp is not None else stamp_fn()
    if force:
        return True, "forced", stamp
    if previous_manifest is None:
        return True, "no previous feed yet", stamp
    prev_stamp = previous_manifest.get("tcgcsvBuild")
    if not prev_stamp:
        return True, "previous feed has no TCGCSV build", stamp
    waited = 0
    while True:
        if parse_stamp(stamp) != parse_stamp(prev_stamp):
            return True, "TCGCSV build %s differs from the feed's %s" % (stamp, prev_stamp), stamp
        late = parse_stamp(stamp).astimezone(datetime.timezone.utc).date() < now().date()
        if not late or waited + poll_seconds > wait_seconds:
            break
        log("TCGCSV still at %s (today's build not out yet); next check in %d min"
            % (stamp, poll_seconds // 60))
        sleep(poll_seconds)
        waited += poll_seconds
        stamp = stamp_fn()
    return False, "feed already has TCGCSV build %s" % stamp, stamp


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
    b.add_argument("--previous", help="previous feed: URL or directory (scripts/state.sh pull prints it); "
                   "empty or omitted = first run")
    b.add_argument("--previous-url")
    b.add_argument("--previous-dir")
    b.add_argument("--history-retention", type=int, default=365, help="dates kept per history file")
    b.add_argument("--daily-retention", type=int, default=90, help="daily snapshots kept")
    b.add_argument("--allow-shrink", action="store_true",
                   help="publish even if the catalog or price file shrank sharply (never lets a card disappear)")
    b.add_argument("--seed-dir", default=SEED_DIR,
                   help="history seeds folded in when their date is absent (default: history-seed/ next to "
                        "this file; '' = none)")
    b.add_argument("--links", default=LINKS,
                   help="affiliate link switch (default: links.json next to this file; missing = no links block)")
    b.add_argument("--images", default=images.CONFIG,
                   help="TCGplayer image switch (default: images.json next to this file; missing = no images block)")

    fl = sub.add_parser("floor", help="add every card of index.json files to catalog-floor.json")
    fl.add_argument("--add", nargs="+", required=True, metavar="INDEX_JSON")
    fl.add_argument("--label", help='what the index is, e.g. "app 0.3.0 bundle"')
    fl.add_argument("--floor", default=CATALOG_FLOOR)

    c = sub.add_parser("check", help="is a new TCGCSV build available?")
    c.add_argument("--previous", help="previous feed: URL or directory (scripts/state.sh pull prints it); "
                   "empty or omitted = first run")
    c.add_argument("--previous-url")
    c.add_argument("--previous-dir")
    c.add_argument("--force", action="store_true")
    c.add_argument("--wait-minutes", type=int, default=0,
                   help="poll TCGCSV this long while today's build is late (default 0)")
    c.add_argument("--poll-minutes", type=int, default=15)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "build":
            summary = build_feed(args.bundle, args.out, previous_state(args),
                                 args.history_retention, args.daily_retention, args.allow_shrink,
                                 seeds=load_seeds(args.seed_dir), floor=load_floor(), removed=load_removed(),
                                 links=load_links(args.links), tcgplayer_images=images.load_config(args.images))
            print_summary(summary)
            return 0
        if args.cmd == "floor":
            before, after = add_to_floor(args.add, args.label, args.floor)
            print("catalog floor %s: %d -> %d cards" % (args.floor, before, after))
            return 0
        if args.cmd == "check":
            changed, reason, stamp = check(previous_state(args).manifest, args.force,
                                           wait_seconds=args.wait_minutes * 60,
                                           poll_seconds=max(1, args.poll_minutes) * 60)
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
