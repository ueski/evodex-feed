import gzip
import hashlib
import json
import os
import shutil
import tempfile
import unittest

import bundle
import history
import publish_feed

NAMES = ["Normal", "Holofoil", "Reverse Holofoil"]


def fold(hist, date, values, retention=365):
    return history.append_column(hist, date, values, retention)


class AppendColumnTests(unittest.TestCase):
    def test_append_to_empty(self):
        h, status = fold(history.empty_history("base1"), "2026-10-21",
                         {(10, "Holofoil"): 500, (11, "Normal"): None})
        self.assertEqual(status, "appended")
        self.assertEqual(h["setId"], "base1")
        self.assertEqual(h["dates"], ["2026-10-21"])
        self.assertEqual(h["subTypes"], ["Holofoil", "Normal"])
        # all-null series are dropped
        self.assertEqual(h["s"], {"10.0": [500]})

    def test_append_next_day_and_null_for_missing_series(self):
        h, _ = fold(history.empty_history("s"), "2026-10-21", {(10, "Holofoil"): 500, (12, "Normal"): 7})
        h, status = fold(h, "2026-10-22", {(10, "Holofoil"): 520})
        self.assertEqual(status, "appended")
        self.assertEqual(h["dates"], ["2026-10-21", "2026-10-22"])
        self.assertEqual(h["s"]["10.0"], [500, 520])
        self.assertEqual(h["s"]["12.1"], [7, None])

    def test_new_series_is_back_filled_with_null(self):
        h, _ = fold(history.empty_history("s"), "2026-10-21", {(10, "Holofoil"): 500})
        h, _ = fold(h, "2026-10-22", {(10, "Holofoil"): 510, (99, "Holofoil"): 42})
        self.assertEqual(h["s"]["99.0"], [None, 42])

    def test_same_date_replaces_the_column(self):
        h, _ = fold(history.empty_history("s"), "2026-10-21", {(10, "Holofoil"): 500})
        h, _ = fold(h, "2026-10-22", {(10, "Holofoil"): 510, (11, "Holofoil"): 3})
        h, status = fold(h, "2026-10-22", {(10, "Holofoil"): 999})
        self.assertEqual(status, "replaced")
        self.assertEqual(h["dates"], ["2026-10-21", "2026-10-22"])
        self.assertEqual(h["s"]["10.0"], [500, 999])
        # 11 had a value only in the replaced column -> all null -> dropped
        self.assertNotIn("11.0", h["s"])

    def test_older_date_is_a_no_op(self):
        h, _ = fold(history.empty_history("s"), "2026-10-22", {(10, "Holofoil"): 500})
        h2, status = fold(h, "2026-10-21", {(10, "Holofoil"): 1})
        self.assertEqual(status, "skipped")
        self.assertEqual(h2, h)

    def test_input_is_not_mutated(self):
        h, _ = fold(history.empty_history("s"), "2026-10-21", {(10, "Holofoil"): 500})
        before = json.dumps(h, sort_keys=True)
        fold(h, "2026-10-22", {(10, "Holofoil"): 1, (11, "Normal"): 2})
        fold(h, "2026-10-21", {(10, "Holofoil"): 3})
        self.assertEqual(json.dumps(h, sort_keys=True), before)

    def test_subtypes_are_remapped_by_name(self):
        prev = {"v": 1, "setId": "s", "dates": ["2026-10-21"], "subTypes": ["Reverse Holofoil", "Normal"],
                "s": {"10.0": [300], "10.1": [100]}}
        h, _ = fold(prev, "2026-10-22", {(10, "Normal"): 110, (10, "Reverse Holofoil"): 310,
                                         (10, "1st Edition"): 5000})
        self.assertEqual(h["subTypes"], ["Reverse Holofoil", "Normal", "1st Edition"])
        self.assertEqual(h["s"]["10.0"], [300, 310])
        self.assertEqual(h["s"]["10.1"], [100, 110])
        self.assertEqual(h["s"]["10.2"], [None, 5000])

    def test_retention_trims_and_drops_all_null_series(self):
        h = history.empty_history("s")
        h, _ = fold(h, "2026-10-20", {(1, "Normal"): 1, (2, "Normal"): 2}, retention=2)
        h, _ = fold(h, "2026-10-21", {(1, "Normal"): 3}, retention=2)
        h, _ = fold(h, "2026-10-22", {(1, "Normal"): 4}, retention=2)
        self.assertEqual(h["dates"], ["2026-10-21", "2026-10-22"])
        self.assertEqual(h["s"], {"1.0": [3, 4]})

    def test_series_keys_are_sorted_numerically(self):
        h, _ = fold(history.empty_history("s"), "2026-10-21",
                    {(100, "Normal"): 1, (20, "Holofoil"): 2, (20, "Normal"): 3})
        self.assertEqual(list(h["s"]), ["20.0", "20.1", "100.0"])


