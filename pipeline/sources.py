"""Fetchers for TCGdex (catalog) and TCGCSV (TCGplayer products + prices).

Caching policy (all under ``cache/``):

TCGdex
  tcgdex/en/series.json, sets.json      refreshed when older than ``list_ttl``
  tcgdex/en/sets/<id>.json              refreshed when older than ``list_ttl``
  tcgdex/en/cards/<id>.json             refreshed when older than ``card_ttl``

TCGCSV (one build per day, stamp from last-updated.txt)
  tcgcsv/last-updated.txt               refreshed when older than 1 hour
  tcgcsv/<stamp>/groups.json            immutable per build
  tcgcsv/products/<gid>@<modifiedOn>.json   re-fetched only when the group's
                                        modifiedOn changes (products rarely do)
  tcgcsv/<stamp>/prices/<gid>.json      immutable per build (this directory is
                                        also the raw material for price history)

PokéAPI (species list, see species.py)
  pokeapi/<file>.csv                    static CSVs from the PokeAPI/pokeapi repository,
                                        refreshed when older than 30 days

pokemontcg.io (fallback images only, see images.py)
  ptcgio/sets.json, ptcgio/cards/<id>.json   normalized {"source", "data"},
                                        refreshed when older than ``ttl`` (7 days)
  images/status.json                    HEAD status of every fallback image URL
"""

import csv
import io
import json
import os
import re
import time
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import net

TCGDEX_BASE = "https://api.tcgdex.net/v2/en"
TCGCSV_BASE = "https://tcgcsv.com"
POKEMON_CATEGORY = 3

# Series that are not physical cards (Pokemon TCG Pocket is a mobile game).
DIGITAL_SERIES = {"tcgp"}

DAY = 24 * 3600


def _q(s):
    return urllib.parse.quote(s, safe="")


def _safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def _progress(label, done, total):
    if total and (done == total or done % 250 == 0):
        sys.stderr.write("  %s %d/%d\n" % (label, done, total))


class TCGdex:
    def __init__(self, cache_dir, workers=4, min_interval=0.075, list_ttl=DAY,
                 card_ttl=14 * DAY, offline=False):
        self.root = os.path.join(cache_dir, "tcgdex", "en")
        self.workers = workers
        self.limiter = net.RateLimiter(min_interval)
        self.list_ttl = list_ttl
        self.card_ttl = card_ttl
        self.offline = offline

    def _get(self, path, cache_rel, ttl):
        return net.cached_json(TCGDEX_BASE + path, os.path.join(self.root, cache_rel),
                               max_age=ttl, limiter=self.limiter, offline=self.offline)

    def series(self):
        return self._get("/series", "series.json", self.list_ttl)

    def set_list(self):
        return self._get("/sets", "sets.json", self.list_ttl)

    def set_detail(self, set_id):
        return self._get("/sets/" + _q(set_id), "sets/%s.json" % _safe_name(set_id),
                         self.list_ttl)

    def card(self, card_id):
        return self._get("/cards/" + _q(card_id), "cards/%s.json" % _safe_name(card_id),
                         self.card_ttl)

    def _parallel(self, fn, keys, label):
        """Run fn over keys with a small pool. A key that still fails after
        net.http_get's retries is reported and mapped to None, so one bad
        card never aborts a 20k-request run."""
        out, failed = {}, []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(fn, k): k for k in keys}
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    out[futs[fut]] = fut.result()
                except net.FetchError as e:
                    out[futs[fut]] = None
                    failed.append(futs[fut])
                    if not self.offline:
                        sys.stderr.write("  ! %s\n" % e)
                _progress(label, i, len(keys))
        if failed:
            sys.stderr.write("  %s: %d failed%s\n" % (
                label, len(failed), " (not cached, offline)" if self.offline else ""))
        self.failed = getattr(self, "failed", []) + failed
        return out

    def fetch_catalog(self, only_sets=None):
        """Return (series, sets, cards): sets carry series + releaseDate, cards
        are full card objects keyed by id. Digital (TCG Pocket) sets are
        returned with ``digital: True`` and their cards are not fetched."""
        series = self.series()
        set_ids = [s["id"] for s in self.set_list()]
        if only_sets:
            set_ids = [s for s in set_ids if s in only_sets]
        details = self._parallel(self.set_detail, set_ids, "tcgdex sets")
        sets = []
        for sid in set_ids:
            d = details[sid]
            if d is None:
                continue
            d["digital"] = d.get("serie", {}).get("id") in DIGITAL_SERIES
            sets.append(d)
        card_ids = [c["id"] for s in sets if not s["digital"] for c in s.get("cards", [])]
        cards = self._parallel(self.card, card_ids, "tcgdex cards")
        return series, sets, {k: v for k, v in cards.items() if v is not None}


