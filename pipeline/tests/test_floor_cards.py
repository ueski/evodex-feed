import json
import os
import shutil
import tempfile
import unittest

import net
import sources


def set_of(sid, local_ids, official=2, total=2):
    return {"id": sid, "cardCount": {"official": official, "total": total},
            "cards": [{"id": "%s-%s" % (sid, lid), "localId": lid, "name": "Listed"} for lid in local_ids]}


def card(cid, name="Back", official=4, total=4):
    sid, lid = cid.rsplit("-", 1)
    return {"id": cid, "localId": lid, "name": name, "image": "https://assets.tcgdex.net/en/%s/%s" % (sid, lid),
            "set": {"id": sid, "cardCount": {"official": official, "total": total}}}


def no_live(_cid):
    return None


class RestoreFloorCardsTests(unittest.TestCase):
    def test_nothing_changes_when_the_list_has_every_floor_card(self):
        sets, cards = [set_of("mee", ["001", "002"])], {}
        report = sources.restore_floor_cards(sets, cards, {"mee": ["001", "002"]}, no_live, {})
        self.assertEqual(report, {"live": [], "snapshot": [], "lost": []})
        self.assertEqual(cards, {})
        self.assertEqual(sets[0]["cardCount"], {"official": 2, "total": 2})

    def test_a_card_the_list_lacks_comes_from_tcgdex_by_id_first(self):
        sets, cards = [set_of("mee", ["001"])], {}
        report = sources.restore_floor_cards(sets, cards, {"mee": ["001", "002"]},
                                             lambda cid: card(cid, "Live"), {"mee-002": card("mee-002", "Snapshot")})
        self.assertEqual(report["live"], ["mee-002"])
        self.assertEqual(cards["mee-002"]["name"], "Live")
        self.assertEqual(sets[0]["cards"][1]["id"], "mee-002")
        self.assertEqual(sets[0]["cards"][1]["name"], "Live")

    def test_the_snapshot_answers_when_tcgdex_does_not_have_the_card(self):
        def not_cached(_cid):
            raise net.FetchError("offline and not cached")
        for fetch in (no_live, not_cached):
            sets, cards = [set_of("mee", ["001"])], {}
            report = sources.restore_floor_cards(sets, cards, {"mee": ["001", "002"]}, fetch,
                                                 {"mee-002": card("mee-002", "Snapshot")})
            self.assertEqual(report["snapshot"], ["mee-002"])
            self.assertEqual(cards["mee-002"]["name"], "Snapshot")

    def test_a_card_found_nowhere_is_reported_and_left_for_the_guard(self):
        sets, cards = [set_of("mee", ["001"])], {}
        report = sources.restore_floor_cards(sets, cards, {"mee": ["001", "002"]}, no_live, {})
        self.assertEqual(report["lost"], ["mee-002"])
        self.assertEqual([b["localId"] for b in sets[0]["cards"]], ["001"])
        self.assertNotIn("mee-002", cards)

    def test_restored_cards_keep_the_floors_order_and_raise_the_sets_counts(self):
        sets, cards = [set_of("mee", ["001", "003"])], {}
        sources.restore_floor_cards(sets, cards, {"mee": ["001", "002", "003", "004"]}, card, {})
        self.assertEqual([b["localId"] for b in sets[0]["cards"]], ["001", "002", "003", "004"])
        self.assertEqual(sets[0]["cardCount"], {"official": 4, "total": 4})

    def test_a_card_newer_than_the_floor_stays_after_the_floors_cards(self):
        sets, cards = [set_of("mee", ["001", "005"])], {}
        sources.restore_floor_cards(sets, cards, {"mee": ["001", "002"]}, card, {})
        self.assertEqual([b["localId"] for b in sets[0]["cards"]], ["001", "002", "005"])

    def test_digital_sets_and_sets_tcgdex_no_longer_lists_are_left_alone(self):
        pocket = dict(set_of("pocket", ["001"]), digital=True)
        sets, cards = [pocket], {}
        report = sources.restore_floor_cards(sets, cards, {"pocket": ["001", "002"], "gone": ["001"]}, card, {})
        self.assertEqual(report, {"live": [], "snapshot": [], "lost": []})
        self.assertEqual(len(pocket["cards"]), 1)


class SnapshotFileTests(unittest.TestCase):
    def test_a_missing_file_is_an_empty_snapshot(self):
        self.assertEqual(sources.load_floor_snapshot("/nonexistent/floor-cards.json"), {})

    def test_the_repository_snapshot_only_holds_floor_cards_and_no_prices(self):
        """TCGdex pricing is another market's data (single price source): it must not sit in the repository."""
        snapshot = sources.load_floor_snapshot()
        floor = sources.load_floor_sets()
        self.assertTrue(snapshot)
        for cid, c in snapshot.items():
            self.assertEqual(cid, c["id"])
            self.assertIn(c["localId"], floor[c["set"]["id"]], cid)
            self.assertNotIn("pricing", c, cid)

    def test_the_snapshot_covers_the_cards_of_the_short_set_lists(self):
        snapshot = sources.load_floor_snapshot()
        for cid in ("mep-089", "mep-110", "mep-120", "mee-009", "mee-016", "30th-B", "30th-G", "30th-R"):
            self.assertIn(cid, snapshot)

    def test_load_reads_the_cards_object(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "floor-cards.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"_doc": "x", "cards": {"mee-002": card("mee-002")}}, f)
        self.assertEqual(list(sources.load_floor_snapshot(path)), ["mee-002"])


if __name__ == "__main__":
    unittest.main()