class SnapshotValuesTests(unittest.TestCase):
    def test_values_by_set_remap_subtypes_by_name(self):
        index = {"subTypes": ["Normal", "Holofoil"],
                 "cards": [{"s": "a", "l": "1", "n": "X", "p": [{"id": 10, "st": [0, 1]}]},
                           {"s": "a", "l": "2", "n": "Y"},
                           {"s": "b", "l": "1", "n": "Z", "p": [{"id": 20}, {"id": 21, "st": [1]}]}]}
        prices = {"subTypes": ["Holofoil", "Normal"],
                  "p": {"10": [[1, 100, 1, 2, 3, 4], [0, 200, None, None, None, None]],
                        "21": [[0, None, 5, 6, 7, 8]]}}
        got = history.snapshot_values_by_set(index, prices)
        self.assertEqual(got, {"a": {(10, "Normal"): 100, (10, "Holofoil"): 200},
                               "b": {(21, "Holofoil"): None}})


def make_bundle(root, build, cards, prices_p, subtypes=("Normal", "Holofoil")):
    """Write a minimal bundle like run.py does (manifest sha256 over exact bytes)."""
    os.makedirs(root, exist_ok=True)
    sets = sorted({c["s"] for c in cards})
    index = {"v": 1, "imageBase": "https://assets.tcgdex.net/en", "rarities": [], "types": [],
             "illustrators": [], "subTypes": list(subtypes), "series": [{"id": "x", "name": "X"}],
             "sets": [{"id": s, "name": s, "series": "x", "releaseDate": "2000-01-01", "official": 1,
                       "total": 1, "groups": []} for s in sets],
             "cards": cards}
    prices = {"v": 1, "tcgcsvBuild": build, "subTypes": list(subtypes), "p": prices_p}
    blobs = {"index.json": bundle.dumps_compact(index), "prices.json": bundle.dumps_compact(prices),
             "prices-market.json": bundle.dumps_compact(bundle.market_only(prices))}
    for name, blob in blobs.items():
        with open(os.path.join(root, name), "wb") as f:
            f.write(blob)
    manifest = {"v": 1, "generatedAt": "2026-10-22T08:00:00Z", "tcgcsvBuild": build, "date": build[:10],
                "index": {"path": "index.json", "sha256": hashlib.sha256(blobs["index.json"]).hexdigest(),
                          "bytes": len(blobs["index.json"]), "cards": len(cards), "sets": len(sets)},
                "prices": {"path": "prices.json", "sha256": hashlib.sha256(blobs["prices.json"]).hexdigest(),
                           "bytes": len(blobs["prices.json"])},
                "pricesMarket": {"path": "prices-market.json",
                                 "sha256": hashlib.sha256(blobs["prices-market.json"]).hexdigest(),
                                 "bytes": len(blobs["prices-market.json"])},
                "history": {"pathTemplate": "history/{groupId}.json", "days": 1}}
    with open(os.path.join(root, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    return root


CARDS = [{"s": "base1", "l": "4", "n": "Charizard", "p": [{"id": 42382, "st": [1]}]},
         {"s": "base1", "l": "5", "n": "Clefairy", "p": [{"id": 42383, "st": [0, 1]}]},
         {"s": "sv03.5", "l": "025", "n": "Pikachu", "p": [{"id": 500, "st": [0]}]},
         {"s": "empty", "l": "1", "n": "Nothing"}]


def read(path, binary=False):
    with open(path, "rb" if binary else "r") as f:
        return f.read() if binary else json.load(f)


class BuildFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def day1(self):
        return make_bundle(self.path("b1"), "2026-10-21T20:05:22+0000", CARDS,
                           {"42382": [[1, 95465, 1, 2, 3, 4]], "42383": [[0, 100, 1, 2, 3, 4]],
                            "500": [[0, 25, None, None, None, None]]})

    def day2(self):
        return make_bundle(self.path("b2"), "2026-10-22T20:04:10+0000", CARDS,
                           {"42382": [[1, 96000, 1, 2, 3, 4]], "42383": [[1, 900, 1, 2, 3, 4]],
                            "500": [[0, 30, None, None, None, None]]})

    def test_manifest_fields_and_sha256(self):
        out = self.path("feed")
        publish_feed.build_feed(self.day1(), out, publish_feed.NoPrevious())
        m = read(os.path.join(out, "manifest.json"))
        self.assertEqual(m["v"], 1)
        self.assertEqual(m["date"], "2026-10-21")
        self.assertEqual(m["tcgcsvBuild"], "2026-10-21T20:05:22+0000")
        for key, name in (("index", "index.json"), ("prices", "prices.json"),
                          ("pricesMarket", "prices-market.json")):
            blob = read(os.path.join(out, name), binary=True)
            self.assertEqual(m[key]["path"], name)
            self.assertEqual(m[key]["sha256"], hashlib.sha256(blob).hexdigest())
            self.assertEqual(m[key]["bytes"], len(blob))
            # byte-for-byte copy of the bundle
            self.assertEqual(blob, read(self.path("b1", name), binary=True))
        self.assertEqual(m["index"]["cards"], 4)
        self.assertEqual(m["history"], {"pathTemplate": "history/{setId}.json", "from": "2026-10-21",
                                        "to": "2026-10-21", "days": 1, "retentionDays": 365, "sets": 2})
        self.assertEqual(m["daily"], {"pathTemplate": "daily/{date}.json", "dates": ["2026-10-21"],
                                      "retentionDays": 90})
        self.assertTrue(os.path.exists(os.path.join(out, ".nojekyll")))
        self.assertFalse(os.path.exists(os.path.join(out, "history", "empty.json")))
        self.assertEqual(read(os.path.join(out, "daily", "2026-10-21.json"), binary=True),
                         read(self.path("b1", "prices-market.json"), binary=True))
        h = read(os.path.join(out, "history", "base1.json"))
        self.assertEqual(h["setId"], "base1")
        self.assertEqual(h["s"], {"42382.1": [95465], "42383.0": [100]})
        # everything is minified JSON
        self.assertNotIn(b"\n", read(os.path.join(out, "manifest.json"), binary=True))

    def test_previous_dir_round_trip_two_days(self):
        feed1, feed2 = self.path("feed1"), self.path("feed2")
        publish_feed.build_feed(self.day1(), feed1, publish_feed.NoPrevious())
        summary = publish_feed.build_feed(self.day2(), feed2, publish_feed.DirPrevious(feed1))
        self.assertEqual(summary["historyStatus"], {"appended": 2})
        m = read(os.path.join(feed2, "manifest.json"))
        self.assertEqual(m["daily"]["dates"], ["2026-10-21", "2026-10-22"])
        self.assertEqual(m["history"]["from"], "2026-10-21")
        self.assertEqual(m["history"]["days"], 2)
        h = read(os.path.join(feed2, "history", "base1.json"))
        self.assertEqual(h["dates"], ["2026-10-21", "2026-10-22"])
        self.assertEqual(h["s"]["42382.1"], [95465, 96000])
        self.assertEqual(h["s"]["42383.0"], [100, None])   # printing lost its price row
        self.assertEqual(h["s"]["42383.1"], [None, 900])   # and gained another one
        self.assertEqual(read(os.path.join(feed2, "daily", "2026-10-21.json"), binary=True),
                         read(os.path.join(feed1, "daily", "2026-10-21.json"), binary=True))
        # re-running the same day is idempotent
        feed3 = self.path("feed3")
        summary = publish_feed.build_feed(self.day2(), feed3, publish_feed.DirPrevious(feed2))
        self.assertEqual(summary["historyStatus"], {"replaced": 2})
        self.assertEqual(read(os.path.join(feed3, "history", "base1.json")), h)

    def test_daily_retention(self):
        feed = self.path("feed0")
        publish_feed.build_feed(self.day1(), feed, publish_feed.NoPrevious(), daily_retention=2)
        prev = feed
        for day in (22, 23, 24):
            b = make_bundle(self.path("b%d" % day), "2026-10-%dT20:00:00+0000" % day, CARDS,
                            {"500": [[0, day, None, None, None, None]]})
            out = self.path("feed%d" % day)
            publish_feed.build_feed(b, out, publish_feed.DirPrevious(prev), daily_retention=2,
                                    allow_shrink=True)
            prev = out
        m = read(os.path.join(prev, "manifest.json"))
        self.assertEqual(m["daily"]["dates"], ["2026-10-23", "2026-10-24"])
        self.assertEqual(sorted(os.listdir(os.path.join(prev, "daily"))), ["2026-10-23.json", "2026-10-24.json"])

    def test_refuses_older_build_and_keeps_output_untouched(self):
        feed2 = self.path("feed2")
        publish_feed.build_feed(self.day2(), feed2, publish_feed.NoPrevious())
        out = self.path("out")
        with self.assertRaises(publish_feed.PublishError):
            publish_feed.build_feed(self.day1(), out, publish_feed.DirPrevious(feed2))
        self.assertFalse(os.path.exists(out))

    def test_refuses_shrunken_catalog(self):
        feed1 = self.path("feed1")
        publish_feed.build_feed(self.day1(), feed1, publish_feed.NoPrevious())
        small = make_bundle(self.path("small"), "2026-10-22T20:00:00+0000", CARDS[:1],
                            {"42382": [[1, 1, 1, 1, 1, 1]]})
        with self.assertRaises(publish_feed.PublishError):
            publish_feed.build_feed(small, self.path("out"), publish_feed.DirPrevious(feed1))
        publish_feed.build_feed(small, self.path("out"), publish_feed.DirPrevious(feed1), allow_shrink=True)

    def test_rejects_bundle_whose_files_do_not_match_manifest(self):
        b = self.day1()
        with open(os.path.join(b, "prices.json"), "ab") as f:
            f.write(b" ")
        with self.assertRaises(publish_feed.PublishError):
            publish_feed.build_feed(b, self.path("out"), publish_feed.NoPrevious())

    def test_summary_reports_gzip_sizes(self):
        summary = publish_feed.build_feed(self.day1(), self.path("feed"), publish_feed.NoPrevious())
        blob = read(self.path("feed", "prices.json"), binary=True)
        self.assertEqual(summary["sizes"]["prices.json"]["raw"], len(blob))
        self.assertEqual(summary["sizes"]["prices.json"]["gzip"], len(gzip.compress(blob, 6)))
        self.assertEqual(summary["sizes"]["history"]["files"], 2)


class CheckTests(unittest.TestCase):
    def run_check(self, live_manifest, stamp, force=False):
        class FakeLive:
            def __init__(self, url):
                self.manifest = live_manifest
        original = publish_feed.UrlPrevious
        publish_feed.UrlPrevious = FakeLive
        try:
            return publish_feed.check("https://example.invalid/", force=force, tcgcsv_stamp=stamp)[0]
        finally:
            publish_feed.UrlPrevious = original

    def test_changed_rules(self):
        live = {"v": 1, "tcgcsvBuild": "2026-10-21T20:05:22+0000"}
        self.assertFalse(self.run_check(live, "2026-10-21T20:05:22+0000"))
        self.assertTrue(self.run_check(live, "2026-10-22T20:03:00+0000"))
        self.assertTrue(self.run_check(None, "2026-10-21T20:05:22+0000"))
        self.assertTrue(self.run_check(live, "2026-10-21T20:05:22+0000", force=True))


class StampTests(unittest.TestCase):
    def test_parse_tcgcsv_stamp(self):
        a = publish_feed.parse_stamp("2026-10-21T20:05:22+0000")
        b = publish_feed.parse_stamp("2026-10-21T20:05:22Z")
        self.assertEqual(a, b)
        self.assertLess(a, publish_feed.parse_stamp("2026-10-22T20:01:00+0000"))


if __name__ == "__main__":
    unittest.main()
