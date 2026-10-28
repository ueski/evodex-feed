import json
import os
import shutil
import tempfile
import unittest

import publish_feed
import validate_feed
from tests.test_publish_feed import CARDS, make_bundle

DAY1 = {"42382": [[1, 95465, 1, 2, 3, 4]], "42383": [[0, 100, 1, 2, 3, 4]], "500": [[0, 25, None, None, None, None]]}
DAY2 = {"42382": [[1, 96000, 1, 2, 3, 4]], "42383": [[1, 900, 1, 2, 3, 4]], "500": [[0, 30, None, None, None, None]]}


class ValidateFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def feed(self, name, build, prices, previous=None, **kw):
        b = make_bundle(self.path("b-" + name), build, CARDS, prices)
        out = self.path(name)
        publish_feed.build_feed(b, out, previous or publish_feed.NoPrevious(), **kw)
        return out

    def manifest(self, feed):
        with open(os.path.join(feed, "manifest.json")) as f:
            return json.load(f)

    def validate(self, feed, previous=None, **kw):
        kw.setdefault("min_cards", 1)
        kw.setdefault("min_products", 1)
        return validate_feed.validate(feed, previous, **kw)[0]

    def two_days(self):
        f1 = self.feed("f1", "2026-10-21T20:05:22+0000", DAY1)
        f2 = self.feed("f2", "2026-10-22T20:04:10+0000", DAY2, publish_feed.DirPrevious(f1))
        return f1, f2

    def test_first_run_and_second_day_pass(self):
        f1, f2 = self.two_days()
        self.assertEqual(self.validate(f1), [])
        self.assertEqual(self.validate(f2, self.manifest(f1)), [])
        errors, stats = validate_feed.validate(f2, self.manifest(f1), min_cards=1, min_products=1)
        self.assertEqual(stats["products"], 3)
        self.assertEqual(stats["historyDays"], 2)
        self.assertEqual(stats["sizes"]["daily"]["files"], 2)

    def test_first_run_floors(self):
        f1 = self.feed("f1", "2026-10-21T20:05:22+0000", DAY1)
        errors = validate_feed.validate(f1, None)[0]          # default floors: 15,000 cards / products
        self.assertTrue(any("first run" in e for e in errors), errors)

    def test_sha256_mismatch_fails(self):
        f1 = self.feed("f1", "2026-10-21T20:05:22+0000", DAY1)
        with open(os.path.join(f1, "prices.json"), "r+b") as f:
            data = f.read()
            f.seek(0)
            f.write(data.replace(b"95465", b"95466"))
        errors = self.validate(f1)
        self.assertTrue(any("sha256" in e for e in errors), errors)

    def test_stray_or_missing_daily_files_fail(self):
        f1, f2 = self.two_days()
        shutil.copy(os.path.join(f2, "daily", "2026-10-22.json"), os.path.join(f2, "daily", "2026-10-20.json"))
        self.assertTrue(any("daily/" in e for e in self.validate(f2, self.manifest(f1))))
        os.remove(os.path.join(f2, "daily", "2026-10-20.json"))
        os.remove(os.path.join(f2, "daily", "2026-10-21.json"))
        self.assertTrue(self.validate(f2, self.manifest(f1)))

    def test_product_row_guard_against_previous(self):
        f1 = self.feed("f1", "2026-10-21T20:05:22+0000", DAY1)
        prev = self.manifest(f1)
        prev["prices"]["products"] = 4                      # 3 of 4 = 75 % < 90 %
        f2 = self.feed("f2", "2026-10-22T20:04:10+0000", DAY2, publish_feed.DirPrevious(f1))
        errors = self.validate(f2, prev)
        self.assertTrue(any("products" in e for e in errors), errors)
        self.assertEqual(self.validate(f2, prev, allow_shrink=True), [])

    def test_lost_history_fails(self):
        f1, f2 = self.two_days()
        prev = self.manifest(f2)
        lonely = self.feed("lonely", "2026-10-23T20:00:00+0000", DAY2)   # bootstrapped by mistake
        errors = self.validate(lonely, prev)
        self.assertTrue(any("history shrank" in e for e in errors), errors)
        self.assertTrue(any("daily snapshots lost" in e for e in errors), errors)

    def test_history_series_length_is_checked(self):
        f1 = self.feed("f1", "2026-10-21T20:05:22+0000", DAY1)
        path = os.path.join(f1, "history", "base1.json")
        with open(path) as f:
            h = json.load(f)
        h["s"]["42382.1"].append(1)
        with open(path, "w") as f:
            json.dump(h, f)
        self.assertTrue(any("wrong length" in e for e in self.validate(f1)))

    def test_site_is_world_readable(self):
        f1 = self.feed("f1", "2026-10-21T20:05:22+0000", DAY1)
        self.assertEqual(os.stat(f1).st_mode & 0o777, 0o755)        # mkdtemp's 0700 would break Pages
        self.assertEqual(os.stat(os.path.join(f1, "history")).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(os.path.join(f1, "prices.json")).st_mode & 0o777, 0o644)
        os.chmod(os.path.join(f1, "prices.json"), 0o600)
        self.assertTrue(any("world-readable" in e for e in self.validate(f1)))

    def test_older_build_than_previous_fails(self):
        f1, f2 = self.two_days()
        errors = self.validate(f1, self.manifest(f2), allow_shrink=True)
        self.assertTrue(any("older" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
