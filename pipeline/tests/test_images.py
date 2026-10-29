import json
import os
import shutil
import tempfile
import unittest

import bundle
import images
import net
import run

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ptcg(id_, number, name, large=True):
    set_id = id_.rsplit("-", 1)[0]
    imgs = {"small": "https://images.pokemontcg.io/%s/%s.png" % (set_id, number)}
    if large:
        imgs["large"] = "https://images.pokemontcg.io/%s/%s_hires.png" % (set_id, number)
    return {"id": id_, "number": number, "name": name, "images": imgs}


class SetIdKeyTests(unittest.TestCase):
    def test_tcgdex_and_ptcgio_ids_agree(self):
        pairs = [("sv03.5", "sv3pt5"), ("swsh12.5gg", "swsh12pt5gg"), ("sm3.5", "sm35"),
                 ("swsh4.5sv", "swsh45sv"), ("sm7.5", "sm75"), ("sv01", "sv1"), ("me02.5", "me2pt5"),
                 ("swsh9tg", "swsh9tg"), ("base1", "base1")]
        for dex, other in pairs:
            with self.subTest(dex=dex):
                self.assertEqual(images.set_id_key(dex), images.set_id_key(other))

    def test_distinct_sets_stay_distinct(self):
        self.assertNotEqual(images.set_id_key("sv10"), images.set_id_key("sv1"))
        self.assertNotEqual(images.set_id_key("sm115"), images.set_id_key("sm11"))
        self.assertNotEqual(images.set_id_key("swsh12"), images.set_id_key("swsh12pt5"))


class MapSetsTests(unittest.TestCase):
    PTCG = [
        {"id": "swsh9tg", "name": "Brilliant Stars Trainer Gallery", "releaseDate": "2022/02/25"},
        {"id": "swsh12pt5gg", "name": "Crown Zenith Galarian Gallery", "releaseDate": "2023/01/20"},
        {"id": "mcd21", "name": "McDonald's Collection 2021", "releaseDate": "2021/02/09"},
        {"id": "mcd17", "name": "McDonald's Collection 2017", "releaseDate": "2017/11/07"},
        {"id": "bp", "name": "Best of Game", "releaseDate": "2002/12/01"},
        {"id": "ex10", "name": "Unseen Forces", "releaseDate": "2005/08/01"},
        {"id": "sv1", "name": "Scarlet & Violet", "releaseDate": "2023/03/31"},
    ]

    def run_map(self, dex, overrides=None):
        return images.map_sets(dex, self.PTCG, overrides)

    def test_same_id_after_normalization(self):
        m = self.run_map([{"id": "swsh12.5gg", "name": "Crown Zenith Galarian Gallery", "releaseDate": "2023-01-20"},
                          {"id": "swsh9tg", "name": "Brilliant Stars Trainer Gallery", "releaseDate": "2022-02-25"}])
        self.assertEqual(m["swsh12.5gg"], ("swsh12pt5gg", "id"))
        self.assertEqual(m["swsh9tg"], ("swsh9tg", "id"))

    def test_same_name_and_release_date(self):
        m = self.run_map([{"id": "2021swsh", "name": "McDonald's Collection 2021", "releaseDate": "2021-02-09"},
                          {"id": "bog", "name": "Best of game", "releaseDate": "2002-12-01"}])
        self.assertEqual(m["2021swsh"], ("mcd21", "name"))
        self.assertEqual(m["bog"], ("bp", "name"))

    def test_same_name_but_far_apart_dates_is_not_mapped(self):
        # TCGdex dates McDonald's 2017 three months before pokemontcg.io does: needs the override
        m = self.run_map([{"id": "2017sm", "name": "McDonald's Collection 2017", "releaseDate": "2017-08-03"}])
        self.assertEqual(m["2017sm"], (None, "none"))

    def test_same_id_with_unrelated_name_and_date_is_not_mapped(self):
        m = self.run_map([{"id": "sv01", "name": "Something Else", "releaseDate": "2010-01-01"}])
        self.assertEqual(m["sv01"], (None, "none"))

    def test_overrides_win_and_null_means_no_counterpart(self):
        m = self.run_map([{"id": "exu", "name": "Unseen Forces Unown Collection", "releaseDate": "2005-08-22"},
                          {"id": "mep", "name": "MEP Black Star Promos", "releaseDate": "2025-09-26"},
                          {"id": "swsh9tg", "name": "Brilliant Stars Trainer Gallery", "releaseDate": "2022-02-25"}],
                         {"exu": "ex10", "mep": None, "swsh9tg": "does-not-exist"})
        self.assertEqual(m["exu"], ("ex10", "override"))
        self.assertEqual(m["mep"], (None, "override"))
        self.assertEqual(m["swsh9tg"], (None, "override"))