class TCGCSV:
    def __init__(self, cache_dir, min_interval=0.15, offline=False):
        self.root = os.path.join(cache_dir, "tcgcsv")
        self.limiter = net.RateLimiter(min_interval)
        self.offline = offline
        self._stamp = None

    def stamp(self):
        """Build timestamp of the current TCGCSV dump, e.g. 2026-10-21T200522."""
        if self._stamp is None:
            txt = net.cached_text(TCGCSV_BASE + "/last-updated.txt",
                                  os.path.join(self.root, "last-updated.txt"),
                                  max_age=3600, limiter=self.limiter, offline=self.offline)
            self._stamp = _safe_name(txt.strip()[:19].replace(":", ""))
        return self._stamp

    def last_updated_raw(self):
        with open(os.path.join(self.root, "last-updated.txt"), encoding="utf-8") as f:
            return f.read().strip()

    def groups(self):
        url = "%s/tcgplayer/%d/groups" % (TCGCSV_BASE, POKEMON_CATEGORY)
        path = os.path.join(self.root, self.stamp(), "groups.json")
        return net.cached_json(url, path, None, self.limiter, self.offline)["results"]

    def products(self, group):
        gid = group["groupId"]
        url = "%s/tcgplayer/%d/%d/products" % (TCGCSV_BASE, POKEMON_CATEGORY, gid)
        path = os.path.join(self.root, "products", "%d@%s.json" % (
            gid, _safe_name(group.get("modifiedOn", "")[:19])))
        data = net.cached_json(url, path, None, self.limiter, self.offline)
        return (data or {}).get("results", [])

    def prices(self, gid):
        url = "%s/tcgplayer/%d/%d/prices" % (TCGCSV_BASE, POKEMON_CATEGORY, gid)
        path = os.path.join(self.root, self.stamp(), "prices", "%d.json" % gid)
        data = net.cached_json(url, path, None, self.limiter, self.offline)
        return (data or {}).get("results", [])

    def fetch_all(self):
        """Sequential on purpose: TCGCSV asks for ~100 ms between requests."""
        groups = self.groups()
        products, prices = {}, {}
        for i, g in enumerate(groups, 1):
            products[g["groupId"]] = self.products(g)
            prices[g["groupId"]] = self.prices(g["groupId"])
            _progress("tcgcsv groups", i, len(groups))
        return groups, products, prices


