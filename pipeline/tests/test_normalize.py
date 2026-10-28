import unittest

from normalize import (name_key, name_similarity, number_key, number_prefix,
                       product_qualifier, set_name_key, set_name_similarity)


class NumberKeyTests(unittest.TestCase):
    def check(self, cases):
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(number_key(raw), want)

    def test_plain_and_slash_numbers(self):
        self.check([("025/165", "25"), ("25", "25"), ("001", "1"), ("0", "0"),
                    ("000", "0"), ("4/102", "4"), ("108/102", "108"), ("050/185", "50")])

    def test_prefixed_numbers(self):
        self.check([("SV001", "SV1"), ("SV001/SV122", "SV1"), ("SV1/SV94", "SV1"),
                    ("TG01", "TG1"), ("TG01/TG30", "TG1"), ("GG01", "GG1"), ("GG70/GG70", "GG70"),
                    ("RC1", "RC1"), ("RC1/RC32", "RC1"), ("H1", "H1"), ("H01/H32", "H1")])

    def test_promo_numbers(self):
        self.check([("SWSH001", "SWSH1"), ("SWSH020", "SWSH20"), ("SM01", "SM1"), ("SM100", "SM100"),
                    ("XY01", "XY1"), ("BW01", "BW1"), ("DP01", "DP1"), ("HGSS01", "HGSS1"),
                    ("37/SM-P", "37"), ("PW 1", "PW1")])

    def test_suffix_letters(self):
        self.check([("18a", "18A"), ("018a/147", "18A"), ("XY67a", "XY67A"), ("SM103a", "SM103A"),
                    ("24a", "24A"), ("098b/122", "98B")])

    def test_letters_and_symbols(self):
        self.check([("A", "A"), ("A/28", "A"), ("!", "!"), ("!/28", "!"), ("?/28", "?"),
                    ("%3F", "?"), ("ONE", "ONE"), ("Museum", "MUSEUM")])

    def test_both_sides_agree(self):
        # TCGdex localId vs TCGplayer extendedData Number for the same card
        pairs = [("025", "025/165"), ("TG01", "TG01/TG30"), ("SV001", "SV001/SV122"),
                 ("GG01", "GG01/GG70"), ("SWSH001", "SWSH001"), ("SM01", "SM01"),
                 ("RC1", "RC1/RC25"), ("H1", "H1/H32"), ("24a", "024a/119"), ("%3F", "?/28"),
                 ("1", "001/005")]
        for dex, tcgp in pairs:
            with self.subTest(dex=dex):
                self.assertEqual(number_key(dex), number_key(tcgp))

    def test_none_and_whitespace(self):
        self.assertEqual(number_key(None), "")
        self.assertEqual(number_key("  007 "), "7")

    def test_prefix(self):
        self.assertEqual(number_prefix("TG1"), "TG")
        self.assertEqual(number_prefix("25"), "")
        self.assertEqual(number_prefix("A"), "")


class NameTests(unittest.TestCase):
    def test_name_key(self):
        cases = [
            ("Charizard ex", "charizard ex"),
            ("Flabébé", "flabebe"),
            ("Chimecho δ", "chimecho"),
            ("Chimecho (Delta Species)", "chimecho"),
            ("Pikachu - 025/165 (Cosmos Holo)", "pikachu"),
            ("Acerola - 112a/147 (League Challenge) [1st Place]", "acerola"),
            ("M Lucario EX - 55a/111 Promo", "m lucario ex"),
            ("Blastoise Classic Collection", "blastoise"),
            ("Unown [A]", "unown"),
            ("Professor’s Letter", "professors letter"),
            ("Professor's Letter (Sylveon)", "professors letter"),
            ("Pokémon Catcher", "pokemon catcher"),
            ("Nidoran♀", "nidoran f"),
            ("Treecko - 1/5", "treecko"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(name_key(raw), want)

    def test_similarity(self):
        self.assertEqual(name_similarity("Pikachu", "Pikachu - 025/165"), 1.0)
        self.assertEqual(name_similarity("Umbreon ☆", "Umbreon Star"), 0.8)
        self.assertEqual(name_similarity("Pikachu on the Ball", "Pikachu"), 0.8)
        # sharing only mechanic tokens is not similar
        self.assertEqual(name_similarity("Mew ex", "Scizor ex"), 0.0)
        self.assertEqual(name_similarity("Grass Energy", "Fire Energy"), 0.0)
        # V vs VMAX of the same Pokemon: similar but below the 0.5 bar
        self.assertLess(name_similarity("Pikachu V", "Pikachu VMAX"), 0.5)
        self.assertGreater(name_similarity("Pikachu V", "Pikachu VMAX"), 0.0)
        self.assertEqual(name_similarity("", "Pikachu"), 0.0)

    def test_qualifier(self):
        self.assertEqual(product_qualifier("Pikachu - 025/165 (Cosmos Holo)"), "Cosmos Holo")
        self.assertEqual(product_qualifier("Acerola - 112a/147 (League Challenge) [1st Place]"),
                         "League Challenge, 1st Place")
        self.assertEqual(product_qualifier("Charizard"), "")


class SetNameTests(unittest.TestCase):
    def test_set_name_key(self):
        cases = [
            ("SWSH09: Brilliant Stars Trainer Gallery", "brilliant stars trainer gallery"),
            ("XY - Flashfire", "flashfire"),
            ("SM - Guardians Rising", "guardians rising"),
            ("EX Ruby and Sapphire", "ruby and sapphire"),
            ("Ruby & Sapphire", "ruby and sapphire"),
            ("SV: Scarlet & Violet 151", "scarlet and violet 151"),
            ("ME01: Mega Evolution", "mega evolution"),
            ("SWSH: Crown Zenith: Galarian Gallery", "crown zenith galarian gallery"),
            ("Pokémon GO", "go"),
            ("Base Set", "base set"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(set_name_key(raw), want)

    def test_set_similarity(self):
        self.assertEqual(set_name_similarity("Crown Zenith Galarian Gallery",
                                             "SWSH: Crown Zenith: Galarian Gallery"), 1.0)
        self.assertGreaterEqual(set_name_similarity("Expedition Base Set", "Expedition"), 0.75)
        self.assertLess(set_name_similarity("Crown Zenith", "SWSH: Crown Zenith: Galarian Gallery"), 1.0)
        self.assertEqual(set_name_similarity("Base Set", "Base Set"), 1.0)


if __name__ == "__main__":
    unittest.main()