class MatchCardsTests(unittest.TestCase):
    def test_prefixed_numbers(self):
        cards = [{"id": "swsh9tg-TG01", "localId": "TG01", "name": "Flareon"},
                 {"id": "swsh4.5sv-SV001", "localId": "SV001", "name": "Rowlet"},
                 {"id": "swsh12.5gg-GG01", "localId": "GG01", "name": "Pikachu"}]
        pool = [ptcg("swsh9tg-TG01", "TG01", "Flareon"), ptcg("swsh45sv-SV001", "SV001", "Rowlet"),
                ptcg("swsh12pt5gg-GG01", "GG01", "Pikachu")]
        got = images.match_cards(cards, pool)
        self.assertEqual(images.large_image(got["swsh9tg-TG01"]), "https://images.pokemontcg.io/swsh9tg/TG01_hires.png")
        self.assertEqual(got["swsh4.5sv-SV001"]["id"], "swsh45sv-SV001")
        self.assertEqual(got["swsh12.5gg-GG01"]["id"], "swsh12pt5gg-GG01")

    def test_renumbered_reprints_match_on_the_printed_number(self):
        # Classic Collection: TCGdex CC002 is printed "4/102"; pokemontcg.io keeps number "4".
        # Four different cards share number 15, so the name decides.
        cards = [{"id": "cel25cc-CC002", "localId": "CC002", "name": "Charizard", "printedNumber": "4/102"},
                 {"id": "cel25cc-CC006", "localId": "CC006", "name": "Here Comes Team Rocket!",
                  "printedNumber": "15/82"},
                 {"id": "cel25cc-CC013", "localId": "CC013", "name": "Rocket's Zapdos", "printedNumber": "15/132"}]
        pool = [ptcg("cel25c-4_A", "4", "Charizard"), ptcg("cel25c-15_A1", "15", "Venusaur"),
                ptcg("cel25c-15_A2", "15", "Here Comes Team Rocket!"),
                ptcg("cel25c-15_A3", "15", "Rocket's Zapdos"), ptcg("cel25c-15_A4", "15", "Claydol")]
        got = images.match_cards(cards, pool)
        self.assertEqual(got["cel25cc-CC002"]["id"], "cel25c-4_A")
        self.assertEqual(got["cel25cc-CC006"]["id"], "cel25c-15_A2")
        self.assertEqual(got["cel25cc-CC013"]["id"], "cel25c-15_A3")

    def test_same_number_different_card_is_rejected(self):
        cards = [{"id": "smp-SM01", "localId": "SM01", "name": "Rowlet"}]
        self.assertEqual(images.match_cards(cards, [ptcg("smp-SM01", "SM01", "Litten")]), {})

    def test_unown_letters_and_percent_encoded_question_mark(self):
        cards = [{"id": "exu-A", "localId": "A", "name": "Unown A"},
                 {"id": "exu-%3F", "localId": "%3F", "name": "Unown ?"}]
        pool = [ptcg("ex10-A", "A", "Unown"), ptcg("ex10-?", "?", "Unown"), ptcg("ex10-1", "1", "Blastoise")]
        got = images.match_cards(cards, pool)
        self.assertEqual(got["exu-A"]["id"], "ex10-A")
        self.assertEqual(got["exu-%3F"]["id"], "ex10-?")

    def test_two_equally_good_candidates_are_skipped(self):
        cards = [{"id": "x-1", "localId": "1", "name": "Pikachu"}]
        pool = [ptcg("y-1a", "1", "Pikachu"), ptcg("y-1b", "001", "Pikachu")]
        self.assertEqual(images.match_cards(cards, pool), {})

    def test_small_image_when_no_large(self):
        self.assertEqual(images.large_image(ptcg("a-1", "1", "X", large=False)), "https://images.pokemontcg.io/a/1.png")