class PokemonTCGio:
    """pokemontcg.io card data, used only for fallback image URLs.

    The v2 API (no key: 1,000 requests/day, 30/minute) answers 500/502 to a
    good share of requests, so each list is read from the API first (with
    net.http_get's retries) and, when that still fails, from the project's own
    data repository on GitHub, which the API is built from (same ids, numbers
    and image URLs). Results are normalized to plain lists and cached for
    ``ttl`` seconds; only sets that need a fallback image are ever fetched.
    """

    API = "https://api.pokemontcg.io/v2"
    MIRROR = "https://raw.githubusercontent.com/PokemonTCG/pokemon-tcg-data/master"
    CARD_FIELDS = "id,number,name,images"

    def __init__(self, cache_dir, ttl=7 * DAY, offline=False):
        self.root = os.path.join(cache_dir, "ptcgio")
        self.api_limiter = net.RateLimiter(2.0)      # 30 requests/minute
        self.mirror_limiter = net.RateLimiter(0.25)
        self.ttl = ttl
        self.offline = offline
        self.sources = {}   # list name -> "api" | "github" | "cache"

    def _cached(self, rel, fetch_api, fetch_mirror):
        path = os.path.join(self.root, rel)
        if os.path.exists(path) and (self.offline or time.time() - os.path.getmtime(path) <= self.ttl):
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            self.sources[rel] = "cache:" + doc["source"]
            return doc["data"]
        if self.offline:
            raise net.FetchError("offline and not cached: pokemontcg.io %s" % rel)
        try:
            data, source = fetch_api(), "api"
        except (net.FetchError, net.NotFound, ValueError) as e:
            sys.stderr.write("  pokemontcg.io API failed for %s (%s); using the GitHub mirror\n" % (rel, e))
            data, source = fetch_mirror(), "github"
        net._atomic_write(path, json.dumps({"source": source, "data": data},
                                           ensure_ascii=False).encode("utf-8"))
        self.sources[rel] = source
        return data

    def _api_pages(self, path, params):
        out, page = [], 1
        while True:
            url = "%s%s?%s" % (self.API, path, urllib.parse.urlencode(dict(params, page=page, pageSize=250)))
            doc = json.loads(net.http_get(url, limiter=self.api_limiter))
            out.extend(doc["data"])
            if not doc["data"] or len(out) >= doc.get("totalCount", 0):
                return out
            page += 1

    def _mirror(self, rel):
        return json.loads(net.http_get("%s/%s" % (self.MIRROR, rel), limiter=self.mirror_limiter))

    def sets(self):
        """[{id, name, series, releaseDate "YYYY/MM/DD", ...}]"""
        return self._cached("sets.json", lambda: self._api_pages("/sets", {}),
                            lambda: self._mirror("sets/en.json"))

    def cards(self, set_id):
        """[{id, number, name, images: {small, large}}] of one pokemontcg.io set."""
        def slim(cards):
            return [{k: c[k] for k in ("id", "number", "name", "images") if k in c} for c in cards]
        return self._cached(
            "cards/%s.json" % _safe_name(set_id),
            lambda: slim(self._api_pages("/cards", {"q": "set.id:" + set_id, "select": self.CARD_FIELDS})),
            lambda: slim(self._mirror("cards/en/%s.json" % urllib.parse.quote(set_id))))


class PokeAPI:
    """PokéAPI's own static CSV export (data/v2/csv in the PokeAPI/pokeapi
    repository): six small files instead of ~2,000 REST calls. Cached for
    ``ttl`` seconds; offline runs use the cache only."""

    BASE = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv"
    FILES = ("pokemon_species", "pokemon_species_names", "pokemon", "pokemon_types", "types")

    def __init__(self, cache_dir, ttl=30 * DAY, offline=False):
        self.root = os.path.join(cache_dir, "pokeapi")
        self.limiter = net.RateLimiter(0.5)
        self.ttl = ttl
        self.offline = offline

    def rows(self, name):
        """[dict] rows of data/v2/csv/<name>.csv. A failed refresh falls back
        to the stale cached copy (the species list changes once a generation)."""
        path = os.path.join(self.root, name + ".csv")
        try:
            text = net.cached_text("%s/%s.csv" % (self.BASE, name), path,
                                   max_age=self.ttl, limiter=self.limiter, offline=self.offline)
        except (net.FetchError, net.NotFound) as e:
            if not os.path.exists(path):
                raise
            sys.stderr.write("  PokéAPI %s refresh failed (%s); using the cached copy\n" % (name, e))
            with open(path, encoding="utf-8") as f:
                text = f.read()
        return list(csv.DictReader(io.StringIO(text)))

    def tables(self):
        return {name: self.rows(name) for name in self.FILES}
