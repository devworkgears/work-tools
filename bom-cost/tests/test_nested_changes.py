"""同じ試算で、部品（組立品）とその中の場所を両方は変えられない（場所の変更は版の構成の使用数で倍率を取るので原価が誤る）。"""

import unittest

import example_settings  # noqa: F401
from bomcost import Change, ChangeKind, CostHead, Scenario
import example_data as ex

class NestedScenario(unittest.TestCase):
    def scenario(self) -> Scenario:
        return Scenario("t", "V-110", "V-110#", ex.imported_2026_10(), CostHead.MATERIAL, ex.policy())

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


if __name__ == "__main__":
    unittest.main()
