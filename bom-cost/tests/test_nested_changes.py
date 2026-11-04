"""同じ試算で、部品（組立品）とその中の場所を両方は変えられない（場所の変更は版の構成の使用数で倍率を取るので原価が誤る）。"""

import unittest

from bomcost import Change, ChangeKind, CostHead, Scenario
from bomcost.demo import demo_policy, demo_versions
from test_group import NAME, SampleDbCase

PARENT_OUT = {"kind": "REPLACE", "part": "A-100", "parent": "V-110", "balloon": "1", "replacement": "C-300",
              "quantity": "1"}
PARENT_X2 = {"kind": "SET_QUANTITY", "part": "A-100", "parent": "V-110", "balloon": "1", "quantity": "2"}
CHILD = {"kind": "REPLACE", "part": "B-200", "parent": "A-100", "balloon": "1", "replacement": "F-600", "quantity": "2"}


class NestedScenario(unittest.TestCase):
    def scenario(self) -> Scenario:
        return Scenario("t", "V-110", "V-110#", demo_versions()[0], CostHead.MATERIAL, demo_policy())

    def test_parent_and_inside_refused_both_orders(self) -> None:
        parent = Change(ChangeKind.REPLACE, "A-100", 1, "C-300", parent="V-110", balloon="1")
        child = Change(ChangeKind.REPLACE, "B-200", 2, "F-600", parent="A-100", balloon="1")
        for first, second in ((parent, child), (child, parent)):
            s = self.scenario()
            s.apply(first)
            with self.assertRaisesRegex(ValueError, "変えられない"):
                s.apply(second)
            self.assertEqual(s.changes, (first,))

    def test_part_wide_change_inside_refused_both_orders(self) -> None:
        parent = Change(ChangeKind.REPLACE, "A-100", 1, "C-300", parent="V-110", balloon="1")
        child = Change(ChangeKind.REMOVE, "B-200", 2)
        for first, second in ((parent, child), (child, parent)):
            s = self.scenario()
            s.apply(first)
            with self.assertRaisesRegex(ValueError, "変えられない"):
                s.apply(second)
            self.assertEqual(s.changes, (first,))

    def test_batch_left_unchanged_when_refused(self) -> None:
        s = self.scenario()
        s.apply(Change(ChangeKind.REMOVE, "A-100", 1))
        before = s.history
        with self.assertRaises(ValueError):
            s.apply_batch([Change(ChangeKind.SET_QUANTITY, "F-600", 2, parent="V-110", balloon="2"),
                           Change(ChangeKind.SET_QUANTITY, "C-300", 1, parent="A-100", balloon="2")])
        self.assertEqual(s.history, before)

    def test_head_and_add_not_concerned(self) -> None:
        s = self.scenario()
        s.apply(Change(ChangeKind.REPLACE, "A-100", 1, "C-300", parent="V-110", balloon="1"))
        s.apply(Change(ChangeKind.ADD, "B-200", 1))
        s.apply(Change(ChangeKind.SET_HEAD, "V-110", 10, target_head="DIRECT"))
        self.assertEqual(len(s.history), 3)


class NestedApi(SampleDbCase):
    def scenario_changes(self) -> int:
        return len(self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")["rows"])

    def refused(self, change: dict, words: str) -> None:
        before = self.scenario_changes()
        bad = self.call("POST", "/api/group/changes", body={"version_id": "2026-10", "name": NAME, "product": "V-110",
                                                            **change})
        self.assert_error(bad, 400, words)
        self.assertEqual(self.scenario_changes(), before)

    def test_replace_and_inside_replace_refused_both_orders(self) -> None:
        self.change("V-110", **PARENT_OUT)
        self.refused(CHILD, "A-100 を変えたので、その中の A-100 の #1 B-200 は変えられない")
        self.ok("POST", "/api/group/changes/undo", body={"scenario_id": self.sid(), "sequence": 1})
        self.change("V-110", **CHILD)
        self.refused(PARENT_OUT, "A-100 の #1 B-200 を変えたので、V-110 の #1 A-100 は変えられない")

    def test_quantity_and_inside_replace_refused_both_orders(self) -> None:
        self.change("V-110", **PARENT_X2)
        self.refused(CHILD, "A-100 を変えたので")
        self.ok("POST", "/api/group/changes/undo", body={"scenario_id": self.sid(), "sequence": 1})
        self.change("V-110", **CHILD)
        self.refused(PARENT_X2, "V-110 の #1 A-100 は変えられない")

    def test_part_change_without_place_and_bulk_replace_refused(self) -> None:
        self.change("V-110", kind="REMOVE", part="A-100", quantity="1")
        self.refused(CHILD, "A-100 を変えたので")
        bad = self.call("POST", "/api/group/changes/bulk", body={
            "version_id": "2026-10", "name": NAME, "replacement": "X-610",
            "items": [{"product": "V-110", "part": "B-200"}]})
        self.assert_error(bad, 400, "A-100 を変えたので")
        self.assertEqual(self.scenario_changes(), 1)

    def test_unrelated_place_still_changes(self) -> None:
        self.change("V-110", **PARENT_OUT)
        self.change("V-110", kind="SET_QUANTITY", part="F-600", parent="V-110", balloon="2", quantity="2")
        self.assertEqual(self.scenario_changes(), 2)

    def test_locks_for_screen(self) -> None:
        self.change("V-110", **PARENT_OUT)
        locks = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")["locks"]["V-110"]
        self.assertEqual(locks["inside"].get("A-100"), "A-100")
        rows = self.parts(f"&name={NAME}&part=B-200")["rows"]
        self.assertEqual([(r["product"], r["locked"]) for r in rows if r["product"] == "V-110"],
                         [("V-110", {"kind": "inside", "by": "A-100"})])
        self.ok("POST", "/api/group/changes/undo", body={"scenario_id": self.sid(), "sequence": 1})
        self.change("V-110", **CHILD)
        rows = self.parts(f"&name={NAME}&part=A-100")["rows"]
        self.assertEqual([r["locked"] for r in rows if r["product"] == "V-110"],
                         [{"kind": "outer", "by": "A-100 の #1 B-200"}])

    def sid(self) -> int:
        return self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")["rows"][0]["scenario_id"]


if __name__ == "__main__":
    unittest.main()