class ThumbnailAndHostTests(unittest.TestCase):
    def test_thumbnail_rule(self):
        self.assertEqual(images.thumbnail_url("https://images.pokemontcg.io/swsh9tg/TG01_hires.png"),
                         "https://images.pokemontcg.io/swsh9tg/TG01.png")
        self.assertEqual(images.thumbnail_url("https://images.scrydex.com/pokemon/me55c-58/large"),
                         "https://images.scrydex.com/pokemon/me55c-58/small")
        self.assertEqual(images.thumbnail_url("https://tcgplayer-cdn.tcgplayer.com/product/118819_in_1000x1000.jpg"),
                         "https://tcgplayer-cdn.tcgplayer.com/product/118819_in_400x400.jpg")
        self.assertEqual(images.thumbnail_url("https://example.com/a.png"), "https://example.com/a.png")

    def test_pokemontcgio_and_scrydex_always_tcgplayer_only_behind_the_switch(self):
        """v0.3 IP decision, re-opened 2026-10-29: TCGplayer's product photos only while images.json switches
        them on, and only in the one shape the pipeline writes (research.md §3.4)."""
        self.assertEqual(images.SOURCES, ("ptcgio", "scrydex", "tcgplayer"))
        self.assertEqual(images.ALLOWED_HOSTS, ("images.pokemontcg.io", "images.scrydex.com"))
        tcgp = "https://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg"
        for switch in (False, True):
            self.assertTrue(images.allowed("https://images.pokemontcg.io/a/1_hires.png", switch))
            self.assertTrue(images.allowed("https://images.scrydex.com/pokemon/me55c-58/large", switch))
            self.assertFalse(images.allowed("http://images.pokemontcg.io/a/1_hires.png", switch))
            self.assertFalse(images.allowed(None, switch))
        self.assertFalse(images.allowed(tcgp))
        self.assertTrue(images.allowed(tcgp, tcgplayer=True))

    def test_tcgplayer_url_has_exactly_one_shape(self):
        self.assertEqual(images.tcgplayer_url(118819),
                         "https://tcgplayer-cdn.tcgplayer.com/product/118819_in_1000x1000.jpg")
        for bad in (None, 0, -5, "118819", True, 1.5):
            self.assertIsNone(images.tcgplayer_url(bad), bad)
        self.assertEqual(images.tcgplayer_product(images.tcgplayer_url(659612)), 659612)
        hostile = [
            "http://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg",
            "https://tcgplayer-cdn.tcgplayer.com/product/1_in_400x400.jpg",
            "https://tcgplayer-cdn.tcgplayer.com/product/1_200w.jpg",
            "https://tcgplayer-cdn.tcgplayer.com/product/01_in_1000x1000.jpg",
            "https://tcgplayer-cdn.tcgplayer.com/product/x_in_1000x1000.jpg",
            "https://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg?x=1",
            "https://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg#f",
            "https://tcgplayer-cdn.tcgplayer.com:8443/product/1_in_1000x1000.jpg",
            "https://user@tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg",
            "https://tcgplayer-cdn.tcgplayer.com@evil.com/product/1_in_1000x1000.jpg",
            "https://evil.com/tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg",
            "https://tcgplayer-cdn.tcgplayer.com.evil.com/product/1_in_1000x1000.jpg",
            "https://TCGPLAYER-CDN.tcgplayer.com/product/1_in_1000x1000.jpg",
            "https://tcgplayer-cdn.tcgplayer.com/product/../1_in_1000x1000.jpg",
            "https://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg\n",
            " https://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg",
            "https://tcgplayer-cdn.tcgplayer.com/product/12345678901_in_1000x1000.jpg",
        ]
        for url in hostile:
            self.assertIsNone(images.tcgplayer_product(url), url)
            self.assertFalse(images.allowed(url, tcgplayer=True), url)
        self.assertIsNone(images.tcgplayer_product(None))
        self.assertTrue(images.is_tcgplayer_host("https://user@tcgplayer-cdn.tcgplayer.com:8443/x"))
        self.assertFalse(images.is_tcgplayer_host("https://tcgplayer-cdn.tcgplayer.com.evil.com/x"))
        self.assertFalse(images.is_tcgplayer_host(None))

    def test_load_config(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)

        def config(text):
            path = os.path.join(tmp, "images.json")
            with open(path, "w") as f:
                f.write(text)
            return path
        self.assertIsNone(images.load_config(os.path.join(tmp, "missing.json")))
        self.assertTrue(images.load_config(config('{"tcgplayerFallback": true, "_comment": "x"}')))
        self.assertIs(images.load_config(config('{"tcgplayerFallback": false}')), False)
        self.assertIs(images.load_config(config('{"_comment": "off"}')), False)
        for bad in ('{"tcgplayerFallback": "true"}', '{"tcgplayerFallback": 1}', '{"tcgplayerFallback": null}',
                    '{"tcgPlayerFallback": true}', '[true]', '{not json'):
            with self.assertRaises(ValueError, msg=bad):
                images.load_config(config(bad))

    def test_repository_switch_is_valid(self):
        """images.json exists only in evodex-feed (feed_sync FEED_ONLY); there it must load, here there is none."""
        value = images.load_config()     # raises ValueError for a broken switch
        if not os.path.exists(images.CONFIG):
            self.assertIsNone(value)

    def test_a_custom_tcgdex_image_base_stays_on_tcgdex(self):
        self.assertTrue(images.allowed_tcgdex("https://assets.tcgdex.net/en/xy/xy4/24a"))
        self.assertFalse(images.allowed_tcgdex("https://images.pokemontcg.io/xy4/24a_hires.png"))
        self.assertFalse(images.allowed_tcgdex("https://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg"))
        self.assertFalse(images.allowed_tcgdex("http://assets.tcgdex.net/en/xy/xy4/24a"))


