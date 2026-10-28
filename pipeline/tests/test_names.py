import unittest

import run
from normalize import clean_card_name, has_foreign_script, name_key


class CleanCardNameTests(unittest.TestCase):
    def test_bracketed_english_name_wins(self):
        # QA-17: TCGdex xy12-109
        self.assertEqual(clean_card_name("ナッシー[Exeggutor]"), "Exeggutor")
        self.assertEqual(clean_card_name("ピカチュウ（Pikachu）"), "Pikachu")
        self.assertEqual(clean_card_name("【Mew】ミュウ"), "Mew")
        self.assertEqual(clean_card_name("ナッシー [ Alolan  Exeggutor ]"), "Alolan Exeggutor")

    def test_foreign_runs_are_dropped(self):
        self.assertEqual(clean_card_name("Pikachu ピカチュウ"), "Pikachu")
        self.assertEqual(clean_card_name("Pikachu (ピカチュウ)"), "Pikachu")
        self.assertEqual(clean_card_name("이상해씨 Bulbasaur"), "Bulbasaur")

    def test_nothing_latin_left_keeps_the_name(self):
        self.assertEqual(clean_card_name("ナッシー"), "ナッシー")

    def test_real_english_names_are_unchanged(self):
        for name in ["Unown [A]", "Ancient Technical Machine [Ice]", "Chimecho δ", "Umbreon ☆", "Espeon ★",
                     "Garchomp ◇", "Nidoran♀", "Nidoran♂", "Miracle Sphere α", "Rotom Dex—Poké Finder Mode",
                     "Farfetch’d", "Professor's Research (Professor Oak)", "Flabébé", "PokéDex (HANDY909)",
                     "Imakuni?'s Doduo", "_____'s Pikachu", "", None]:
            with self.subTest(name=name):
                self.assertEqual(clean_card_name(name), name)

    def test_foreign_script_detection(self):
        self.assertTrue(has_foreign_script("ナッシー"))
        self.assertTrue(has_foreign_script("ー"))
        self.assertTrue(has_foreign_script("皮卡丘"))
        self.assertFalse(has_foreign_script("Chimecho δ"))
        self.assertFalse(has_foreign_script("Umbreon ☆ Flabébé"))

    def test_fixed_name_is_matchable(self):
        # the raw name had an empty name_key, so the matcher could never pair it with a product
        self.assertEqual(name_key("ナッシー[Exeggutor]"), "")
        self.assertEqual(name_key(clean_card_name("ナッシー[Exeggutor]")), "exeggutor")


class FixNamesTests(unittest.TestCase):
    def sets(self):
        return [
            {"id": "xy12", "digital": False, "cards": [
                {"id": "xy12-109", "localId": "109", "name": "ナッシー[Exeggutor]"},
                {"id": "xy12-110", "localId": "110", "name": "Flying Pikachu"}]},
            {"id": "x", "digital": False, "cards": [
                {"id": "x-1", "localId": "1", "name": "ピカチュウ"},
                {"id": "x-2", "localId": "2", "name": "ミュウ"}]},
            {"id": "A1", "digital": True, "cards": [{"id": "A1-1", "localId": "1", "name": "フシギダネ"}]},
        ]

    def test_rule_override_and_unresolved(self):
        sets = self.sets()
        details = {"xy12-109": {"name": "ナッシー[Exeggutor]"}, "xy12-110": {"name": "Flying Pikachu"},
                   "x-1": {"name": "ピカチュウ"}}
        rows = run.fix_names(sets, details, {"x-1": "Pikachu"})
        self.assertEqual(rows, [["xy12-109", "ナッシー[Exeggutor]", "Exeggutor", "rule"],
                                ["x-1", "ピカチュウ", "Pikachu", "override"],
                                ["x-2", "ミュウ", "ミュウ", "unresolved"]])
        # both the set brief (used by matching) and the card detail (used by the record) are fixed
        self.assertEqual(sets[0]["cards"][0]["name"], "Exeggutor")
        self.assertEqual(details["xy12-109"]["name"], "Exeggutor")
        self.assertEqual(sets[1]["cards"][0]["name"], "Pikachu")
        self.assertEqual(details["x-1"]["name"], "Pikachu")
        # untouched: English names, unresolved names, digital sets
        self.assertEqual(sets[0]["cards"][1]["name"], "Flying Pikachu")
        self.assertEqual(sets[1]["cards"][1]["name"], "ミュウ")
        self.assertEqual(sets[2]["cards"][0]["name"], "フシギダネ")

    def test_brief_only_foreign_name_is_fixed_for_matching(self):
        sets = [{"id": "s", "digital": False, "cards": [
            {"id": "s-1", "localId": "1", "name": "ナッシー[Exeggutor]"},
            {"id": "s-2", "localId": "2", "name": "Pikachu"}]}]
        details = {"s-1": {"name": "Exeggutor"}, "s-2": {"name": "Pikachu ex"}}
        self.assertEqual(run.fix_names(sets, details, {}), [])
        self.assertEqual(sets[0]["cards"][0]["name"], "Exeggutor")
        self.assertEqual(sets[0]["cards"][1]["name"], "Pikachu", "an English brief is never rewritten")

    def test_override_pins_a_name_that_is_already_english(self):
        sets = self.sets()
        rows = run.fix_names(sets, {}, {"xy12-110": "Flying Pikachu"})
        self.assertNotIn("xy12-110", [r[0] for r in rows])


if __name__ == "__main__":
    unittest.main()
