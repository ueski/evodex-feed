import json
import os
import shutil
import tempfile
import unittest

import bundle
import same_art

SETS = {"xy1": {"series": "xy", "releaseDate": "2014-02-05"}, "xy0": {"series": "xy", "releaseDate": "2013-11-08"}, "g1": {"series": "xy", "releaseDate": "2016-02-22"},
        "sm1": {"series": "sm", "releaseDate": "2017-02-03"}, "sm2": {"series": "sm", "releaseDate": "2017-05-05"},
        "smp": {"series": "sm", "releaseDate": "2017-02-03"}, "hgss1": {"series": "hgss", "releaseDate": "2010-02-10"},
        "base1": {"series": "base", "releaseDate": "1999-01-09"}, "svp": {"series": "sv", "releaseDate": "2023-01-01"},
        "mep": {"series": "me", "releaseDate": "2025-09-26"}, "swsh4": {"series": "swsh", "releaseDate": "2020-08-14"},
        "2024sv": {"series": "mc", "releaseDate": "2024-12-04"},
        "tk-xy-b": {"series": "tk", "releaseDate": "2014-11-01"}, "tk-sm-r": {"series": "tk", "releaseDate": "2017-04-21"},
        "tk-hs-g": {"series": "tk", "releaseDate": "2010-05-01"}}


def card(cid, name, category="Pokemon", hp=None, illustrator=None, rarity="Common", own=True, shape=((), 0)):
    set_id, local = cid.rsplit("-", 1)
    return {"id": cid, "setId": set_id, "localId": local, "name": name, "category": category, "hp": hp,
            "illustrator": illustrator, "rarity": rarity, "ownImage": own, "needsImage": not own,
            "attackShape": shape}


class EnergyTests(unittest.TestCase):
    def test_kit_energy_takes_the_eras_first_main_set_common_energy(self):
        cards = [card("xy1-139", "Metal Energy", "Energy"), card("g1-82", "Metal Energy", "Energy"),
                 card("sm1-163", "Metal Energy", "Energy", rarity="Secret Rare"),
                 card("sm1-171", "Metal Energy", "Energy"), card("sm2-171", "Metal Energy", "Energy"),
                 card("tk-xy-b-2", "Metal Energy", "Energy", own=False),
                 card("tk-sm-r-2", "Metal Energy", "Energy", own=False)]
        self.assertEqual(same_art.aliases(cards, SETS), {"tk-xy-b-2": ("xy1-139", "energy"),
                                                          "tk-sm-r-2": ("sm1-171", "energy")})

    def test_no_energy_alias_outside_the_checked_eras_or_outside_kits(self):
        cards = [card("hgss1-117", "Water Energy", "Energy"), card("tk-hs-g-2", "Water Energy", "Energy", own=False),
                 card("xy1-134", "Water Energy", "Energy"), card("svp-2", "Water Energy", "Energy", own=False)]
        self.assertEqual(same_art.aliases(cards, SETS), {}, "HGSS energies carry a silhouette the HS kits lack")

    def test_era_series(self):
        self.assertEqual(same_art.era_series("tk-xy-latio", "tk"), "xy")
        self.assertEqual(same_art.era_series("tk-sm-l", "tk"), "sm")
        self.assertEqual(same_art.era_series("tk-hs-g", "tk"), "hgss")
        self.assertEqual(same_art.era_series("mep", "me"), "me")


class ReprintTests(unittest.TestCase):
    SHAPE = (("10", "70"), 0)

    def test_exact_reprint_prefers_the_same_series_then_the_earliest_then_the_id(self):
        cards = [card("sm2-1", "Alolan Raichu", hp=110, illustrator="5ban Graphics", shape=self.SHAPE),
                 card("smp-SM65", "Alolan Raichu", hp=110, illustrator="5ban Graphics", shape=self.SHAPE),
                 card("svp-9", "Alolan Raichu", hp=110, illustrator="5ban Graphics", shape=self.SHAPE),
                 card("tk-sm-r-17", "Alolan Raichu", hp=110, illustrator="5ban Graphics", own=False, shape=self.SHAPE)]
        self.assertEqual(same_art.aliases(cards, SETS), {"tk-sm-r-17": ("smp-SM65", "reprint")})

    def test_every_field_must_match_and_the_illustrator_must_be_known(self):
        base = card("sm1-13", "Grubbin", hp=70, illustrator="Akira Komayama", shape=self.SHAPE)
        for other in (dict(base, hp=60), dict(base, illustrator="Someone"), dict(base, name="Grubbin ex"),
                      dict(base, category="Trainer"), dict(base, attackShape=(("20",), 0)),
                      dict(base, ownImage=False)):
            target = card("tk-sm-r-16", "Grubbin", hp=70, illustrator="Akira Komayama", own=False, shape=self.SHAPE)
            self.assertEqual(same_art.aliases([other, target], SETS), {}, other)
        unknown = [dict(base, illustrator=None), card("tk-sm-r-16", "Grubbin", hp=70, own=False, shape=self.SHAPE)]
        self.assertEqual(same_art.aliases(unknown, SETS), {}, "never without an illustrator")

    def test_a_pokemon_may_come_from_another_series_a_trainer_may_not(self):
        cards = [card("swsh4-25", "Charizard", hp=170, illustrator="Ryuta Fuse", shape=(("100+",), 1)),
                 card("2024sv-1", "Charizard", hp=170, illustrator="Ryuta Fuse", own=False, shape=(("100+",), 1)),
                 card("base1-91", "Bill", "Trainer", illustrator="Ken Sugimori"),
                 card("tk-hs-g-18", "Bill", "Trainer", illustrator="Ken Sugimori", own=False),
                 card("svp-045", "Paradise Resort", "Trainer", illustrator="Naoki Saito"),
                 card("mep-092", "Paradise Resort", "Trainer", illustrator="Naoki Saito", own=False),
                 card("sm1-120", "Hau", "Trainer", illustrator="Ken Sugimori"),
                 card("tk-sm-r-19", "Hau", "Trainer", illustrator="Ken Sugimori", own=False)]
        self.assertEqual(same_art.aliases(cards, SETS), {"2024sv-1": ("swsh4-25", "reprint"),
                                                          "tk-sm-r-19": ("sm1-120", "reprint")})


