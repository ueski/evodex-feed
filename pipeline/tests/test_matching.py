import unittest

import matching
import setmap
import variants


def product(pid, name, number=None, rarity="Common", group=1, extra=None):
    ed = []
    if number is not None:
        ed.append({"name": "Number", "value": number})
    if rarity is not None:
        ed.append({"name": "Rarity", "value": rarity})
    ed.extend(extra or [])
    return {"productId": pid, "groupId": group, "name": name, "extendedData": ed}


def card(cid, local_id, name):
    return {"id": cid, "localId": local_id, "name": name}


def run(cards, raw_products, **kw):
    return matching.match_set(cards, matching.card_products(raw_products), **kw)


def primary(res, cid):
    return next(p["productId"] for p in res[cid]["products"] if p["primary"])


class SealedTests(unittest.TestCase):
    def test_skips_sealed_and_code_cards(self):
        raw = [product(1, "Pikachu", "025/165"),
               product(2, "Scarlet & Violet 151 Booster Box", None, None,
                       extra=[{"name": "UPC", "value": "123"}]),
               product(3, "Code Card - 151 Booster Pack", None, "Code Card"),
               product(4, "Energy (unnumbered)", None, "Common")]
        ids = [p["productId"] for p in matching.card_products(raw)]
        self.assertEqual(ids, [1, 4])


class MatchSetTests(unittest.TestCase):
    def test_number_and_leading_zeros(self):
        res, left = run([card("s-25", "025", "Pikachu"), card("s-1", "1", "Bulbasaur")],
                        [product(10, "Pikachu", "025/165"), product(11, "Bulbasaur", "001/165")])
        self.assertEqual(primary(res, "s-25"), 10)
        self.assertEqual(primary(res, "s-1"), 11)
        self.assertEqual(res["s-25"]["method"], "number+name")
        self.assertEqual(left, [])

    def test_trainer_gallery_and_shiny_vault_prefixes(self):
        res, _ = run([card("tg-TG01", "TG01", "Flareon"), card("sv-SV001", "SV001", "Rowlet")],
                     [product(20, "Flareon", "TG01/TG30"), product(21, "Rowlet", "SV001/SV122")])
        self.assertEqual(primary(res, "tg-TG01"), 20)
        self.assertEqual(primary(res, "sv-SV001"), 21)

    def test_name_breaks_ties_between_same_number(self):
        # two trainer-kit halves share a TCGplayer group and both have "#4"
        res, left = run([card("a-4", "4", "Switch")],
                        [product(30, "Bunnelby", "4/30"), product(31, "Switch (Sylveon)", "4/30")])
        self.assertEqual(primary(res, "a-4"), 31)
        self.assertEqual([p["productId"] for p in left], [30])

    def test_number_only_needs_some_name_overlap(self):
        # renumbered reprint set: same key, unrelated names -> no match
        res, _ = run([card("c-4", "004", "Genesect EX")], [product(40, "Charizard Classic Collection", "4/102")])
        self.assertNotIn("c-4", res)

    def test_alternate_printings_and_primary(self):
        res, _ = run([card("p-SWSH020", "SWSH020", "Pikachu")],
                     [product(52, "Pikachu - SWSH020 (Prerelease)", "SWSH020"),
                      product(50, "Pikachu - SWSH020", "SWSH020"),
                      product(51, "Pikachu - SWSH020 (Staff)", "SWSH020")])
        prods = res["p-SWSH020"]["products"]
        self.assertEqual(len(prods), 3)
        self.assertEqual(primary(res, "p-SWSH020"), 50)

    def test_name_fallback_for_foreign_numbering(self):
        cards = [card("cc-CC001", "CC001", "Blastoise"), card("cc-CC002", "CC002", "Charizard"),
                 card("cc-CC015", "CC015", "Umbreon ☆")]
        raw = [product(60, "Blastoise Classic Collection", "2/102"),
               product(61, "Charizard Classic Collection", "4/102"),
               product(62, "Umbreon Star Classic Collection", "17/17")]
        res, _ = run(cards, raw)
        self.assertEqual(primary(res, "cc-CC001"), 60)
        self.assertEqual(primary(res, "cc-CC002"), 61)
        self.assertEqual(primary(res, "cc-CC015"), 62)
        self.assertEqual(res["cc-CC001"]["method"], "name")

    def test_name_fallback_respects_digits_when_numbering_is_native(self):
        cards = [card("s-%d" % i, str(i), "Mon%d" % i) for i in range(1, 5)] + [card("s-29", "29", "Switch")]
        raw = [product(70 + i, "Mon%d" % i, "%d/30" % i) for i in range(1, 5)]
        raw.append(product(80, "Switch (Noivern)", "4/30"))   # TCGplayer typo: should be 29/30
        res, _ = run(cards, raw)
        self.assertNotIn("s-29", res)           # guarded: digits 4 != 29
        res, _ = run(cards, raw, card_overrides={"s-29": [80]})
        self.assertEqual(primary(res, "s-29"), 80)
        self.assertEqual(res["s-29"]["method"], "override")

    def test_name_fallback_without_numbers_prefers_unqualified(self):
        cards = [card("m-1", "1", "Bulbasaur"), card("m-2", "2", "Ivysaur"), card("m-3", "3", "Oddish")]
        raw = [product(91, "Bulbasaur (Blue Border)", None, "Unconfirmed"),
               product(90, "Bulbasaur", None, "Unconfirmed"),
               product(92, "Ivysaur", None, "Unconfirmed")]
        res, _ = run(cards, raw)
        self.assertEqual(primary(res, "m-1"), 90)
        self.assertEqual(primary(res, "m-2"), 92)
        self.assertNotIn("m-3", res)

    def test_filter_pool(self):
        pool = matching.card_products([product(1, "Switch (Sylveon)", "4/30"),
                                       product(2, "Switch (Noivern)", "4/30")])
        kept = matching.filter_pool(pool, exclude=["noivern"])
        self.assertEqual([p["productId"] for p in kept], [1])
        kept = matching.filter_pool(pool, include=["Sylveon"])
        self.assertEqual([p["productId"] for p in kept], [1])

    def test_unown_letters(self):
        res, _ = run([card("exu-A", "A", "Unown"), card("exu-%3F", "%3F", "Unown")],
                     [product(1, "Unown (A)", "A/28"), product(2, "Unown (?)", "?/28")])
        self.assertEqual(primary(res, "exu-A"), 1)
        self.assertEqual(primary(res, "exu-%3F"), 2)


