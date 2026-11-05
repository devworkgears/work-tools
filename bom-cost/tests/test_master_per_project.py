"""マスタ登録・型費・流用先・部品の対応表はグループごと（グループどうしは共有しない）。データはすべて架空。"""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from bomcost import (
    BomLine,
    CostHead,
    CostRow,
    Item,
    MoldCost,
    ReuseTarget,
    Store,
    Version,
    calculate_cost,
)
from bomcost.api import Api
from bomcost.importer import load_openpyxl
from test_screens import EmptyDbCase

M = CostHead.MATERIAL


def bom(group_id: str, dev: str, extra_cost: dict[str, int] | None = None) -> Version:
    """製品 P-1 ＝ 部品 B-1 ×2（＋ extra_cost の品番 ×1。その品番は品目・原価も BOM に入る）。"""
    v = Version(group_id, dev_number=dev)
    v.add_items([Item("P-1", "製品", "P", has_children=1), Item("B-1", "部品B", "B")])
    v.add_bom_lines([BomLine("P-1", "1", "B-1", 2)])
    v.add_cost(CostRow("B-1", {M: 10}))
    for code, value in (extra_cost or {}).items():
        v.add_item(Item(code, "BOM の品目", "B"))
        v.add_bom_lines([BomLine("P-1", "2", code, 1)])
        v.add_cost(CostRow(code, {M: value}))
    return v


def registration(*costs: tuple[str, int]) -> Version:
    v = Version("manual")
    for code, value in costs:
        v.add_item(Item(code, "登録の品目", "B"))
        v.add_cost(CostRow(code, {M: value}))
    return v


