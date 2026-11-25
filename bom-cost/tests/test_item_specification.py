"""仕様の取込。データはすべて架空。"""

import io
import unittest

import example_settings  # noqa: F401
from bomcost import ColumnMap, Version, VersionImport
from bomcost.importer import TABLE_FIELDS, load_openpyxl, read_table_bytes

try:
    load_openpyxl()
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


class ItemImport(unittest.TestCase):
    def check_import(self, filename: str, data: bytes) -> None:
        table = read_table_bytes(filename, data)
        for mapped in (True, False):
            with self.subTest(mapped=mapped):
                columns = {"code": "品番", "name": "名称"}
                if mapped:
                    columns["specification"] = "仕様"
                version = Version("v1")
                importer = VersionImport(version)
                importer.add_table(table, ColumnMap("item", table.headers, columns))
                self.assertEqual(importer.warnings, [])
                self.assertEqual(version.items["P-1"].specification, "M6 × 20" if mapped else "")
                self.assertEqual(version.items["P-2"].specification, "")

    def test_csv_optional_mapping(self) -> None:
        self.assertFalse(next(f for f in TABLE_FIELDS["item"] if f.name == "specification").required)
        self.check_import("items.csv", "品番,名称,仕様\nP-1,部品,M6 × 20\nP-2,部品,\n".encode())

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl が無い")
    def test_xlsx_optional_mapping(self) -> None:
        wb = load_openpyxl().Workbook()
        wb.active.append(["品番", "名称", "仕様"])
        wb.active.append(["P-1", "部品", "M6 × 20"])
        wb.active.append(["P-2", "部品", None])
        out = io.BytesIO()
        wb.save(out)
        wb.close()
        self.check_import("items.xlsx", out.getvalue())


