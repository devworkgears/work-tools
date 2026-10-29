"""マスタ登録を開発番号ごと＋共通に分ける・同じ品番は BOM ＞ 開発番号の登録 ＞ 共通の登録。データはすべて架空。"""

import json
import sqlite3
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
    Source,
    Store,
    Version,
    Volume,
    calculate_cost,
)
from bomcost.api import Api
from bomcost.importer import load_openpyxl
from test_screens import EmptyDbCase

M = CostHead.MATERIAL


def bom(version_id: str, dev: str, revision: int, extra_cost: dict[str, int] | None = None) -> Version:
    """製品 P-1 ＝ 部品 B-1 ×2（＋ extra_cost の品番 ×1。その品番は品目・原価も BOM に入る）。"""
    v = Version(version_id, dev_number=dev, revision=revision)
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
        self.store.save_version(bom("D-100-r1", "D-100", 1))
        self.store.save_version(bom("D-100-r2", "D-100", 2, {"N-999": 120}))
        self.store.save_version(bom("D-200-r1", "D-200", 1))

    def tearDown(self) -> None:
        self.store.close()

    def total(self, version_id: str, code: str = "P-1") -> Decimal:
        return calculate_cost(self.store.load_version(version_id), code, M).total

    def test_example_n999_bom_wins_from_its_revision(self) -> None:
        own = registration(("N-999", 100))
        own.add_bom_lines([BomLine("T-1", None, "N-999", 1)])  # 試作の製品に N-999 を使う
        self.store.save_manual(own, "D-100")
        r1, r2 = self.store.load_version("D-100-r1"), self.store.load_version("D-100-r2")
        self.assertEqual((r1.cost_value("N-999", M), r2.cost_value("N-999", M)), (Decimal(100), Decimal(120)))
        self.assertIs(r1.cost_row("N-999").source, Source.MANUAL)
        self.assertIs(r2.cost_row("N-999").source, Source.IMPORTED)
        self.assertEqual((self.total("D-100-r1", "T-1"), self.total("D-100-r2", "T-1")), (Decimal(100), Decimal(120)))
        self.assertEqual((self.total("D-100-r1"), self.total("D-100-r2")), (Decimal(20), Decimal(140)))

    def test_other_dev_number_does_not_overlay(self) -> None:
        self.store.save_manual(registration(("N-999", 100)), "D-100")
        other = self.store.load_version("D-200-r1")
        self.assertNotIn("N-999", other.items)
        self.assertIsNone(other.cost_value("N-999", M))

    def test_order_bom_then_dev_then_common_for_each_table(self) -> None:
        common = registration(("N-999", 1), ("C-1", 2), ("B-1", 3))
        common.add_bom_lines([BomLine("C-1", None, "X-1", 9), BomLine("Q-1", None, "X-1", 9), BomLine("P-1", None, "X-1", 9)])
        common.add_volume(Volume("P-1", 9))
        common.add_volume(Volume("Q-1", 9))
        common.add_volume(Volume("C-1", 9))
        dev = registration(("N-999", 100))
        dev.add_bom_lines([BomLine("Q-1", None, "X-2", 1)])
        dev.add_volume(Volume("Q-1", 50))
        self.store.save_manual(common)
        self.store.save_manual(dev, "D-100")
        r1 = self.store.load_version("D-100-r1")
        # 品目・原価: BOM（B-1）＞ 開発番号（N-999）＞ 共通（C-1）
        self.assertEqual({c: r1.cost_value(c, M) for c in ("B-1", "N-999", "C-1")},
                         {"B-1": Decimal(10), "N-999": Decimal(100), "C-1": Decimal(2)})
        self.assertEqual((r1.items["B-1"].name, r1.items["N-999"].name), ("部品B", "登録の品目"))
        # 構成: 親の単位で BOM（P-1）＞ 開発番号（Q-1）＞ 共通（C-1）
        self.assertEqual([l.child for l in r1.children("P-1")], ["B-1"])
        self.assertEqual([l.child for l in r1.children("Q-1")], ["X-2"])
        self.assertEqual([l.child for l in r1.children("C-1")], ["X-1"])
        # 年間台数: 開発番号 ＞ 共通
        self.assertEqual((r1.volume("Q-1").annual, r1.volume("C-1").annual, r1.volume("P-1").annual),
                         (Decimal(50), Decimal(9), Decimal(9)))
        # 別の開発番号には共通だけが重なる
        d200 = self.store.load_version("D-200-r1")
        self.assertEqual((d200.cost_value("N-999", M), d200.volume("Q-1").annual), (Decimal(1), Decimal(9)))
        self.assertEqual([l.child for l in d200.children("Q-1")], ["X-1"])
        # BOM に隠れた登録は重なりに残す
        self.assertIn(("cost", "B-1"), {(o.table, o.code) for o in r1.manual_overlaps})
        self.assertIn(("bom_line", "P-1"), {(o.table, o.code) for o in r1.manual_overlaps})

    def test_manual_versions_are_not_listed_or_deletable(self) -> None:
        self.store.save_manual(registration(("N-999", 100)), "D-100")
        self.assertEqual({v.version_id for v in self.store.list_versions()}, {"D-100-r1", "D-100-r2", "D-200-r1"})
        with self.assertRaises(ValueError):
            self.store.delete_version("manual:D-100")
        self.assertEqual(self.store.master_dev_numbers(), ["D-100"])

    def test_bom_entries_first_revision(self) -> None:
        self.assertEqual(self.store.bom_entries(["N-999", "B-1", "Z-0"], "D-100"),
                         {"N-999": [("D-100", 2)], "B-1": [("D-100", 1)]})
        self.assertEqual(self.store.bom_entries(["B-1"]), {"B-1": [("D-100", 1), ("D-200", 1)]})

    def test_molds_reuse_part_maps_per_dev_and_common(self) -> None:
        s = self.store
        s.save_mold_cost(MoldCost("B-1", 1000, 2))
        s.save_mold_cost(MoldCost("B-1", 3000, 3), "D-100")
        s.save_mold_cost(MoldCost("N-999", 500, 1), "D-200")
        self.assertEqual(s.mold_costs_for("D-100")["B-1"].amount, Decimal(3000))
        self.assertEqual(s.mold_costs_for("D-200")["B-1"].amount, Decimal(1000))
        self.assertNotIn("N-999", s.mold_costs_for("D-100"))
        self.assertEqual(set(s.mold_costs()), {"B-1"})
        s.add_reuse_target(ReuseTarget("B-1", "K-1", 1))
        s.add_reuse_target(ReuseTarget("B-1", "K-1", 4), "D-100")
        s.add_reuse_target(ReuseTarget("B-1", "K-2", 1), "D-200")
        self.assertEqual([(t.product, t.quantity_per_unit) for t in s.reuse_targets_for("D-100", "B-1")],
                         [("K-1", Decimal(4))])
        self.assertEqual([t.product for t in s.reuse_targets_for("D-200")], ["K-2", "K-1"])
        s.add_part_map("P-0", "P-1", "B-0", "B-1")
        s.add_part_map("P-0", "P-1", "B-0", "B-2", dev_number="D-100")
        s.add_part_map("P-0", "P-1", "C-0", "C-1", dev_number="D-200")
        self.assertEqual(s.part_map_for("D-100", "P-0", "P-1"), {"B-0": "B-2"})
        self.assertEqual(s.part_map_for("D-200", "P-0", "P-1"), {"B-0": "B-1", "C-0": "C-1"})
        self.assertEqual(s.part_map("P-0", "P-1"), {"B-0": "B-1"})
        self.assertEqual(s.delete_part_map("P-0", "P-1", "B-0", "B-2"), 0)
        self.assertEqual(s.delete_part_map("P-0", "P-1", "B-0", "B-2", dev_number="D-100"), 1)


