"""置換の代わりの部品の空いた費目に、置き換えた元の部品の値を引き継ぐ（②・③・③分析・分析用ブック）。"""

import unittest
from decimal import Decimal

import example_settings  # noqa: F401
from bomcost import BomLine, CostHead, CostRow, Item, Version
from bomcost.scenario import Change, ChangeKind, Scenario

M, D = CostHead.MATERIAL, CostHead.DIRECT


class InheritedCosts(unittest.TestCase):
    """Scenario の引き継ぎの規則。版は製品 P の下に部品 A（材料費 100・直接費 5）と B（材料費 70）。"""

    def setUp(self) -> None:
        v = Version("g")
        v.add_items([Item("P", "製品"), Item("A", "部品A"), Item("B", "部品B"), Item("N", "新部品"), Item("Q", "次の部品")])
        v.add_bom_lines([BomLine("P", "1", "A", 2), BomLine("P", "2", "B", 1)])
        v.add_costs([CostRow("A", {M: 100, D: 5}), CostRow("B", {M: 70}), CostRow("N", {D: 9})])
        self.version = v

    def scenario(self, *changes: Change) -> Scenario:
        s = Scenario("", "P", "P#", self.version)
        for change in changes:
            s.apply(change)
        return s

    def test_only_empty_heads_take_the_original_values(self) -> None:
        s = self.scenario(Change(ChangeKind.REPLACE, "A", 2, replacement="N", parent="P", balloon="1"))
        self.assertEqual(s.inherited_costs(), {"N": {M: Decimal(100)}})  # 直接費は N の 9 のまま
        priced = s.priced_version()
        self.assertEqual((priced.cost_value("N", M), priced.cost_value("N", D)), (Decimal(100), Decimal(9)))
        self.assertIsNone(self.version.cost_value("N", M))  # 元の版は変えない
        self.assertEqual(s.calculate().total, Decimal(270))
        self.assertEqual(s.history[0].cost_after, Decimal(270))

    def test_no_cost_row_takes_every_head(self) -> None:
        s = self.scenario(Change(ChangeKind.REPLACE, "B", 1, replacement="Q"))
        self.assertEqual(s.inherited_costs(), {"Q": {M: Decimal(70)}})
        self.assertEqual(s.calculate().total, Decimal(270))

    def test_conflicting_originals_leave_the_head_empty(self) -> None:
        s = self.scenario(Change(ChangeKind.REPLACE, "A", 2, replacement="Q", parent="P", balloon="1"),
                          Change(ChangeKind.REPLACE, "B", 1, replacement="Q", parent="P", balloon="2"))
        # 材料費は 100 と 70・直接費は 5 と空で食い違う
        self.assertEqual(s.inherited_costs(), {})
        self.assertIn("Q", s.calculate().missing)

    def test_same_values_from_two_originals_are_taken(self) -> None:
        self.version.add_cost(CostRow("B", {M: 100, D: 5}))
        s = self.scenario(Change(ChangeKind.REPLACE, "A", 2, replacement="Q", parent="P", balloon="1"),
                          Change(ChangeKind.REPLACE, "B", 1, replacement="Q", parent="P", balloon="2"))
        self.assertEqual(s.inherited_costs(), {"Q": {M: Decimal(100), D: Decimal(5)}})

    def test_add_does_not_inherit(self) -> None:
        s = self.scenario(Change(ChangeKind.ADD, "Q", 1))
        self.assertEqual(s.inherited_costs(), {})
        self.assertIs(s.priced_version(), self.version)
        self.assertIn("Q", s.calculate().missing)

    def test_replacing_a_replacement_carries_the_inherited_value(self) -> None:
        s = self.scenario(Change(ChangeKind.REPLACE, "B", 1, replacement="Q"),
                          Change(ChangeKind.REPLACE, "Q", 1, replacement="N"))
        self.assertEqual(s.inherited_costs()["N"], {M: Decimal(70)})

    def test_changes_argument_limits_the_inheritance(self) -> None:
        s = self.scenario(Change(ChangeKind.REPLACE, "B", 1, replacement="Q"))
        self.assertEqual(s.inherited_costs(changes=()), {})
        self.assertEqual(s.calculate(changes=()).total, Decimal(270))


if __name__ == "__main__":
    unittest.main()
