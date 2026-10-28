import json
import unittest

import bundle
import species as sp

FIXTURE = [(3, "Venusaur"), (6, "Charizard"), (25, "Pikachu"), (26, "Raichu"), (29, "Nidoran♀"), (32, "Nidoran♂"),
           (83, "Farfetch’d"), (100, "Voltorb"), (101, "Electrode"), (122, "Mr. Mime"), (137, "Porygon"),
           (150, "Mewtwo"), (151, "Mew"), (194, "Wooper"), (197, "Umbreon"), (201, "Unown"), (233, "Porygon2"),
           (249, "Lugia"), (250, "Ho-Oh"), (390, "Chimchar"), (391, "Monferno"), (439, "Mime Jr."),
           (445, "Garchomp"), (474, "Porygon-Z"), (483, "Dialga"), (484, "Palkia"), (488, "Cresselia"),
           (491, "Darkrai"), (493, "Arceus"), (644, "Zekrom"), (646, "Kyurem"), (654, "Braixen"),
           (669, "Flabébé"), (772, "Type: Null"), (785, "Tapu Koko"), (898, "Calyrex"), (901, "Ursaluna"),
           (1002, "Chien-Pao"), (1006, "Iron Valiant"), (1017, "Ogerpon"), (1024, "Terapagos")]
SPECIES = [{"id": i, "name": n, "gen": sp.generation_for_dex(i), "types": ["Normal"]} for i, n in FIXTURE]
INDEX = sp.SpeciesIndex(SPECIES)