OLD_SCHEMA = """
CREATE TABLE version (version_id TEXT PRIMARY KEY, imported_at TEXT, source_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '', warnings TEXT);
CREATE TABLE mold_cost (part TEXT PRIMARY KEY, amount TEXT NOT NULL, years TEXT NOT NULL);
CREATE TABLE reuse_target (part TEXT NOT NULL, product TEXT NOT NULL, quantity TEXT NOT NULL, replaced_part TEXT);
CREATE TABLE part_map (old_product TEXT NOT NULL, new_product TEXT NOT NULL, old_part TEXT NOT NULL,
    new_part TEXT NOT NULL, old_balloon TEXT, new_balloon TEXT);
CREATE TABLE item (version_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
    lifecycle TEXT NOT NULL, has_children INTEGER NOT NULL, PRIMARY KEY (version_id, code));
CREATE TABLE cost (version_id TEXT NOT NULL, code TEXT NOT NULL, mbom_code TEXT, material TEXT, direct TEXT,
    indirect TEXT, adjustment TEXT, amortization TEXT, manufacturing TEXT, PRIMARY KEY (version_id, code));
INSERT INTO version (version_id, note) VALUES ('manual', '手入力');
INSERT INTO item VALUES ('manual', 'N-999', '登録の品目', 'B', '', 0);
INSERT INTO cost (version_id, code, material) VALUES ('manual', 'N-999', '100');
INSERT INTO mold_cost VALUES ('N-999', '2400', '2');
INSERT INTO reuse_target VALUES ('N-999', 'K-1', '2', NULL);
INSERT INTO part_map VALUES ('P-0', 'P-1', 'B-0', 'B-1', NULL, NULL);
"""