class ChooseTests(unittest.TestCase):
    A, B = "https://images.pokemontcg.io/a/1_hires.png", "https://images.scrydex.com/pokemon/me55c-4/large"
    TCGP = "https://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg"

    def test_order_and_head_status(self):
        cands = [("ptcgio", self.A), ("ptcgio", self.B)]
        self.assertEqual(images.choose(cands, {self.A: 200, self.B: 200}.get), ("ptcgio", self.A))
        self.assertEqual(images.choose(cands, {self.A: 404, self.B: 200}.get), ("ptcgio", self.B))
        self.assertEqual(images.choose(cands, {self.A: 404, self.B: 403}.get), (None, None))

    def test_unchecked_url_is_accepted_and_missing_candidates_skipped(self):
        self.assertEqual(images.choose([("ptcgio", None), ("ptcgio", self.B)], {}.get), ("ptcgio", self.B))
        self.assertEqual(images.choose([("ptcgio", None)], {}.get), (None, None))

    def test_a_host_off_the_allow_list_is_never_chosen(self):
        self.assertEqual(images.choose([("other", self.TCGP)], {self.TCGP: 200}.get), (None, None))
        self.assertEqual(images.choose([("other", self.TCGP), ("ptcgio", self.A)], {}.get), ("ptcgio", self.A))
        self.assertEqual(images.choose([("other", "https://example.com/a.png")], {}.get, tcgplayer=True), (None, None))

    def test_tcgplayer_comes_last_needs_the_switch_and_a_200(self):
        cands = [("ptcgio", self.A), ("tcgplayer", self.TCGP)]
        self.assertEqual(images.choose(cands, {self.A: 404, self.TCGP: 200}.get, tcgplayer=True),
                         ("tcgplayer", self.TCGP))
        self.assertEqual(images.choose(cands, {self.A: 200, self.TCGP: 200}.get, tcgplayer=True), ("ptcgio", self.A))
        self.assertEqual(images.choose(cands, {self.A: 404, self.TCGP: 200}.get), (None, None), "switch off")
        self.assertEqual(images.choose(cands, {self.A: 404}.get, tcgplayer=True), (None, None), "never checked")
        self.assertEqual(images.choose(cands, {self.A: 404, self.TCGP: 403}.get, tcgplayer=True), (None, None),
                         "TCGplayer answers 403 for a product without a photo")


