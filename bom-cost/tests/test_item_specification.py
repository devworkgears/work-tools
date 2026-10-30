"""記号・寸法の移行・取込・登録・照会。データはすべて架空。"""

import io
from contextlib import closing
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bomcost import ColumnMap, Item, Store, Version, VersionImport
from bomcost.importer import TABLE_FIELDS, load_openpyxl, read_table_bytes
from bomcost.masterbook import SHEETS, apply_plan, book_bytes, plan_book
from bomcost.store import SCHEMA
from test_group import SampleDbCase

try:
    load_openpyxl()
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


class ItemMigration(unittest.TestCase):
    def test_old_database_adds_empty_column_and_preserves_other_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(SCHEMA.replace("    specification TEXT NOT NULL DEFAULT '',\n", ""))
                db.execute("INSERT INTO version (version_id, dev_number) VALUES ('v1', 'D-1')")
                db.execute("INSERT INTO item VALUES ('v1', 'P-1', '部品', 'B', '', 0)")
                before = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name != 'item'"))
            for _ in range(2):
                with Store(path) as store:
                    version = store.load_version("v1", with_manual=False)
                    self.assertEqual(version.items["P-1"].specification, "")
                    version.add_item(Item("P-2", "部品", specification="M6 × 20"))
                    store.save_version(version, replace=True)
                    self.assertEqual(store.load_version("v1").items["P-2"].specification, "M6 × 20")
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(dict(db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name != 'item'")), before)
                columns = {r[1]: r for r in db.execute("PRAGMA table_info(item)")}
                self.assertEqual(len(columns), 7)
                self.assertEqual(columns["specification"][3:5], (1, "''"))


class ItemImport(unittest.TestCase):
    def check_import(self, filename: str, data: bytes) -> None:
        table = read_table_bytes(filename, data)
        for mapped in (True, False):
            with self.subTest(mapped=mapped):
                columns = {"code": "品番", "name": "名称"}
                if mapped:
                    columns["specification"] = "記号・寸法"
                version = Version("v1")
                importer = VersionImport(version)
                importer.add_table(table, ColumnMap("item", table.headers, columns))
                self.assertEqual(importer.warnings, [])
                self.assertEqual(version.items["P-1"].specification, "M6 × 20" if mapped else "")
                self.assertEqual(version.items["P-2"].specification, "")

    def test_csv_optional_mapping(self) -> None:
        self.assertFalse(next(f for f in TABLE_FIELDS["item"] if f.name == "specification").required)
        self.check_import("items.csv", "品番,名称,記号・寸法\nP-1,部品,M6 × 20\nP-2,部品,\n".encode())

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl が無い")
    def test_xlsx_optional_mapping(self) -> None:
        wb = load_openpyxl().Workbook()
        wb.active.append(["品番", "名称", "記号・寸法"])
        wb.active.append(["P-1", "部品", "M6 × 20"])
        wb.active.append(["P-2", "部品", None])
        out = io.BytesIO()
        wb.save(out)
        wb.close()
        self.check_import("items.xlsx", out.getvalue())


class ItemSpecificationApi(SampleDbCase):
    def test_register_products_parts_and_lookup_candidates(self) -> None:
        for role, code, spec, costs in (("product", "X-1", "φ20", {"MATERIAL": "0", "MANUFACTURING": "12.5"}),
                                         ("part", "X-2", "M6", {})):
            result = self.ok("PUT", "/api/manual/items", body={
                "dev_number": "2026-10", "role": role, "code": code, "name": "照会用", "specification": spec,
                "costs": costs})
            item = next(i for i in result["items"] if i["code"] == code)
            self.assertEqual((item["role"], item["specification"]), (role, spec))
            self.assertEqual(self.store.load_manual("2026-10").items[code].specification, spec)
            self.assertNotIn(code, self.store.load_manual().items)
        candidates = self.ok("GET", "/api/lookup", "version=2026-10&q=照会用")["candidates"]
        self.assertEqual(candidates, [
            {"code": "X-1", "name": "照会用", "specification": "φ20", "material": "0", "manufacturing": "12.5"},
            {"code": "X-2", "name": "照会用", "specification": "M6", "material": None, "manufacturing": None}])
        item = self.ok("GET", "/api/lookup", "version=2026-10&q=X-1")["item"]
        self.assertEqual(item["specification"], "φ20")
        self.ok("PUT", "/api/manual/items", body={"dev_number": "2026-10", "code": "X-1", "name": "照会用", "specification": ""})
        self.assertEqual(self.store.load_manual("2026-10").items["X-1"].specification, "")

    def test_structure_filter_and_comparison_labels(self) -> None:
        version = self.store.load_version("2026-10", with_manual=False)
        for code in ("A-100", "B-200"):
            version.add_item(replace(version.items[code], specification=f"寸法 {code}"))
        self.store.save_version(version, replace=True)
        self.assertEqual(self.parts("&part=B-200")["rows"][0]["part_specification"], "寸法 B-200")
        tree = self.ok("GET", "/api/group/tree", "version=2026-10&product=A-100")
        self.assertEqual(next(n for n in tree["children"] if n["code"] == "B-200")["specification"], "寸法 B-200")
        self.change("A-100", kind="REPLACE", part="B-200", replacement="X-610", quantity="2")
        table = self.ok("GET", "/api/group/compare/table", 'version=2026-10&names=["群の検討"]&ignore_amortization=1')
        self.assertEqual(table["products"][0]["specification"], "寸法 A-100")


class MasterSpecification(unittest.TestCase):
    def test_plan_and_apply_specification_only_change(self) -> None:
        with Store(":memory:") as store:
            manual = Version("manual")
            manual.add_item(Item("P-1", "製品", "S", specification="旧寸法"))
            store.save_manual(manual)
            sheets = {name: [list(headers)] for name, headers in SHEETS.items()}
            sheets["製品"].append(["P-1", "製品", *([None] * (len(SHEETS["製品"]) - 3)), "新寸法"])
            with patch("bomcost.masterbook._read_sheets", return_value=sheets):
                plan = plan_book(store, "S", b"")
                self.assertEqual(plan.errors, [])
                self.assertEqual(plan.total("changed"), 1)
                apply_plan(store, plan)
                self.assertEqual(store.load_manual().items["P-1"].specification, "新寸法")
                self.assertEqual(plan_book(store, "S", b"").total("unchanged"), 1)

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl が無い")
    def test_excel_round_trip_products_parts_and_blank(self) -> None:
        with Store(":memory:") as source, Store(":memory:") as target:
            manual = Version("manual")
            manual.add_items([Item("P-1", "製品", "S", specification="φ20"),
                              Item("P-2", "部品", "B", specification="M6 × 20"), Item("P-3", "空欄", "B")])
            source.save_manual(manual, "D-1")
            data = book_bytes(source, "S", "D-1")
            plan = plan_book(target, "S", data, "D-1")
            self.assertEqual(plan.errors, [])
            apply_plan(target, plan)
            self.assertEqual({c: i.specification for c, i in target.load_manual("D-1").items.items()},
                             {"P-1": "φ20", "P-2": "M6 × 20", "P-3": ""})
            again = plan_book(target, "S", book_bytes(target, "S", "D-1"), "D-1")
            self.assertEqual((again.total("added"), again.total("changed"), again.total("unchanged")), (0, 0, 3))
