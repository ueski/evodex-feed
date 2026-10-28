import json
import unittest

import bundle
import images


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


class TcgplayerUrlTests(unittest.TestCase):
    def test_rewrites_tcgcsv_image_url_to_the_1000px_variant(self):
        self.assertEqual(images.tcgplayer_large("https://tcgplayer-cdn.tcgplayer.com/product/264210_200w.jpg"),
                         "https://tcgplayer-cdn.tcgplayer.com/product/264210_in_1000x1000.jpg")
        self.assertEqual(images.tcgplayer_large("https://tcgplayer-cdn.tcgplayer.com/product/264210_in_800x800.jpg"),
                         "https://tcgplayer-cdn.tcgplayer.com/product/264210_in_1000x1000.jpg")

    def test_falls_back_to_the_product_id(self):
        self.assertEqual(images.tcgplayer_large(None, 654594),
                         "https://tcgplayer-cdn.tcgplayer.com/product/654594_in_1000x1000.jpg")
        self.assertEqual(images.tcgplayer_large("https://example.com/x.jpg", 1),
                         "https://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg")
        self.assertIsNone(images.tcgplayer_large(None, None))

    def test_thumbnail_rule(self):
        self.assertEqual(images.thumbnail_url("https://images.pokemontcg.io/swsh9tg/TG01_hires.png"),
                         "https://images.pokemontcg.io/swsh9tg/TG01.png")
        self.assertEqual(images.thumbnail_url("https://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg"),
                         "https://tcgplayer-cdn.tcgplayer.com/product/1_200w.jpg")
        self.assertEqual(images.thumbnail_url("https://images.scrydex.com/pokemon/me55c-58/large"),
                         "https://images.scrydex.com/pokemon/me55c-58/small")
        self.assertEqual(images.thumbnail_url("https://example.com/a.png"), "https://example.com/a.png")


class ChooseTests(unittest.TestCase):
    A, B = "https://images.pokemontcg.io/a/1_hires.png", "https://tcgplayer-cdn.tcgplayer.com/product/1_in_1000x1000.jpg"

    def test_order_and_head_status(self):
        cands = [("ptcgio", self.A), ("tcgplayer", self.B)]
        self.assertEqual(images.choose(cands, {self.A: 200, self.B: 200}.get), ("ptcgio", self.A))
        self.assertEqual(images.choose(cands, {self.A: 404, self.B: 200}.get), ("tcgplayer", self.B))
        self.assertEqual(images.choose(cands, {self.A: 404, self.B: 403}.get), (None, None))

    def test_unchecked_url_is_accepted_and_missing_candidates_skipped(self):
        self.assertEqual(images.choose([("ptcgio", None), ("tcgplayer", self.B)], {}.get), ("tcgplayer", self.B))
        self.assertEqual(images.choose([("ptcgio", None), ("tcgplayer", None)], {}.get), (None, None))


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