class TCGplayerStockPictureTests(unittest.TestCase):
    """A TCGplayer file that differently named cards share is a stock picture; one card's photo under several
    products (a trainer kit's Lightning Energy at ten numbers) is a real image."""

    def test_stock_etags(self):
        u = images.tcgplayer_url
        status = {u(1): [200, 0, "", '"energy"'], u(2): [200, 0, "", '"energy"'], u(3): [200, 0, "", '"energy"'],
                  u(4): [200, 0, "", '"stock"'], u(5): [200, 0, "", '"stock"'], u(6): [200, 0, "", '"mew"'],
                  u(7): [200, 0],                     # recorded before ETags were kept
                  u(8): [403, 0, "", ""], u(9): [200, 0, "", '"stock"'],
                  "https://images.scrydex.com/pokemon/x/large": [200, 0, "", '"stock"']}
        names = {1: "Lightning Energy", 2: "Lightning Energy", 3: "Lightning Energy", 4: "Pikachu",
                 5: "Bulbasaur", 6: "Ancient Mew", 7: "Pikachu", 8: "Pikachu"}
        self.assertEqual(images.stock_etags(status, names), {'"stock"'})
        self.assertEqual(images.stock_etags(status, {1: "Pokémon Collector", 2: "Pokemon Collector"}), set(),
                         "compared by name_key")
        self.assertEqual(images.stock_etags({}, names), set())


class TCGplayerImageProblemsTests(unittest.TestCase):
    TCGP = "https://tcgplayer-cdn.tcgplayer.com/product/%d_in_1000x1000.jpg"

    def card(self, im, i=0, pid=42382):
        c = {"s": "mep", "l": "023", "n": "X", "i": i, "im": im}
        if pid:
            c["p"] = [{"id": pid}, {"id": 9}]
        return c

    def test_switch_shape_tcgdex_image_and_product(self):
        good = [self.card(self.TCGP % 42382), self.card("https://images.pokemontcg.io/a/1_hires.png", pid=None)]
        self.assertEqual(images.tcgplayer_image_problems(good, True), [])
        self.assertEqual(images.tcgplayer_image_problems(good[1:], False), [])
        self.assertEqual(len(images.tcgplayer_image_problems(good, False)), 1)
        self.assertIn("switch is off", images.tcgplayer_image_problems(good, False)[0])
        cases = {"TCGdex image": [dict(self.card(self.TCGP % 42382), i="https://assets.tcgdex.net/en/me/mep/023")],
                 "not of the form": [self.card("https://tcgplayer-cdn.tcgplayer.com/product/42382_200w.jpg")],
                 "first product": [self.card(self.TCGP % 9)]}
        for needle, cards in cases.items():
            problems = images.tcgplayer_image_problems(cards, True)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn(needle, problems[0])
        self.assertEqual(len(images.tcgplayer_image_problems([self.card(self.TCGP % 42382, pid=None)], True)), 1)


