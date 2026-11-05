"""社内加工費を品目の原価として登録する（保存・取込・マスタ登録・式・照会）。品番・数値は架空。"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from bomcost import BomLine, ColumnMap, CostHead, CostRow, Item, Store, Version, VersionImport
from bomcost.formula import Head, OwnValues, breakdown, registered_own
from bomcost.importer import TABLE_FIELDS, load_openpyxl, read_table_bytes
from bomcost.rollup import calculate_cost
from bomcost.scenario import Change, ChangeKind, Scenario
from test_app import DemoDbCase
from test_group import HAS_OPENPYXL, SampleDbCase

P = CostHead.PROCESSING
D = Decimal


def version_with_processing() -> Version:
    """P（社内加工費 20・償却費 10・直接費 5）→ C ×2（材料費 100・社内加工費 7）。"""
    v = Version("t")
    v.add_items([Item("P", "製品", "P"), Item("C", "部品C", "B")])
    v.add_bom_lines([BomLine("P", "1", "C", 2)])
    v.add_costs([
        CostRow("P", {P: 20, CostHead.AMORTIZATION: 10, CostHead.DIRECT: 5}),
        CostRow("C", {CostHead.MATERIAL: 100, P: 7, CostHead.DIRECT: 3}),
    ])
    return v


class Formula(unittest.TestCase):
    def test_uses_own_registered_value_not_children(self) -> None:
        v = version_with_processing()
        self.assertEqual(registered_own(v, "P"), OwnValues(D(20), D(10), D(5)))
        self.assertEqual(calculate_cost(v, "P", P).total, D(20))  # 子の 2×7 は足さない
        # (200 ＋ 20 ＋ 10) × 1.03 × 1.11 ＋ 5
        self.assertEqual(calculate_cost(v, "P", CostHead.MANUFACTURING).total, D(230) * D("1.03") * D("1.11") + 5)

    def test_empty_counts_as_zero_without_mark(self) -> None:
        v = version_with_processing()
        v.add_cost(CostRow("P", {CostHead.AMORTIZATION: 10, CostHead.DIRECT: 5}))
        self.assertEqual(registered_own(v, "P").processing, D(0))
        s = Scenario("s", "P", "P#", v, CostHead.MANUFACTURING)
        self.assertEqual(s.calculate().total, D(210) * D("1.03") * D("1.11") + 5)
        b =breakdown(D(200), registered_own(v, "P"))
        self.assertEqual(b.value(Head.PROCESSING), D(0))
        self.assertEqual(b.incomplete, frozenset())

    def test_set_head_overrides_registered(self) -> None:
        s = Scenario("s", "P", "P#", version_with_processing(), CostHead.MANUFACTURING)
        self.assertEqual(s.own_values().processing, D(20))
        s.apply(Change(ChangeKind.SET_HEAD, "P", 50, target_head="PROCESSING"))
        self.assertEqual(s.own_values().processing, D(50))
        self.assertEqual(s.calculate().total, D(260) * D("1.03") * D("1.11") + 5)
        s.undo()
        self.assertEqual(s.own_values().processing, D(20))


class StoreAndImport(unittest.TestCase):
    def test_csv_mapping_and_store_round_trip(self) -> None:
        spec = next(f for f in TABLE_FIELDS["cost"] if f.name == "processing")
        self.assertEqual((spec.label, spec.required), ("社内加工費", False))
        table = read_table_bytes("cost.csv", "MBOM品番,材料費,加工\nP-1,100,25\nP-2,50,\n".encode())
        version = Version("v1")
        importer = VersionImport(version)
        importer.add_table(table, ColumnMap("cost", table.headers,
                                            {"mbom_code": "MBOM品番", "material": "材料費", "processing": "加工"}))
        self.assertEqual((version.cost_value("P-1", P), version.cost_value("P-2", P)), (D(25), None))
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "x.sqlite") as store:
            store.save_group(version)
            loaded = store.load_group("v1")
            self.assertEqual((loaded.cost_value("P-1", P), loaded.cost_value("P-2", P)), (D(25), None))
            self.assertEqual(loaded.cost_value("P-1", CostHead.MATERIAL), D(100))

    def test_csv_without_column_leaves_empty(self) -> None:
        table = read_table_bytes("cost.csv", "MBOM品番,材料費\nP-1,100\n".encode())
        version = Version("v1")
        VersionImport(version).add_table(table, ColumnMap("cost", table.headers,
                                                          {"mbom_code": "MBOM品番", "material": "材料費"}))
        self.assertIsNone(version.cost_value("P-1", P))


class Api(SampleDbCase):
    def test_master_heads_have_processing(self) -> None:
        labels = ["材料費", "償却費", "直接費", "社内加工費"]
        self.assertEqual([h["label"] for h in self.ok("GET", "/api/manual", "group=2026-10")["heads"]], labels)
        self.assertEqual([h["label"] for h in self.ok("GET", "/api/meta")["master_heads"]], labels)

    def test_register_and_use_in_formula_and_lookup(self) -> None:
        # A-100 は BOM で材料費 200・償却費と直接費は未登録。社内加工費は登録で埋まる
        self.ok("PUT", "/api/manual/items", body={"group_id": "2026-10", "code": "A-100", "name": "製品A", "costs": {"PROCESSING": "30"}})
        manual = {i["code"]: i for i in self.ok("GET", "/api/manual", "group=2026-10")["items"]}
        self.assertEqual(manual["A-100"]["costs"]["PROCESSING"], "30")
        item = self.ok("GET", "/api/lookup", "group=2026-10&q=A-100")["item"]
        costs = {c["head"]: (c["value"], c["source"], c["label"]) for c in item["costs"]}
        self.assertEqual(costs["PROCESSING"], ("30", "master", "社内加工費"))
        calc = {c["head"]: (c["value"], c["incomplete"]) for c in item["calculated"]}
        self.assertEqual(calc["PROCESSING"], ("30", False))
        self.assertEqual(calc["MANUFACTURING"][0], "262.959")  # (200 ＋ 30) × 1.03 × 1.11
        brief = self.ok("GET", "/api/lookup", "group=2026-10&q=A-100&brief=1")["item"]
        self.assertIn(("PROCESSING", "30"), [(c["head"], c["value"]) for c in brief["costs"]])


class Book(DemoDbCase):
    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl が無い")
    def test_master_book_round_trip(self) -> None:
        self.api.open_file = None
        self.ok("PUT", "/api/manual/items", body={"group_id": "2026-10", "role": "part", "code": "MQ-1", "name": "部品1", "kind": "B",
                                                  "costs": {"MATERIAL": "10", "PROCESSING": "4"}})
        path = Path(self.ok("POST", "/api/master/book", body={"group_id": "2026-10"})["path"])
        wb = load_openpyxl().load_workbook(path)
        ws = wb["部品"]
        headers = [c.value for c in ws[1]]
        column = headers.index("社内加工費") + 1
        row = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value == "MQ-1")
        self.assertEqual(ws.cell(row, column).value, 4)
        self.assertEqual(self.ok("POST", "/api/master/check", body={"group_id": "2026-10"})["changed"], 0)
        ws.cell(row, column).value = 6.5
        wb.save(path)
        wb.close()
        self.assertEqual(self.ok("POST", "/api/master/check", body={"group_id": "2026-10"})["changed"], 1)
        self.ok("POST", "/api/master/apply", body={"group_id": "2026-10"})
        self.assertEqual(self.store.load_manual("2026-10").cost_value("MQ-1", P), D("6.5"))


if __name__ == "__main__":
    unittest.main()