class OldDb(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "old.sqlite"
        conn = sqlite3.connect(self.path)
        conn.executescript(OLD_SCHEMA)
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_old_registrations_become_common_and_results_stay(self) -> None:
        with Store(self.path) as store:
            store.save_version(bom("2026-10", "", 1))
            self.assertEqual(list(store.load_manual().items), ["N-999"])
            self.assertEqual(store.load_version("2026-10").cost_value("N-999", M), Decimal(100))
            self.assertEqual(store.mold_costs(), {"N-999": MoldCost("N-999", 2400, 2)})
            self.assertEqual(store.mold_costs_for("2026-10"), {"N-999": MoldCost("N-999", 2400, 2)})
            self.assertEqual(store.reuse_targets(), [ReuseTarget("N-999", "K-1", 2)])
            self.assertEqual(store.part_map_for("2026-10", "P-0", "P-1"), {"B-0": "B-1"})
            # 開発番号ごとの型費は同じ部品でも別の行として入る（主キーが開発番号つきになった）
            store.save_mold_cost(MoldCost("N-999", 1, 1), "2026-10")
            self.assertEqual(store.mold_costs()["N-999"].amount, Decimal(2400))
        with Store(self.path) as store:  # 2回目に開いても移し直さない
            self.assertEqual(store.mold_costs_for("2026-10")["N-999"].amount, Decimal(1))
            self.assertEqual(store.load_version("2026-10").cost_value("N-999", M), Decimal(100))


class ManualApi(EmptyDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.save_version(bom("D-100-r1", "D-100", 1))
        self.store.save_version(bom("D-100-r2", "D-100", 2, {"N-999": 120}))
        self.store.save_version(bom("D-200-r1", "D-200", 1))

    def test_registration_goes_to_selected_dev_number(self) -> None:
        data = self.ok("PUT", "/api/manual/items", body={"code": "N-999", "name": "仮", "kind": "B",
                                                         "costs": {"MATERIAL": "100"}, "dev_number": "D-100"})
        self.assertEqual((data["dev_number"], [i["code"] for i in data["items"]]), ("D-100", ["N-999"]))
        self.assertEqual(data["in_bom"], {"N-999": [{"dev_number": "D-100", "revision": 2}]})
        self.assertEqual(set(data["dev_numbers"]), {"D-100", "D-200"})
        self.assertEqual(self.ok("GET", "/api/manual")["items"], [])
        self.assertEqual(self.ok("GET", "/api/manual", "dev_number=D-200")["items"], [])
        # 版で送ればその版の開発番号に入る（検討案の画面の試作品の登録）
        self.ok("PUT", "/api/manual/items", body={"code": "T-1", "name": "試作", "role": "part", "version": "D-200-r1"})
        self.assertEqual([i["code"] for i in self.ok("GET", "/api/manual", "dev_number=D-200")["items"]], ["T-1"])
        self.assertIn("T-1", self.store.load_version("D-200-r1").items)
        self.assertNotIn("T-1", self.store.load_version("D-100-r1").items)
        self.assert_error(self.call("PUT", "/api/manual/items", body={"code": "T-2", "name": "x", "version": "NOPE"}),
                          404, "NOPE")
        self.ok("DELETE", "/api/manual/items", "code=N-999&dev_number=D-100")
        self.assertEqual(self.ok("GET", "/api/manual", "dev_number=D-100")["items"], [])

    def test_molds_reuse_part_maps_by_dev_number(self) -> None:
        self.ok("PUT", "/api/molds", body={"part": "B-1", "amount": "1000", "years": "2"})
        data = self.ok("PUT", "/api/molds", body={"part": "B-1", "amount": "4000", "years": "2", "dev_number": "D-100"})
        self.assertEqual([m["amount"] for m in data["molds"]], ["4000"])
        self.assertEqual([m["amount"] for m in self.ok("GET", "/api/manual")["molds"]], ["1000"])
        self.ok("PUT", "/api/manual/volumes", body={"product": "P-1", "annual": "1000"})
        view = self.ok("GET", "/api/parts/view", "part=B-1&version=D-100-r1")
        self.assertEqual(view["mold"]["amount"], "4000")
        view = self.ok("GET", "/api/parts/view", "part=B-1&version=D-200-r1")
        self.assertEqual(view["mold"]["amount"], "1000")
        self.ok("DELETE", "/api/molds", "part=B-1&dev_number=D-100")
        self.assertEqual(self.ok("GET", "/api/parts/view", "part=B-1&version=D-100-r1")["mold"]["amount"], "1000")
        self.ok("PUT", "/api/reuse", body={"part": "B-1", "product": "K-1", "quantity": "1", "dev_number": "D-200"})
        self.assertEqual(self.ok("GET", "/api/manual")["reuse"], [])
        self.assertEqual(len(self.ok("GET", "/api/manual", "dev_number=D-200")["reuse"]), 1)
        rows = self.ok("POST", "/api/partmaps", body={"old_product": "P-1", "new_product": "P-2", "old_part": "B-1",
                                                       "new_part": "B-2", "dev_number": "D-100"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.ok("GET", "/api/partmaps"), [])
        self.assertEqual(len(self.ok("GET", "/api/partmaps", "dev_number=D-100")), 1)


class MasterBookPerDev(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "db.sqlite"
        self.store = Store(self.path)
        self.store.save_version(bom("D-100-r1", "D-100", 1))
        self.api = Api(self.store)
        self.api.open_file = None

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def ok(self, method: str, path: str, body: object) -> dict:
        response = self.api.handle(method, path, "", json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self.assertLess(response.status, 300, response.body.decode("utf-8"))
        return response.json()

    def test_book_per_dev_number(self) -> None:
        self.ok("PUT", "/api/manual/items", {"code": "N-1", "name": "共通品", "kind": "B"})
        self.ok("PUT", "/api/manual/items", {"code": "N-2", "name": "開発品", "kind": "B", "dev_number": "D-100"})
        common = Path(self.ok("POST", "/api/master/book", {})["path"])
        own = Path(self.ok("POST", "/api/master/book", {"dev_number": "D-100"})["path"])
        self.assertEqual((common.name, own.name), ("db-master.xlsx", "db-master-D-100.xlsx"))
        wb = load_openpyxl().load_workbook(own)
        self.assertEqual([r[0].value for r in wb["部品"].iter_rows(min_row=2)], ["N-2"])
        wb["部品"].append(["N-3", "開発品3", "B"])
        wb.save(own)
        check = self.ok("POST", "/api/master/check", {"dev_number": "D-100"})
        self.assertEqual((check["added"], check["changed"]), (1, 0))
        done = self.ok("POST", "/api/master/apply", {"dev_number": "D-100"})
        self.assertEqual({i["code"] for i in done["manual"]["items"]}, {"N-2", "N-3"})
        self.assertEqual([i.code for i in self.store.load_manual().items.values()], ["N-1"])


if __name__ == "__main__":
    unittest.main()