class McDonaldsMappingTests(unittest.TestCase):
    """Regression (v0.3.1): every McDonald's Collection pokemontcg.io has maps, with the real overrides.json.
    Set names and dates as TCGdex and pokemontcg.io list them on 2026-10-29."""
    DEX = [("2011bw", "McDonald's Collection 2011", "2011-06-17"), ("2012bw", "McDonald's Collection 2012", "2012-06-15"),
           ("2014xy", "McDonald's Collection 2014", "2014-05-23"), ("2015xy", "McDonald's Collection 2015", "2015-11-27"),
           ("2016xy", "McDonald's Collection 2016", "2016-08-20"), ("2017sm", "McDonald's Collection 2017", "2017-08-03"),
           ("2018sm", "McDonald's Collection 2018", "2018-10-19"), ("2019sm", "McDonald's Collection 2019", "2019-10-15"),
           ("2021swsh", "McDonald's Collection 2021", "2021-02-09"),
           ("2022swsh", "McDonald's Collection 2022", "2022-08-03"),
           ("2023sv", "McDonald's Collection 2023", "2023-08-01"), ("2024sv", "McDonald's Collection 2024", "2024-12-04")]
    PTCG = [("mcd11", "McDonald's Collection 2011", "2011/06/17"), ("mcd12", "McDonald's Collection 2012", "2012/06/15"),
            ("mcd14", "McDonald's Collection 2014", "2014/05/23"), ("mcd15", "McDonald's Collection 2015", "2015/11/27"),
            ("mcd16", "McDonald's Collection 2016", "2016/08/19"), ("mcd17", "McDonald's Collection 2017", "2017/11/07"),
            ("mcd18", "McDonald's Collection 2018", "2018/10/16"), ("mcd19", "McDonald's Collection 2019", "2019/10/15"),
            ("mcd21", "McDonald's Collection 2021", "2021/02/09"), ("mcd22", "McDonald's Collection 2022", "2022/08/03")]

    def test_every_mcdonalds_set_maps(self):
        with open(os.path.join(HERE, "overrides.json"), encoding="utf-8") as f:
            ov = {k: v for k, v in json.load(f)["ptcgioSets"].items() if not k.startswith("_")}
        got = images.map_sets([{"id": i, "name": n, "releaseDate": d} for i, n, d in self.DEX],
                              [{"id": i, "name": n, "releaseDate": d} for i, n, d in self.PTCG], ov)
        want = {"2011bw": "mcd11", "2012bw": "mcd12", "2014xy": "mcd14", "2015xy": "mcd15", "2016xy": "mcd16",
                "2017sm": "mcd17", "2018sm": "mcd18", "2019sm": "mcd19", "2021swsh": "mcd21", "2022swsh": "mcd22",
                "2023sv": None, "2024sv": None}      # pokemontcg.io has no 2023 / 2024 set
        self.assertEqual({k: v[0] for k, v in got.items()}, want)


class ScrydexCandidateTests(unittest.TestCase):
    def test_scrydex_file_of_the_same_pokemontcgio_id_comes_second(self):
        # images.pokemontcg.io/mcd14/2_hires.png answers 404; Scrydex has the card under the same id
        self.assertEqual(images.candidates(ptcg("mcd14-2", "2", "Chespin")),
                         [("ptcgio", "https://images.pokemontcg.io/mcd14/2_hires.png"),
                          ("scrydex", "https://images.scrydex.com/pokemon/mcd14-2/large")])
        self.assertEqual(images.scrydex_url({"id": "xyp-XY39"}), "https://images.scrydex.com/pokemon/xyp-XY39/large")
        self.assertEqual(images.scrydex_url({"id": "ex10-?"}), "https://images.scrydex.com/pokemon/ex10-%3F/large")
        self.assertIsNone(images.scrydex_url({}))

    def test_no_duplicate_when_the_record_already_points_at_scrydex(self):
        p = {"id": "me55c-11", "number": "11", "name": "X",
             "images": {"large": "https://images.scrydex.com/pokemon/me55c-11/large"}}
        self.assertEqual(images.candidates(p), [("ptcgio", "https://images.scrydex.com/pokemon/me55c-11/large")])

    def test_a_derived_scrydex_url_needs_a_200(self):
        a, b = "https://images.pokemontcg.io/mcd14/2_hires.png", "https://images.scrydex.com/pokemon/mcd14-2/large"
        cands = [("ptcgio", a), ("scrydex", b)]
        self.assertEqual(images.choose(cands, {a: 404, b: 200}.get), ("scrydex", b))
        self.assertEqual(images.choose(cands, {a: 404}.get), (None, None))          # never checked: not used
        self.assertEqual(images.choose(cands, {a: 404, b: 404}.get), (None, None))  # placeholder recorded as 404
        self.assertEqual(images.choose(cands, {}.get), ("ptcgio", a))               # a record's own URL may be unchecked

    def test_placeholder_is_recognised_by_the_probe_etag(self):
        probe = {"etag": '"cfl2loWl"', "content-length": "186316", "content-type": "image/png"}
        sig = images.placeholder_signature(probe)
        self.assertEqual(sig, ("etag", '"cfl2loWl"'))
        self.assertTrue(images.is_placeholder({"etag": '"cfl2loWl"', "content-length": "186316"}, sig))
        self.assertFalse(images.is_placeholder({"etag": '"cfw4hl4A"', "content-length": "306615"}, sig))
        self.assertFalse(images.is_placeholder({"etag": '"cfl2loWl"'}, None))
        by_size = images.placeholder_signature({"content-length": "186316"})
        self.assertTrue(images.is_placeholder({"content-length": "186316"}, by_size))
        self.assertIsNone(images.placeholder_signature({}))