class VariantTests(unittest.TestCase):
    def test_labels(self):
        prods = [{"productId": 1, "groupId": 604, "qualifier": "", "primary": True},
                 {"productId": 2, "groupId": 1663, "qualifier": "", "primary": True},
                 {"productId": 3, "groupId": 604, "qualifier": "Black Dot Error", "primary": False}]
        prices = {1: [{"subTypeName": "Holofoil", "marketPrice": 10.0}],
                  2: [{"subTypeName": "Unlimited Holofoil", "marketPrice": 20.0},
                      {"subTypeName": "1st Edition Holofoil", "marketPrice": None}]}
        vs = variants.build_variants(prods, prices, {1663: "Shadowless"})
        self.assertEqual([v["label"] for v in vs],
                         ["Holofoil", "Shadowless 1st Edition Holofoil", "Shadowless Unlimited Holofoil",
                          "Unpriced (Black Dot Error)"])
        self.assertEqual(vs[0]["marketPrice"], 10.0)
        self.assertIsNone(vs[3]["subType"])


class SetMapTests(unittest.TestCase):
    def test_auto_mapping_by_name_date_and_numbers(self):
        sets = [{"id": "sv03.5", "name": "151", "releaseDate": "2023-09-22"},
                {"id": "svp", "name": "SVP Black Star Promos", "releaseDate": "2023-03-31"},
                {"id": "sv01", "name": "Scarlet & Violet", "releaseDate": "2023-03-31"}]
        groups = [{"groupId": 1, "name": "SV: Scarlet & Violet 151", "publishedOn": "2023-09-22T00:00:00"},
                  {"groupId": 2, "name": "SV: Scarlet & Violet Promo Cards", "publishedOn": "2023-03-31T00:00:00"},
                  {"groupId": 3, "name": "SV01: Scarlet & Violet Base Set", "publishedOn": "2023-03-31T00:00:00"}]
        set_keys = {"sv03.5": {str(i) for i in range(1, 166)}, "svp": {str(i) for i in range(1, 100)},
                    "sv01": {str(i) for i in range(1, 199)}}
        group_keys = {1: {str(i) for i in range(1, 166)}, 2: {str(i) for i in range(1, 98)},
                      3: {str(i) for i in range(1, 199)}}
        mapping, rep = setmap.build_mapping(sets, set_keys, groups, group_keys, {})
        self.assertEqual(mapping["sv03.5"]["groups"], [1])
        self.assertEqual(mapping["sv01"]["groups"], [3])
        self.assertEqual(mapping["svp"]["groups"], [2])   # by numbers + same week
        self.assertEqual(rep["unmatchedSets"], [])

    def test_override_wins_and_allows_one_to_many(self):
        sets = [{"id": "base1", "name": "Base Set", "releaseDate": "1999-01-09"}]
        groups = [{"groupId": 604, "name": "Base Set", "publishedOn": "1999-01-09"},
                  {"groupId": 1663, "name": "Base Set (Shadowless)", "publishedOn": "1999-01-09"}]
        ov = {"base1": {"groups": [604, 1663], "labels": {"1663": "Shadowless"}}}
        mapping, rep = setmap.build_mapping(sets, {"base1": {"1"}}, groups, {604: {"1"}, 1663: {"1"}}, ov)
        self.assertEqual(mapping["base1"]["groups"], [604, 1663])
        self.assertEqual(mapping["base1"]["labels"], {1663: "Shadowless"})
        self.assertEqual(rep["unmatchedGroups"], [])

    def test_date_score(self):
        self.assertEqual(setmap.date_score("2023-03-31", "2023-04-03T00:00:00"), 1.0)
        self.assertEqual(setmap.date_score("2023-03-31", "2023-05-01"), 0.5)
        self.assertEqual(setmap.date_score("2023-03-31", "2026-10-21"), 0.0)
        self.assertEqual(setmap.date_score(None, "2026-10-21"), 0.0)


if __name__ == "__main__":
    unittest.main()