class CuratedTests(unittest.TestCase):
    """v0.3.6 reviewed pairs (same-art-curated.json): only the illustrator / HP / attack-shape match is waived."""

    def cards(self):
        return [card("xy1-113", "Fletchling", hp=50, illustrator="5ban Graphics", shape=(("", "20"), 0)),
                card("xy0-19", "Pawniard", hp=60, illustrator="Kouki Saitou"),
                card("xy1-81", "Pawniard", hp=60, illustrator="Midori Harada"),
                card("xy0-37", "Potion", "Trainer", illustrator="5ban Graphics"),
                card("base1-94", "Potion", "Trainer", illustrator="Keiji Kinebuchi"),
                card("tk-xy-b-1", "Fletchling", hp=50, own=False),
                card("tk-xy-b-13", "Pawniard", hp=60, own=False),
                card("tk-xy-b-25", "Potion", "Trainer", own=False),
                card("tk-xy-b-29", "Potion", "Trainer", own=False)]

    def test_curated_pairs_fill_cards_without_an_illustrator(self):
        curated = {"tk-xy-b-1": {"source": "xy1-113", "note": "n"}, "tk-xy-b-13": {"source": "xy0-19", "note": "n"},
                   "tk-xy-b-25": {"source": "xy0-37", "note": "n"}}
        problems = {}
        self.assertEqual(same_art.aliases(self.cards(), SETS, curated, problems),
                         {"tk-xy-b-1": ("xy1-113", "curated"), "tk-xy-b-13": ("xy0-19", "curated"),
                          "tk-xy-b-25": ("xy0-37", "curated")})
        self.assertEqual(problems, {})

    def test_name_category_chains_and_era_still_apply(self):
        cases = {"tk-xy-b-1": {"source": "xy0-19", "note": "n"},              # another name
                 "tk-xy-b-13": {"source": "tk-xy-b-1", "note": "n"},          # a source without its own image
                 "tk-xy-b-25": {"source": "base1-94", "note": "n"},           # a Trainer from another era
                 "tk-xy-b-29": {"source": "xy9-999", "note": "n"}}            # not in the catalog
        problems = {}
        self.assertEqual(same_art.aliases(self.cards(), SETS, cases, problems), {})
        self.assertEqual(sorted(problems), sorted(cases))
        self.assertIn("another era", problems["tk-xy-b-25"])
        waived = {"tk-xy-b-25": {"source": "base1-94", "note": "by eye: same art", "crossEra": True}}
        self.assertEqual(same_art.aliases(self.cards(), SETS, waived), {"tk-xy-b-25": ("base1-94", "curated")})

    def test_a_curated_pair_never_replaces_an_image(self):
        cards = self.cards()
        cards[5]["needsImage"] = False   # tk-xy-b-1 got an image of its own
        self.assertEqual(same_art.aliases(cards, SETS, {"tk-xy-b-1": {"source": "xy1-113", "note": "n"}}), {})

    def test_load_curated(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "c.json")

        def write(doc):
            with open(path, "w") as f:
                json.dump(doc, f)
            return path
        self.assertEqual(same_art.load_curated(os.path.join(tmp, "missing.json")), {})
        good = {"cards": {"a-1": {"source": "b-1", "note": "by eye", "crossEra": False}}}
        self.assertEqual(same_art.load_curated(write(good)), good["cards"])
        for bad in ({"cards": {"a-1": {"source": "b-1"}}}, {"cards": {"a-1": {"source": "", "note": "x"}}},
                    {"cards": {"a-1": {"source": "a-1", "note": "x"}}},
                    {"cards": {"a-1": {"source": "b-1", "note": "x", "crossEra": "yes"}}},
                    {"cards": {"a-1": {"source": "b-1", "note": "x", "url": "https://x"}}}, {"cards": []}, []):
            with self.assertRaises(ValueError, msg=str(bad)):
                same_art.load_curated(write(bad))

    def test_the_repository_file_is_valid(self):
        curated = same_art.load_curated()
        self.assertGreaterEqual(len(curated), 67)
        for cid, e in curated.items():
            self.assertIn("2026-", e["note"], cid)
            self.assertFalse(e.get("crossEra"), "no cross-era pair so far")


