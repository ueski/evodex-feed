import unittest

import bundle


def variant(pid, sub, gid, label="", qualifier=""):
    return {"productId": pid, "subType": sub, "groupId": gid, "groupLabel": label, "qualifier": qualifier}


class CompactIndexGroupsTest(unittest.TestCase):
    def setUp(self):
        self.sets = [{"id": "base1", "name": "Base Set", "series": "base", "releaseDate": "1999-01-09",
                      "official": 102, "total": 102, "groups": [604, 1663]}]
        self.cards = [{
            "setId": "base1", "localId": "4", "name": "Charizard", "category": "Pokemon",
            "image": "https://assets.tcgdex.net/en/base/base1/4",
            "tcgplayer": {"number": "004/102", "variants": [
                variant(42382, "Holofoil", 604),
                variant(106999, "1st Edition Holofoil", 1663, label="Shadowless"),
                variant(106999, "Unlimited Holofoil", 1663, label="Shadowless"),
                variant(999, "Holofoil", 1522, qualifier="Black Dot Error"),
            ]},
        }]
        self.groups = [{"groupId": 604, "name": "Base Set", "abbreviation": "BS"},
                       {"groupId": 1663, "name": "Base Set (Shadowless)", "abbreviation": ""},
                       {"groupId": 1522, "name": "Alternate Art Promos", "abbreviation": "AAP"},
                       {"groupId": 7, "name": "Unrelated", "abbreviation": "UN"}]

    def test_gid_only_when_not_the_sets_first_group(self):
        idx = bundle.compact_index([{"id": "base", "name": "Base"}], self.sets, self.cards,
                                   ["Normal", "Holofoil"], groups=self.groups)
        products = idx["cards"][0]["p"]
        self.assertNotIn("gid", products[0])
        self.assertEqual(products[1]["gid"], 1663)
        self.assertEqual(products[2]["gid"], 1522)

    def test_tcg_groups_cover_referenced_groups_only(self):
        idx = bundle.compact_index([{"id": "base", "name": "Base"}], self.sets, self.cards,
                                   ["Normal", "Holofoil"], groups=self.groups)
        self.assertEqual(idx["tcgGroups"], [
            {"id": 604, "name": "Base Set", "abbr": "BS"},
            {"id": 1522, "name": "Alternate Art Promos", "abbr": "AAP"},
            {"id": 1663, "name": "Base Set (Shadowless)"},
        ])


if __name__ == "__main__":
    unittest.main()