class Overlay(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.store.save_group(bom("g1", "D-100"))
        self.store.save_group(bom("g2", "D-100", {"N-999": 120}))
        self.store.save_group(bom("g3", "D-200"))

    def tearDown(self) -> None:
        self.store.close()

    def total(self, group_id: str, code: str = "P-1") -> Decimal:
        return calculate_cost(self.store.load_group(group_id), code, M).total

    def test_registration_overlays_only_its_group(self) -> None:
        own = registration(("N-999", 100))
        own.add_bom_lines([BomLine("T-1", None, "N-999", 1)])  # 試作の製品に N-999 を使う
        self.store.save_manual(own, "g1")
        self.assertEqual((self.total("g1", "T-1"), self.total("g1")), (Decimal(100), Decimal(20)))
        # 同じ開発番号の別のグループにも重ならない
        for group_id in ("g2", "g3"):
            other = self.store.load_group(group_id)
            self.assertNotIn("T-1", other.items)
            self.assertNotIn("T-1", other.parents())
        self.assertEqual(self.store.load_group("g2").cost_value("N-999", M), Decimal(120))
        self.assertIsNone(self.store.load_group("g3").cost_value("N-999", M))

    def test_molds_reuse_part_maps_per_group(self) -> None:
        s = self.store
        s.save_mold_cost(MoldCost("B-1", 3000, 3), "g1")
        s.save_mold_cost(MoldCost("N-999", 500, 1), "g3")
        self.assertEqual(s.mold_costs("g1")["B-1"].amount, Decimal(3000))
        self.assertEqual((set(s.mold_costs("g2")), set(s.mold_costs("g3"))), (set(), {"N-999"}))
        s.add_reuse_target(ReuseTarget("B-1", "K-1", 4), "g1")
        s.add_reuse_target(ReuseTarget("B-1", "K-2", 1), "g3")
        self.assertEqual([(t.product, t.quantity_per_unit) for t in s.reuse_targets("g1", "B-1")],
                         [("K-1", Decimal(4))])
        self.assertEqual([t.product for t in s.reuse_targets("g3")], ["K-2"])
        s.add_part_map("g1", "P-0", "P-1", "B-0", "B-2")
        s.add_part_map("g3", "P-0", "P-1", "C-0", "C-1")
        self.assertEqual(s.part_map("g1", "P-0", "P-1"), {"B-0": "B-2"})
        self.assertEqual(s.part_map("g3", "P-0", "P-1"), {"C-0": "C-1"})
        self.assertEqual(s.delete_part_map("g3", "P-0", "P-1", "B-0", "B-2"), 0)
        self.assertEqual(s.delete_part_map("g1", "P-0", "P-1", "B-0", "B-2"), 1)
        with self.assertRaises(KeyError):
            s.save_mold_cost(MoldCost("B-1", 1, 1), "NOPE")


class ManualApi(EmptyDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.save_group(bom("g1", "D-100"))
        self.store.save_group(bom("g2", "D-100", {"N-999": 120}))
        self.store.save_group(bom("g3", "D-200"))

    def test_registration_goes_to_selected_group(self) -> None:
        data = self.ok("PUT", "/api/manual/items", body={"code": "N-999", "name": "仮", "kind": "B",
                                                         "costs": {"MATERIAL": "100"}, "group_id": "g2"})
        self.assertEqual((data["group_id"], [i["code"] for i in data["items"]]), ("g2", ["N-999"]))
        # BOM にもある品番（計算は登録の値を使う）
        self.assertEqual(data["in_bom"], ["N-999"])
        self.assertEqual(self.store.load_group("g2").cost_value("N-999", M), Decimal(100))
        self.assertEqual(self.ok("GET", "/api/manual", "group=g1")["items"], [])
        self.ok("PUT", "/api/manual/items", body={"code": "T-1", "name": "試作", "role": "part", "group_id": "g3"})
        self.assertEqual([i["code"] for i in self.ok("GET", "/api/manual", "group=g3")["items"]], ["T-1"])
        self.assertIn("T-1", self.store.load_group("g3").items)
        self.assertNotIn("T-1", self.store.load_group("g1").items)
        self.assert_error(self.call("PUT", "/api/manual/items", body={"code": "T-2", "name": "x", "group_id": "NOPE"}),
                          404, "NOPE")
        self.assert_error(self.call("PUT", "/api/manual/items", body={"code": "T-2", "name": "x"}), 400, "グループ")
        self.assert_error(self.call("GET", "/api/manual"), 400, "グループ")
        self.ok("DELETE", "/api/manual/items", "code=N-999&group=g2")
        self.assertEqual(self.ok("GET", "/api/manual", "group=g2")["items"], [])

    def test_molds_reuse_part_maps_by_group(self) -> None:
        self.ok("PUT", "/api/molds", body={"part": "B-1", "amount": "1000", "years": "2", "group_id": "g3"})
        data = self.ok("PUT", "/api/molds", body={"part": "B-1", "amount": "4000", "years": "2", "group_id": "g1"})
        self.assertEqual([m["amount"] for m in data["molds"]], ["4000"])
        self.assertEqual([m["amount"] for m in self.ok("GET", "/api/manual", "group=g3")["molds"]], ["1000"])
        self.ok("PUT", "/api/manual/volumes", body={"product": "P-1", "annual": "1000", "group_id": "g1"})
        self.assertEqual(self.ok("GET", "/api/parts/view", "part=B-1&group=g1")["mold"]["amount"], "4000")
        self.assertEqual(self.ok("GET", "/api/parts/view", "part=B-1&group=g3")["mold"]["amount"], "1000")
        self.ok("DELETE", "/api/molds", "part=B-1&group=g1")
        self.assertIsNone(self.ok("GET", "/api/parts/view", "part=B-1&group=g1")["mold"])
        self.ok("PUT", "/api/reuse", body={"part": "B-1", "product": "K-1", "quantity": "1", "group_id": "g3"})
        self.assertEqual(self.ok("GET", "/api/manual", "group=g1")["reuse"], [])
        self.assertEqual(len(self.ok("GET", "/api/manual", "group=g3")["reuse"]), 1)
        rows = self.ok("POST", "/api/partmaps", body={"old_product": "P-1", "new_product": "P-2", "old_part": "B-1",
                                                       "new_part": "B-2", "group_id": "g1"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.ok("GET", "/api/partmaps", "group=g3"), [])
        self.assertEqual(len(self.ok("GET", "/api/partmaps", "group=g1")), 1)


class MasterBookPerGroup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "db.sqlite"
        self.store = Store(self.path)
        self.store.save_group(bom("g1", "D-100"))
        self.store.save_group(bom("g2", "D-100"))
        self.api = Api(self.store)
        self.api.open_file = None

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def ok(self, method: str, path: str, body: object) -> dict:
        response = self.api.handle(method, path, "", json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self.assertLess(response.status, 300, response.body.decode("utf-8"))
        return response.json()

    def test_book_per_group(self) -> None:
        self.ok("PUT", "/api/manual/items", {"code": "N-1", "name": "別のグループの品", "kind": "B", "group_id": "g1"})
        self.ok("PUT", "/api/manual/items", {"code": "N-2", "name": "開発品", "kind": "B", "group_id": "g2"})
        first = Path(self.ok("POST", "/api/master/book", {"group_id": "g1"})["path"])
        own = Path(self.ok("POST", "/api/master/book", {"group_id": "g2"})["path"])
        self.assertEqual((first.name, own.name), ("db-master-g1.xlsx", "db-master-g2.xlsx"))
        wb = load_openpyxl().load_workbook(own)
        self.assertEqual([r[0].value for r in wb["部品"].iter_rows(min_row=2)], ["N-2"])
        wb["部品"].append(["N-3", "開発品3", "B"])
        wb.save(own)
        check = self.ok("POST", "/api/master/check", {"group_id": "g2"})
        self.assertEqual((check["added"], check["changed"]), (1, 0))
        done = self.ok("POST", "/api/master/apply", {"group_id": "g2"})
        self.assertEqual({i["code"] for i in done["manual"]["items"]}, {"N-2", "N-3"})
        self.assertEqual([i.code for i in self.store.load_manual("g1").items.values()], ["N-1"])


if __name__ == "__main__":
    unittest.main()