class AliasProblemsTests(unittest.TestCase):
    def index(self, cards):
        return {"sets": [{"id": sid, "series": m["series"]} for sid, m in SETS.items()], "cards": cards}

    def test_a_good_alias_passes(self):
        cards = [{"s": "xy1", "l": "139", "n": "Metal Energy", "c": "E"},
                 {"s": "tk-xy-b", "l": "2", "n": "Metal Energy", "c": "E", "i": 0, "ia": "xy1-139"},
                 {"s": "sm1", "l": "120", "n": "Hau", "c": "T"},
                 {"s": "tk-sm-r", "l": "19", "n": "Hau", "c": "T", "i": 0, "ia": "sm1-120"},
                 {"s": "swsh4", "l": "25", "n": "Charizard", "c": "P"},
                 {"s": "2024sv", "l": "1", "n": "Charizard", "c": "P", "i": 0, "ia": "swsh4-25"}]
        self.assertEqual(same_art.alias_problems(self.index(cards)), [])

    def test_bad_aliases_fail(self):
        cases = {
            "not on an imageless card": [{"s": "xy1", "l": "139", "n": "M", "c": "E"},
                                         {"s": "tk-xy-b", "l": "2", "n": "M", "c": "E", "ia": "xy1-139"}],
            "source missing": [{"s": "tk-xy-b", "l": "2", "n": "M", "c": "E", "i": 0, "ia": "xy1-999"}],
            "source without its own TCGdex image": [
                {"s": "xy1", "l": "139", "n": "M", "c": "E", "i": 0, "ia": "g1-82"},
                {"s": "g1", "l": "82", "n": "M", "c": "E"},
                {"s": "tk-xy-b", "l": "2", "n": "M", "c": "E", "i": 0, "ia": "xy1-139"}],
            "another name or category": [{"s": "xy1", "l": "139", "n": "Metal Energy", "c": "E"},
                                         {"s": "tk-xy-b", "l": "2", "n": "Fairy Energy", "c": "E", "i": 0,
                                          "ia": "xy1-139"}],
            "Trainer or Energy from another era": [{"s": "sm1", "l": "171", "n": "M", "c": "E"},
                                                   {"s": "tk-xy-b", "l": "2", "n": "M", "c": "E", "i": 0,
                                                    "ia": "sm1-171"}],
        }
        for why, cards in cases.items():
            problems = same_art.alias_problems(self.index(cards))
            self.assertTrue(any(why in p for p in problems), (why, problems))
        cross = [{"s": "base1", "l": "94", "n": "Potion", "c": "T"},
                 {"s": "tk-xy-b", "l": "25", "n": "Potion", "c": "T", "i": 0, "ia": "base1-94"}]
        self.assertTrue(any("another era" in p for p in same_art.alias_problems(self.index(cross))))
        self.assertTrue(any("another era" in p for p in same_art.alias_problems(
            self.index(cross), {"tk-xy-b-25": {"source": "base1-94", "note": "n"}})), "crossEra must be explicit")
        self.assertEqual(same_art.alias_problems(
            self.index(cross), {"tk-xy-b-25": {"source": "base1-94", "note": "n", "crossEra": True}}), [])
        tcgplayer = [{"s": "xy1", "l": "139", "n": "M", "c": "E"},
                     {"s": "tk-xy-b", "l": "2", "n": "M", "c": "E", "i": 0, "im": "https://x", "ia": "xy1-139"}]
        self.assertTrue(any("not on an imageless card" in p for p in same_art.alias_problems(self.index(tcgplayer))))


class BundleFieldTests(unittest.TestCase):
    def test_ia_only_without_any_other_image(self):
        sets = [{"id": "tk-xy-b", "name": "Kit", "series": "tk", "groups": []},
                {"id": "xy1", "name": "XY", "series": "xy", "groups": []}]
        alias = {"source": "xy1-139", "method": "energy"}
        cards = [{"id": "tk-xy-b-2", "setId": "tk-xy-b", "localId": "2", "name": "Metal Energy", "image": None,
                  "imageAlias": alias, "tcgplayer": None},
                 {"id": "tk-xy-b-3", "setId": "tk-xy-b", "localId": "3", "name": "Metal Energy", "image": None,
                  "imageAlias": alias, "imageFallback": {"source": "tcgplayer", "url": "https://t/1.jpg"},
                  "tcgplayer": None}]
        idx = bundle.compact_index([{"id": "tk", "name": "Kits"}], sets, cards, [])
        self.assertEqual(idx["cards"][0]["ia"], "xy1-139")
        self.assertEqual(idx["cards"][0]["i"], 0)
        self.assertNotIn("ia", idx["cards"][1], "a TCGplayer photo of this printing wins")


if __name__ == "__main__":
    unittest.main()
