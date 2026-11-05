"""置換の代わりの部品の空いた費目に、置き換えた元の部品の値を引き継ぐ（②・③・③分析・分析用ブック）。"""

import io
import json
import unittest
from decimal import Decimal
from urllib.parse import quote

from bomcost import BomLine, CostHead, CostRow, Item, Version
from bomcost.importer import load_openpyxl
from bomcost.scenario import Change, ChangeKind, Scenario
from test_group import HAS_OPENPYXL, NAME, SampleDbCase

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


class InheritedOnScreens(SampleDbCase):
    """見本の DB の A-100 の #2 C-300（材料費 500）×3 を、直接費だけマスタ登録した N-999 に置換する。"""

    def setUp(self) -> None:
        super().setUp()
        self.ok("PUT", "/api/manual/items", body={"group_id": "2026-10", "code": "N-999", "name": "新部品",
                                                  "costs": {"DIRECT": "30"}})

    def replace(self, replacement: str = "N-999", balloon: str = "2", part: str = "C-300") -> dict:
        return self.change("A-100", kind="REPLACE", part=part, parent="A-100", balloon=balloon,
                           replacement=replacement, quantity="3")

    def table(self) -> dict:
        return self.ok("GET", "/api/group/compare/table",
                       f"group=2026-10&names={quote(json.dumps([NAME]))}&ignore_amortization=1")

    def trial_of(self, table: dict, product: str = "A-100") -> dict:
        return next(p for p in table["products"] if p["product"] == product)["trials"][0]

    def analysis(self) -> dict:
        return self.ok("GET", "/api/group/compare/analysis",
                       f"group=2026-10&names={quote(json.dumps([NAME]))}&products={quote(json.dumps(['A-100']))}")

    def test_partly_registered_part_is_filled_in_compare(self) -> None:
        self.replace()
        trial = self.trial_of(self.table())
        # 200（A-100 の登録値）− 500×3 ＋ 500×3（引き継いだ材料費）
        self.assertEqual(trial["after"]["MATERIAL"], "200")
        self.assertNotIn("MATERIAL", trial["incomplete"])  # 直接費は製品 A-100 自身の登録値が無いので現行から一部未登録
        rows = {r["code"]: r for r in self.analysis()["series"][1]["rows"]}
        self.assertEqual((rows["N-999"]["amount"], rows["N-999"]["missing"]), ("1500", 0))
        self.assertEqual(self.by_code()["A-100"]["after"], "200")

    def test_mark_on_trial_rows(self) -> None:
        self.replace()
        change = self.ok("GET", "/api/group/changes", "group=2026-10")["rows"][0]
        self.assertEqual(change["replacements"][0]["inherited"], ["MATERIAL"])
        self.assertEqual(change["difference"], "0")
        tree = self.ok("GET", "/api/group/tree", "group=2026-10&product=A-100")
        nodes = {n["code"]: n for n in tree["children"]}
        self.assertEqual((nodes["N-999"]["inherited"], nodes["N-999"]["cost"]), (["MATERIAL"], "500"))
        self.assertEqual(nodes["A-100"]["inherited"], [])
        # グループの登録値（品目照会）は変えない
        costs = {c["head"]: c["value"] for c in self.ok("GET", "/api/lookup", "group=2026-10&q=N-999&brief=1")["item"]["costs"]}
        self.assertEqual((costs["MATERIAL"], costs["DIRECT"]), (None, "30"))

    def test_unregistered_costs_take_every_head(self) -> None:
        self.ok("PUT", "/api/manual/items", body={"group_id": "2026-10", "code": "N-998", "name": "未登録の部品"})
        self.replace("N-998")
        trial = self.trial_of(self.table())
        self.assertEqual(trial["after"]["MATERIAL"], "200")
        self.assertNotIn("MATERIAL", trial["incomplete"])

    def test_conflicting_originals_stay_unregistered(self) -> None:
        self.replace()
        self.change("A-100", kind="REPLACE", part="B-200", parent="A-100", balloon="1", replacement="N-999",
                    quantity="2")
        trial = self.trial_of(self.table())
        self.assertIn("MATERIAL", trial["incomplete"])
        change = self.ok("GET", "/api/group/changes", "group=2026-10")["rows"][0]
        self.assertNotIn("inherited", change["replacements"][0])

    def test_add_stays_unregistered(self) -> None:
        self.change("A-100", kind="ADD", part="N-999", quantity="1")
        trial = self.trial_of(self.table())
        self.assertIn("MATERIAL", trial["incomplete"])
        rows = {r["code"]: r for r in self.analysis()["series"][1]["rows"]}
        self.assertEqual((rows["N-999"]["amount"], rows["N-999"]["missing"]), (None, 1))

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl が無い")
    def test_analysis_book_uses_inherited_values(self) -> None:
        self.replace()
        r = self.call("GET", "/api/group/compare/analysis/xlsx",
                      f"group=2026-10&names={quote(json.dumps([NAME]))}&ignore_amortization=1")
        self.assertEqual(r.status, 200, r.body[:300])
        ws = load_openpyxl().load_workbook(io.BytesIO(r.body))["構成"]
        rows = list(ws.iter_rows(values_only=True))
        places = [dict(zip(rows[0], row)) for row in rows[1:]]
        new = next(p for p in places if p["検討案"] == NAME and p["部品"] == "N-999")
        self.assertEqual(Decimal(str(new["材料費"])), Decimal(1500))


if __name__ == "__main__":
    unittest.main()