class CheckImageUrlsTests(unittest.TestCase):
    """run.check_image_urls with a fake HEAD: Scrydex's card-back placeholder is a 404, not an image."""
    PROBE = images.PLACEHOLDER_PROBES["images.scrydex.com"]
    REAL = "https://images.scrydex.com/pokemon/mcd14-2/large"
    UNKNOWN = "https://images.scrydex.com/pokemon/hsp-HGSS18/large"
    PTCG = "https://images.pokemontcg.io/mcd14/2_hires.png"

    def run_check(self, responses):
        calls = []

        def fake_head(url, limiter=None, retries=3, timeout=30):
            calls.append(url)
            return responses[url]
        original = net.http_head
        net.http_head = fake_head
        try:
            status = {}
            n = run.check_image_urls([self.REAL, self.UNKNOWN, self.PTCG], status, offline=False, workers=1)
        finally:
            net.http_head = original
        return n, status, calls

    def test_placeholder_becomes_404(self):
        placeholder = (200, {"etag": '"card-back"', "content-length": "186316"})
        n, status, calls = self.run_check({self.PROBE: placeholder, self.UNKNOWN: placeholder,
                                           self.REAL: (200, {"etag": '"chespin"', "content-length": "306615"}),
                                           self.PTCG: (404, {})})
        self.assertEqual(n, 3)
        self.assertEqual(calls[0], self.PROBE)
        self.assertEqual(status[self.REAL][0], 200)
        self.assertEqual(status[self.UNKNOWN][0], 404)
        self.assertEqual(status[self.UNKNOWN][2], "placeholder")
        self.assertEqual(status[self.PTCG][0], 404)

    def test_failed_probe_leaves_that_host_unchecked(self):
        n, status, _ = self.run_check({self.PROBE: (503, {}), self.PTCG: (404, {})})
        self.assertEqual(n, 1)
        self.assertEqual(set(status), {self.PTCG})

    def test_tcgplayer_heads_keep_the_etag(self):
        tcgp, gone = images.tcgplayer_url(118819), images.tcgplayer_url(98030)
        old = images.tcgplayer_url(1)
        responses = {tcgp: (200, {"etag": '"9301656c"'}), gone: (403, {}), old: (200, {"etag": '"e"'})}
        original = net.http_head
        net.http_head = lambda url, limiter=None, retries=3, timeout=30: responses[url]
        try:
            status = {old: [200, 2000000000]}    # a 200 without its ETag is checked again
            n = run.check_image_urls([tcgp, gone, old], status, offline=False, workers=1)
        finally:
            net.http_head = original
        self.assertEqual(n, 3)
        self.assertEqual(status[tcgp][0], 200)
        self.assertEqual(status[tcgp][2:], ["", '"9301656c"'])
        self.assertEqual(status[gone][0], 403)
        self.assertEqual(status[old][3], '"e"')