class GenerationTests(unittest.TestCase):
    def test_boundaries(self):
        cases = {1: 1, 151: 1, 152: 2, 251: 2, 252: 3, 386: 3, 387: 4, 493: 4, 494: 5, 649: 5, 650: 6, 721: 6,
                 722: 7, 808: 7, 809: 7, 810: 8, 899: 8, 905: 8, 906: 9, 1025: 9}
        for dex, gen in cases.items():
            with self.subTest(dex=dex):
                self.assertEqual(sp.generation_for_dex(dex), gen)

    def test_outside_the_dex(self):
        for dex in (0, -1, 1026):
            self.assertIsNone(sp.generation_for_dex(dex))

    def test_ranges_are_contiguous_and_cover_1_to_1025(self):
        expect = 1
        for g, (number, roman, region, first, last) in enumerate(sp.GENERATIONS, 1):
            self.assertEqual(number, g)
            self.assertEqual(first, expect)
            self.assertGreaterEqual(last, first)
            expect = last + 1
        self.assertEqual(expect, 1026)
        self.assertEqual([r for _, r, *_ in sp.GENERATIONS], ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX"])


class BuildSpeciesTests(unittest.TestCase):
    TABLES = {
        "pokemon_species": [{"id": "26", "generation_id": "1"}, {"id": "494", "generation_id": "5"},
                            {"id": "152", "generation_id": "1"}],
        "pokemon_species_names": [
            {"pokemon_species_id": "26", "local_language_id": "1", "name": "ライチュウ"},
            {"pokemon_species_id": "26", "local_language_id": "9", "name": "Raichu"},
            {"pokemon_species_id": "494", "local_language_id": "9", "name": "Victini"},
            {"pokemon_species_id": "152", "local_language_id": "9", "name": "Chikorita"}],
        "pokemon": [{"id": "26", "species_id": "26", "is_default": "1"},
                    {"id": "10100", "species_id": "26", "is_default": "0"},
                    {"id": "494", "species_id": "494", "is_default": "1"},
                    {"id": "152", "species_id": "152", "is_default": "1"}],
        "pokemon_types": [{"pokemon_id": "26", "type_id": "13", "slot": "1"},
                          {"pokemon_id": "10100", "type_id": "13", "slot": "1"},
                          {"pokemon_id": "10100", "type_id": "14", "slot": "2"},
                          {"pokemon_id": "494", "type_id": "10", "slot": "2"},
                          {"pokemon_id": "494", "type_id": "14", "slot": "1"},
                          {"pokemon_id": "152", "type_id": "12", "slot": "1"}],
        "types": [{"id": "10", "identifier": "fire"}, {"id": "12", "identifier": "grass"},
                  {"id": "13", "identifier": "electric"}, {"id": "14", "identifier": "psychic"}],
    }

    def test_english_name_default_form_and_slot_order(self):
        out, problems = sp.build_species(self.TABLES)
        by = {s["id"]: s for s in out}
        self.assertEqual([s["id"] for s in out], [26, 152, 494])
        self.assertEqual(by[26], {"id": 26, "name": "Raichu", "gen": 1, "types": ["Electric"]})   # not Alolan
        self.assertEqual(by[494]["types"], ["Psychic", "Fire"])
        self.assertEqual(by[494]["gen"], 5)
        # PokéAPI says generation 1 for #152: reported, not silently accepted
        self.assertEqual(problems, ["152: PokéAPI generation 1, expected 2"])


class NameRuleTests(unittest.TestCase):
    def check(self, cases):
        for name, want in cases:
            with self.subTest(name=name):
                self.assertEqual(INDEX.find(name), want)

    def test_prefixes_and_owners(self):
        self.check([("Dark Charizard", [6]), ("Team Rocket's Mewtwo ex", [150]), ("Radiant Charizard", [6]),
                    ("Alolan Raichu", [26]), ("Ash's Pikachu", [25]), ("_____'s Pikachu", [25]),
                    ("Paldean Wooper", [194]), ("Hisuian Voltorb", [100]), ("Galarian Mr. Mime", [122]),
                    ("Black Kyurem EX", [646]), ("Shadow Rider Calyrex VMAX", [898]),
                    ("Bloodmoon Ursaluna ex", [901]), ("Teal Mask Ogerpon ex", [1017])])

    def test_mechanic_suffixes_and_symbols(self):
        self.check([("Mega Charizard X ex", [6]), ("M Charizard-EX", [6]), ("Charizard VMAX", [6]),
                    ("Charizard VSTAR", [6]), ("Charizard-GX", [6]), ("Charizard δ", [6]),
                    ("Charizard (Delta Species)", [6]), ("Charizard G LV.X", [6]), ("Charizard Lv.X", [6]),
                    ("Charizard BREAK", [6]), ("Garchomp ◇", [445]), ("Umbreon ☆", [197]), ("Umbreon ★", [197]),
                    ("Lugia LEGEND", [249]), ("Unown [A]", [201]), ("Unown A", [201])])

    def test_several_species(self):
        self.check([("Charizard & Braixen-GX", [6, 654]), ("Pikachu & Zekrom-GX", [25, 644]),
                    ("Mewtwo & Mew-GX", [150, 151]), ("Darkrai & Cresselia LEGEND", [491, 488]),
                    ("Arceus & Dialga & Palkia-GX", [493, 483, 484]), ("Celebi & Venusaur-GX", [3])])

    def test_punctuation_and_longest_name(self):
        self.check([("Porygon-Z", [474]), ("Porygon2", [233]), ("Porygon", [137]), ("Mr. Mime", [122]),
                    ("Mime Jr.", [439]), ("Nidoran♀", [29]), ("Nidoran ♂", [32]), ("Farfetch'd", [83]),
                    ("Farfetch’d", [83]), ("Flabébé", [669]), ("Flabebe", [669]), ("Type: Null", [772]),
                    ("Ho-Oh ex", [250]), ("Ho-oh", [250]), ("Tapu Koko-GX", [785]), ("Chien-Pao ex", [1002]),
                    ("Iron Valiant ex", [1006])])

    def test_no_species(self):
        self.check([("Buried Fossil", []), ("Professor Oak", []), ("", []), ("Nidoran", [])])


class CardSpeciesTests(unittest.TestCase):
    def test_tcgdex_dex_id(self):
        self.assertEqual(sp.card_species("x-1", "Charizard ex", [6], INDEX), ([6], "tcgdex"))
        # TCGdex knows species the name cannot show
        self.assertEqual(sp.card_species("svp-500", "Terapagos & Friends", [1024, 25, 906], INDEX),
                         ([1024, 25, 906], "tcgdex"))
        self.assertEqual(sp.card_species("ecard3-47", "Buried Fossil", [138, 140, 142], INDEX),
                         ([138, 140, 142], "tcgdex"))

    def test_disjoint_name_corrects_tcgdex(self):
        self.assertEqual(sp.card_species("2021swsh-12", "Chimchar", [391], INDEX), ([390], "name-corrected"))
        self.assertEqual(sp.card_species("mfb-22", "Electrode", [130], INDEX), ([101], "name-corrected"))

    def test_name_fills_gaps_and_invalid_ids_are_dropped(self):
        self.assertEqual(sp.card_species("x-1", "Dark Charizard", None, INDEX), ([6], "name"))
        self.assertEqual(sp.card_species("x-1", "Dark Charizard", [0, 9999, "6"], INDEX), ([6], "name"))
        self.assertEqual(sp.card_species("x-1", "Pikachu", [25, 25], INDEX), ([25], "tcgdex"))
        self.assertEqual(sp.card_species("x-1", "Buried Fossil", [], INDEX), ([], "none"))

    def test_override_wins(self):
        self.assertEqual(sp.card_species("x-1", "Chimchar", [391], INDEX, {"x-1": [391]}), ([391], "override"))


class RepresentativeTests(unittest.TestCase):
    SETS = {"old": {"releaseDate": "2019-01-01", "official": 100},
            "new": {"releaseDate": "2025-01-01", "official": 100}}

    def card(self, cid, name, rarity="Common", image=True, dx=(6,)):
        set_id, local = cid.rsplit("-", 1)
        return {"id": cid, "setId": set_id, "localId": local, "name": name, "rarity": rarity,
                "image": "https://assets.tcgdex.net/x" if image else None, "dx": list(dx)}

    def pick(self, cards):
        return sp.representative_cards(cards, self.SETS, SPECIES)[6]

    def test_newest_plain_regular_card_with_image(self):
        cards = [self.card("old-4", "Charizard"), self.card("new-6", "Charizard"),
                 self.card("new-7", "Charizard ex", rarity="Double rare"),
                 self.card("new-150", "Charizard", rarity="Illustration rare"),
                 self.card("new-8", "Charizard", image=False)]
        self.assertEqual(self.pick(cards), "new-6")

    def test_regular_rarity_beats_plain_name_and_date(self):
        cards = [self.card("old-4", "Dark Charizard", rarity="Rare"),
                 self.card("new-120", "Charizard", rarity="Special illustration rare")]
        self.assertEqual(self.pick(cards), "old-4")

    def test_secret_numbering_and_tag_teams_are_avoided(self):
        cards = [self.card("new-101", "Charizard", rarity="Rare"), self.card("old-9", "Charizard"),
                 self.card("new-5", "Charizard & Braixen-GX", dx=(6, 654))]
        self.assertEqual(self.pick(cards), "old-9")

    def test_lowest_number_breaks_ties(self):
        self.assertEqual(self.pick([self.card("new-12", "Charizard"), self.card("new-011", "Charizard")]),
                         "new-011")


class SpeciesFileTests(unittest.TestCase):
    def test_shape(self):
        doc = sp.species_file([{"id": 1, "name": "Bulbasaur", "gen": 1, "types": ["Grass", "Poison"]},
                               {"id": 4, "name": "Charmander", "gen": 1, "types": ["Fire"]}], {4: "base1-46"})
        self.assertEqual(doc["types"], ["Fire", "Grass", "Poison"])
        self.assertEqual(doc["species"], [{"id": 1, "n": "Bulbasaur", "g": 1, "t": [1, 2]},
                                          {"id": 4, "n": "Charmander", "g": 1, "t": [0], "rc": "base1-46"}])
        self.assertEqual(len(doc["generations"]), 9)
        self.assertEqual(doc["generations"][8], {"g": 9, "n": "IX", "region": "Paldea", "from": 906, "to": 1025})
        self.assertEqual(doc["v"], 1)


class BundleDxTests(unittest.TestCase):
    def test_dx_only_on_cards_that_have_species(self):
        cards = [{"id": "s-1", "setId": "s", "localId": "1", "name": "Pikachu & Zekrom-GX", "category": "Pokemon",
                  "image": "https://assets.tcgdex.net/en/sm/s/1", "tcgplayer": None, "dx": [25, 644]},
                 {"id": "s-2", "setId": "s", "localId": "2", "name": "Potion", "category": "Trainer",
                  "image": "https://assets.tcgdex.net/en/sm/s/2", "tcgplayer": None}]
        idx = bundle.compact_index([{"id": "sm", "name": "SM"}], [{"id": "s", "name": "S", "series": "sm",
                                                                    "groups": []}], cards, [])
        out = json.loads(bundle.dumps_compact(idx))["cards"]
        self.assertEqual(out[0]["dx"], [25, 644])
        self.assertNotIn("dx", out[1])


if __name__ == "__main__":
    unittest.main()
