import json
import os
import shutil
import tempfile
import unittest

import publish_feed
import validate_feed
from tests.test_publish_feed import CARDS, HOSTILE_TEMPLATES, IMPACT, make_bundle

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

    def rewrite_index(self, feed, mutate):
        """Edit index.json and keep manifest sha256/bytes consistent, so only the image rule can fail."""
        import hashlib
        path = os.path.join(feed, "index.json")
        with open(path) as f:
            index = json.load(f)
        mutate(index)
        blob = json.dumps(index, separators=(",", ":")).encode("utf-8")
        with open(path, "wb") as f:
            f.write(blob)
        m = self.manifest(feed)
        m["index"].update(sha256=hashlib.sha256(blob).hexdigest(), bytes=len(blob))
        with open(os.path.join(feed, "manifest.json"), "w") as f:
            json.dump(m, f)

    def test_tcgplayer_cdn_images_fail_the_build(self):
        f1 = self.feed("f1", "2026-10-21T20:05:22+0000", DAY1)

        def add(urls):
            def mutate(index):
                for card, url in zip(index["cards"], urls):
                    card["i"], card["im"] = 0, url
            return mutate

        self.rewrite_index(f1, add(["https://images.pokemontcg.io/base1/4_hires.png",
                                    "https://images.scrydex.com/pokemon/me55c-58/large"]))
        errors, stats = validate_feed.validate(f1, None, min_cards=1, min_products=1)
        self.assertEqual(errors, [])
        self.assertEqual(stats["imageHosts"], {"images.pokemontcg.io": 1, "images.scrydex.com": 1})
        self.rewrite_index(f1, add(["https://tcgplayer-cdn.tcgplayer.com/product/42382_in_1000x1000.jpg"]))
        errors = self.validate(f1)
        self.assertTrue(any("tcgplayer-cdn.tcgplayer.com" in e for e in errors), errors)
        self.rewrite_index(f1, add(["https://example.com/a.png", "https://images.pokemontcg.io/a/1_hires.png"]))
        self.assertTrue(any("allow-list" in e for e in self.validate(f1)))

    def test_custom_tcgdex_image_base_must_stay_on_tcgdex(self):
        f1 = self.feed("f1", "2026-10-21T20:05:22+0000", DAY1)

        def set_i(url):
            def mutate(index):
                index["cards"][0]["i"] = url
            return mutate

        self.rewrite_index(f1, set_i("https://assets.tcgdex.net/en/xy/xy4/24a"))
        errors, stats = validate_feed.validate(f1, None, min_cards=1, min_products=1)
        self.assertEqual(errors, [])
        self.assertEqual(stats["customImages"], 1)
        self.rewrite_index(f1, set_i("https://tcgplayer-cdn.tcgplayer.com/product/42382_in_1000x1000.jpg"))
        self.assertTrue(any("custom image bases" in e for e in self.validate(f1)))

    def test_cards_are_only_ever_added(self):
        f1, f2 = self.two_days()
        required = publish_feed.required_cards(publish_feed.DirPrevious(f1).index(), {"mep-089"})
        errors = self.validate(f2, self.manifest(f1), required_cards=required)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("mep-089", errors[0])
        # --allow-shrink does not let a card go; overrides.json "removedCards" does
        self.assertEqual(len(self.validate(f2, self.manifest(f1), required_cards=required, allow_shrink=True)), 1)
        required = publish_feed.required_cards(publish_feed.DirPrevious(f1).index(), {"mep-089"},
                                               {"mep-089": "test"})
        self.assertEqual(self.validate(f2, self.manifest(f1), required_cards=required), [])

    def test_first_run_may_carry_history_seeds_only(self):
        seed_bundle = make_bundle(self.path("seed"), "2026-10-20T20:05:22+0000", CARDS, DAY1)
        with open(os.path.join(seed_bundle, "prices-market.json"), "rb") as f:
            seeds = [("2026-10-20", f.read())]
        b = make_bundle(self.path("b1"), "2026-10-21T20:05:22+0000", CARDS, DAY1)
        f1 = self.path("f1")
        publish_feed.build_feed(b, f1, publish_feed.NoPrevious(), seeds=seeds)
        self.assertEqual(self.manifest(f1)["daily"]["dates"], ["2026-10-20", "2026-10-21"])
        self.assertEqual(self.validate(f1, seed_dates=["2026-10-20"]), [])
        self.assertTrue(any("first run" in e for e in self.validate(f1)))

    def rewrite_manifest(self, feed, mutate):
        path = os.path.join(feed, "manifest.json")
        m = self.manifest(feed)
        mutate(m)
        with open(path, "w") as f:
            json.dump(m, f)

    def test_links_are_optional_and_checked(self):
        f1 = self.feed("f1", "2026-10-21T20:05:22+0000", DAY1, links={"tcgplayerAffiliate": IMPACT})
        self.assertEqual(self.validate(f1), [])
        errors, stats = validate_feed.validate(f1, None, min_cards=1, min_products=1)
        self.assertEqual(stats["affiliate"], IMPACT)
        for links in ({"tcgplayerAffiliate": ""}, {"tcgplayerAffiliate": None}, {}, {"future": 1}):
            self.rewrite_manifest(f1, lambda m, links=links: m.update(links=links))
            self.assertEqual(self.validate(f1), [], links)
        self.rewrite_manifest(f1, lambda m: m.pop("links"))
        self.assertEqual(self.validate(f1), [], "a feed without links (every feed before v0.3.1)")

    def test_hostile_or_malformed_links_fail(self):
        f1 = self.feed("f1", "2026-10-21T20:05:22+0000", DAY1)
        for template in HOSTILE_TEMPLATES + [7, ["x"]]:
            self.rewrite_manifest(f1, lambda m, t=template: m.update(links={"tcgplayerAffiliate": t}))
            errors = self.validate(f1)
            self.assertEqual(len(errors), 1, str(template)[:80])
            self.assertIn("links.tcgplayerAffiliate", errors[0])
        for links in ("https://tcgplayer.pxf.io/?u={url}", ["x"], 1):
            self.rewrite_manifest(f1, lambda m, links=links: m.update(links=links))
            self.assertTrue(any("links must be an object" in e for e in self.validate(f1)), links)

    def test_older_build_than_previous_fails(self):
        f1, f2 = self.two_days()
        errors = self.validate(f1, self.manifest(f2), allow_shrink=True)
        self.assertTrue(any("older" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