class TwinImageTests(unittest.TestCase):
    TCGDEX = "https://assets.tcgdex.net/en/xy/%s/%s"

    def card(self, cid, name, products, image=None):
        return {"id": cid, "localId": cid.split("-", 1)[1], "name": name, "image": image, "products": products}

    def test_xya_alternates_take_the_scan_of_their_twin(self):
        # TCGdex lists the XY "a" alternates twice: xya-24a (no scan) and xy4-24a (scan); one TCGplayer product
        cards = [self.card("xya-24a", "M Manectric-EX", [131696]),
                 self.card("xy4-24a", "M Manectric EX", [131696], self.TCGDEX % ("xy4", "24a")),
                 self.card("xy4-24", "M Manectric EX", [99999], self.TCGDEX % ("xy4", "24")),
                 self.card("xya-92a", "Trainers’ Mail", [133815, 133816]),
                 self.card("xy6-92a", "Trainers' Mail", [133815, 133816], self.TCGDEX % ("xy6", "92a"))]
        self.assertEqual(images.twin_images(cards),
                         {"xya-24a": (self.TCGDEX % ("xy4", "24a"), "xy4-24a"),
                          "xya-92a": (self.TCGDEX % ("xy6", "92a"), "xy6-92a")})

    def test_no_twin_without_the_same_product_number_and_name(self):
        img = self.TCGDEX % ("xy4", "24a")
        cases = [
            [self.card("a-24a", "M Manectric-EX", [1]), self.card("b-24a", "M Manectric EX", [2], img)],  # product
            [self.card("a-24b", "M Manectric-EX", [1]), self.card("b-24a", "M Manectric EX", [1], img)],  # number
            [self.card("a-24a", "Pikachu", [1]), self.card("b-24a", "M Manectric EX", [1], img)],         # name
        ]
        for cards in cases:
            with self.subTest(cards=cards):
                self.assertEqual(images.twin_images(cards), {})

    def test_two_equally_good_twins_are_skipped(self):
        cards = [self.card("a-1", "Pikachu", [1]),
                 self.card("b-1", "Pikachu", [1], self.TCGDEX % ("b", "1")),
                 self.card("c-1", "Pikachu", [1], self.TCGDEX % ("c", "1"))]
        self.assertEqual(images.twin_images(cards), {})

    def test_twin_image_is_written_as_a_custom_i(self):
        sets = [{"id": "xya", "name": "Yellow A Alternate", "series": "xy", "groups": []}]
        cards = [{"id": "xya-24a", "setId": "xya", "localId": "24a", "name": "M Manectric-EX",
                  "image": self.TCGDEX % ("xy4", "24a"), "imageTwin": "xy4-24a", "tcgplayer": None}]
        idx = bundle.compact_index([{"id": "xy", "name": "XY"}], sets, cards, [])
        self.assertEqual(idx["cards"][0]["i"], "https://assets.tcgdex.net/en/xy/xy4/24a")
        self.assertNotIn("im", idx["cards"][0])


class BundleFallbackFieldTests(unittest.TestCase):
    def card(self, cid, image, fallback=None):
        set_id, local = cid.split("-", 1)
        c = {"id": cid, "setId": set_id, "localId": local, "name": "X", "image": image, "tcgplayer": None}
        if fallback:
            c["imageFallback"] = {"source": "ptcgio", "url": fallback}
        return c

    def test_im_only_on_cards_without_a_tcgdex_image(self):
        sets = [{"id": "swsh9tg", "name": "BRS TG", "series": "swsh", "groups": []}]
        cards = [
            self.card("swsh9tg-TG01", None, "https://images.pokemontcg.io/swsh9tg/TG01_hires.png"),
            self.card("swsh9tg-TG02", None),
            self.card("swsh9tg-TG03", "https://assets.tcgdex.net/en/swsh/swsh9tg/TG03",
                      "https://images.pokemontcg.io/swsh9tg/TG03_hires.png"),
        ]
        idx = bundle.compact_index([{"id": "swsh", "name": "SWSH"}], sets, cards, [])
        by = {c["l"]: c for c in json.loads(bundle.dumps_compact(idx))["cards"]}
        self.assertEqual(by["TG01"]["i"], 0)
        self.assertEqual(by["TG01"]["im"], "https://images.pokemontcg.io/swsh9tg/TG01_hires.png")
        self.assertEqual(by["TG02"], {"s": "swsh9tg", "l": "TG02", "n": "X", "i": 0})
        self.assertNotIn("i", by["TG03"])
        self.assertNotIn("im", by["TG03"])


if __name__ == "__main__":
    unittest.main()
