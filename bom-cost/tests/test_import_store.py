"""品番の変換・取り込み（CSV／.xlsx・列の対応づけ）・手入力の重ね方。データはすべて架空。"""

import csv
import io
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import example_settings  # noqa: F401
import example_data as ex
from bomcost import (
    MANUAL_VERSION_ID,
    Allocation,
    BomLine,
    ColumnMap,
    CostHead,
    CostRow,
    Item,
    ManualOverlap,
    MissingColumnsError,
    MoldCost,
    Source,
    Version,
    VersionImport,
    Volume,
    VolumeKind,
    allocate_mold_cost,
    base_code,
    mbom_to_ebom,
    overlay_manual,
)
from bomcost.model import split_balloon

M = CostHead.MATERIAL
d = Decimal

ITEM_HEADERS = ["品番", "名称", "区分", "状態", "子あり", "設計者", "備考"]
BOM_HEADERS = ["親品番", "レベル", "子品番", "数量", "風船No", "図面"]
COST_HEADERS = ["原価の品番", "材料費", "直接費", "間接費", "諸経費", "償却費", "製造原価", "登録日"]

ITEM_MAP = {"code": "品番", "name": "名称", "kind": "区分", "lifecycle": "状態", "has_children": "子あり"}
BOM_MAP = {"parent": "親品番", "level": "レベル", "child": "子品番", "quantity": "数量", "balloon": "風船No"}
COST_MAP = {
    "mbom_code": "原価の品番", "material": "材料費", "direct": "直接費", "indirect": "間接費",
    "adjustment": "諸経費", "amortization": "償却費", "manufacturing": "製造原価",
}

ITEM_ROWS = [
    ["AB1234", "製品甲", "S1", "Q", 1, "x", ""],
    ["AB123401", "製品甲の派生", "S1", "Q", 1, "x", ""],
    ["123400", "部品乙", "Z9", "D", 0, "y", "知らない記号"],
]
BOM_ROWS = [
    ["AB1234", 1, "123400", 2, 1, "dwg"],
    ["AB123401", 1, "AB1234", 1, 1, "dwg"],
    ["AB123401", 1, "123400", -1, 2, "dwg"],
]
COST_ROWS = [
    ["AB1234000", "1,600", 10, "", 0, 5, 1615, "x"],
    ["AB1234010", 1600.5, "", "", "", "", "", "x"],
    ["123400000", "0.1", 1, 1, 1, 1, 4.1, "x"],
    ["XYZ-9", 7, "", "", "", "", "", "x"],
]


def write_csv(path: Path, headers, rows, encoding: str) -> None:
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerows([headers] + [["" if v is None else v for v in r] for r in rows])
    path.write_bytes(buffer.getvalue().encode(encoding))


def write_xlsx(path: Path, headers, rows) -> None:
    from bomcost.importer import load_openpyxl

    workbook = load_openpyxl().Workbook()
    ws = workbook.active
    ws.append(headers)
    for row in rows:
        ws.append([None if v == "" else v for v in row])
    workbook.save(path)


class TempDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class CodeConversion(unittest.TestCase):
    def test_mbom_to_ebom(self) -> None:
        self.assertEqual(mbom_to_ebom("AB1234000"), "AB1234")
        self.assertEqual(mbom_to_ebom("AB1234010"), "AB123401")
        self.assertEqual(mbom_to_ebom(123456000), "123456")
        self.assertEqual(mbom_to_ebom("12345699" + "0"), "12345699")

    def test_branch_of_zeros_is_base(self) -> None:
        self.assertEqual(base_code("AB123400"), "AB1234")
        self.assertEqual(base_code("AB123401"), "AB123401")
        self.assertEqual(base_code("AB1234"), "AB1234")

    def test_wrong_digits_rejected(self) -> None:
        for code in ("AB123400", "AB12340000", "AB1234011"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                mbom_to_ebom(code)

    def test_cost_row_from_mbom_keeps_original(self) -> None:
        row = CostRow.from_mbom("AB1234010", {M: 1})
        self.assertEqual((row.code, row.mbom_code), ("AB123401", "AB1234010"))


class SymbolsAndVolumeKind(unittest.TestCase):
    def test_kind_and_lifecycle_are_raw_symbols(self) -> None:
        item = Item("A", "名称", 1, "??", 0)
        self.assertEqual((item.kind, item.lifecycle), ("1", "??"))
        self.assertEqual(Item("A", "名称", "未知の記号").kind, "未知の記号")
        self.assertEqual(Item("A", "名称", None).kind, "")

    def test_volume_kind_from_source(self) -> None:
        self.assertIs(Volume("P", 1).kind, VolumeKind.ACTUAL)
        self.assertIs(Volume("P", 1, Source.MANUAL).kind, VolumeKind.PLAN)

    def test_manual_volume_bears_new_products_only(self) -> None:
        v = Version("t")
        v.add_bom_lines([BomLine("OLD", None, "N", 1), BomLine("NEW", None, "N", 1)])
        v.add_volume(Volume("OLD", 1000))
        manual = Version(MANUAL_VERSION_ID)
        manual.add_volume(Volume("NEW", 500))
        merged = overlay_manual(v, manual)
        a = allocate_mold_cost(merged, MoldCost("N", 1000, 1), Allocation.NEW_PRODUCTS_ONLY)
        self.assertEqual({s.product: s.per_unit for s in a.shares}, {"OLD": d(0), "NEW": d(2)})


class ImportFiles(TempDir):
    def import_all(self, item_path: Path, bom_path: Path, cost_path: Path) -> VersionImport:
        imp = VersionImport(Version("2026-10"))
        imp.add_file(item_path, ColumnMap("item", ITEM_HEADERS, ITEM_MAP))
        imp.add_file(bom_path, ColumnMap("bom_line", BOM_HEADERS, BOM_MAP))
        imp.add_file(cost_path, ColumnMap("cost", COST_HEADERS, COST_MAP))
        return imp

    def make(self, suffix: str, encoding: str = "utf-8", folder: Path | None = None) -> tuple[Path, Path, Path]:
        folder = folder or self.dir
        paths = tuple(folder / f"{name}{suffix}" for name in ("item", "bom", "cost"))
        for path, headers, rows in zip(paths, (ITEM_HEADERS, BOM_HEADERS, COST_HEADERS),
                                       (ITEM_ROWS, BOM_ROWS, COST_ROWS)):
            if suffix == ".csv":
                write_csv(path, headers, rows, encoding)
            else:
                write_xlsx(path, headers, rows)
        return paths

    @staticmethod
    def contents(v: Version):
        return (
            v.items,
            {l.key: l.quantity for l in v.lines},
            {r.code: (r.mbom_code, r.values) for r in v.cost_rows},
        )

    def test_csv_and_xlsx_give_same_version(self) -> None:
        results = []
        for suffix, encoding in ((".csv", "utf-8"), (".csv", "utf-8-sig"), (".csv", "cp932"), (".xlsx", "")):
            with self.subTest(suffix=suffix, encoding=encoding):
                sub = self.dir / f"{suffix[1:]}-{encoding or 'x'}"
                sub.mkdir()
                imp = self.import_all(*self.make(suffix, encoding, sub))
                self.assertEqual(len(imp.warnings), 1)
                results.append(self.contents(imp.version))
        for other in results[1:]:
            self.assertEqual(other, results[0])

    def test_imported_values(self) -> None:
        imp = self.import_all(*self.make(".csv", "cp932"))
        v = imp.version
        self.assertEqual(v.items["123400"].kind, "Z9")
        self.assertEqual(v.items["123400"].lifecycle, "D")
        self.assertIs(v.items["AB1234"].has_children, True)
        self.assertEqual(v.cost_value("AB1234", M), d("1600"))
        self.assertIsNone(v.cost_value("AB1234", CostHead.INDIRECT))
        self.assertEqual(v.cost_value("AB123401", M), d("1600.5"))
        self.assertEqual(v.cost_value("123400", M), d("0.1"))
        self.assertEqual(v.children("AB123401", by_balloon=True)[1].quantity, d(-1))
        self.assertEqual(v.source_name, "item.csv, bom.csv, cost.csv")
        # 桁の合わない 原価の品番は変換せずに取り込み、警告に出る
        self.assertEqual(v.cost_value("XYZ-9", M), d(7))
        self.assertEqual(len(imp.warnings), 1)
        self.assertIn("XYZ-9", imp.warnings[0].message)
        self.assertEqual((imp.warnings[0].file, imp.warnings[0].row), ("cost.csv", 5))

    def test_many_product_files_fold_and_keep_conflicts(self) -> None:
        bom_map = ColumnMap("bom_line", BOM_HEADERS, BOM_MAP)
        p1, p2 = self.dir / "p1.csv", self.dir / "p2.csv"
        write_csv(p1, BOM_HEADERS, [["U1", 1, "C1", 2, 1, ""], ["P1", 1, "U1", 1, 1, ""]], "utf-8")
        write_csv(p2, BOM_HEADERS, [["U1", 1, "C1", 3, 1, ""], ["P2", 1, "U1", 1, 1, ""],
                                    ["U1", 1, "C1", 2, 1, ""]], "utf-8")
        imp = VersionImport(Version("t"))
        imp.add_file(p1, bom_map)
        imp.add_file(p2, bom_map)
        self.assertEqual(len(imp.version.lines), 3)
        self.assertEqual([(c.key, c.quantities) for c in imp.version.conflicts],
                         [(("U1", "1", "C1"), (d(2), d(3)))])

    def test_zero_balloon_is_no_balloon(self) -> None:
        """風船番号の 0（00 も）は、空欄と同じ風船番号なしになる。10 や 01 の 0 は残る。"""
        path = self.dir / "bom.csv"
        write_csv(path, BOM_HEADERS, [["P", 1, "A", 1, 0, ""], ["P", 1, "B", 1, "00", ""], ["P", 1, "C", 1, "", ""],
                                      ["P", 1, "D", 1, 10, ""], ["P", 1, "E", 1, "01", ""]], "utf-8")
        imp = VersionImport(Version("t"))
        imp.add_file(path, ColumnMap("bom_line", BOM_HEADERS, BOM_MAP))
        self.assertEqual({l.child: l.balloon for l in imp.version.lines}, {"A": None, "B": None, "C": None, "D": "10", "E": "01"})

    def test_branch_tells_places_apart(self) -> None:
        """サブ番号は風船番号と同じく箇所を見分ける（同じ風船番号・同じ子でもサブ番号が違えば別の行）。0 と空は無し。風船番号の無い行のサブ番号は持たない。"""
        headers = BOM_HEADERS + ["サブ番号"]
        path = self.dir / "bom.csv"
        write_csv(path, headers, [["P", 1, "A", 1, 10, "", 1], ["P", 1, "A", 2, 10, "", 2], ["P", 1, "B", 1, 10, "", 0],
                                  ["P", 1, "C", 1, "", "", 3]], "utf-8")
        imp = VersionImport(Version("t"))
        imp.add_file(path, ColumnMap("bom_line", headers, {**BOM_MAP, "branch": "サブ番号"}))
        self.assertEqual(sorted((l.child, l.balloon, l.quantity) for l in imp.version.lines),
                         [("A", "10#1", d(1)), ("A", "10#2", d(2)), ("B", "10", d(1)), ("C", None, d(1))])
        self.assertEqual([split_balloon(b) for b in ("10#2", "10", None)], [("10", "2"), ("10", None), (None, None)])

    def test_bad_row_is_skipped_with_warning(self) -> None:
        path = self.dir / "bom.csv"
        write_csv(path, BOM_HEADERS, [["P", 1, "C", "abc", 1, ""], ["P", 1, "D", 1, 2, ""]], "utf-8")
        imp = VersionImport(Version("t"))
        imp.add_file(path, ColumnMap("bom_line", BOM_HEADERS, BOM_MAP))
        self.assertEqual([l.child for l in imp.version.lines], ["D"])
        self.assertEqual(imp.warnings[0].row, 2)

    def test_self_rows_are_dropped_with_one_warning(self) -> None:
        path = self.dir / "bom.csv"
        write_csv(path, BOM_HEADERS, [["P", 0, "P", "", "", ""], ["P", 1, "C", 2, 1, ""], ["Q", 0, "Q", 1, "", ""]],
                  "utf-8")
        imp = VersionImport(Version("t"))
        imp.add_file(path, ColumnMap("bom_line", BOM_HEADERS, BOM_MAP))
        self.assertEqual([(l.parent, l.child) for l in imp.version.lines], [("P", "C")])
        self.assertEqual(len(imp.warnings), 1)
        self.assertIn("2 行外した", imp.warnings[0].message)


class ColumnMapping(TempDir):
    def test_missing_columns_listed(self) -> None:
        with self.assertRaises(MissingColumnsError) as caught:
            ColumnMap("bom_line", ["親品番", "子品番", "その他"], {"parent": "親品番", "child": "子品番"})
        self.assertEqual(caught.exception.missing, ("数量",))
        cm = ColumnMap("cost", COST_HEADERS, COST_MAP)
        with self.assertRaises(MissingColumnsError) as caught:
            cm.check(["材料費", "直接費"])
        self.assertEqual(caught.exception.missing, ("原価の品番",))
        with self.assertRaises(MissingColumnsError) as caught:
            ColumnMap("volume", ["x"], {})
        self.assertEqual(caught.exception.missing, ("製品の品番", "年間台数"))

    def test_extra_columns_ignored(self) -> None:
        path = self.dir / "item.csv"
        write_csv(path, ITEM_HEADERS, ITEM_ROWS, "utf-8")
        imp = VersionImport(Version("t"))
        imp.add_file(path, ColumnMap("item", ITEM_HEADERS, {"code": "品番", "name": "名称"}))
        self.assertEqual(imp.version.items["123400"], Item("123400", "部品乙"))
        self.assertEqual(imp.warnings, [])


class ManualVersion(unittest.TestCase):
    def test_manual_overlays_any_version(self) -> None:
        for v in (ex.version_2026_10(), ex.version_2026_12()):
            self.assertIs(v.items["X-610"].source, Source.MANUAL)
            self.assertEqual(v.cost_value("X-610", M), d(200))
            self.assertIs(v.volume("V-110").kind, VolumeKind.PLAN)
        self.assertIs(ex.version_2026_10().volume("K-200").kind, VolumeKind.ACTUAL)

    def test_master_wins_per_head_and_overlap_reported(self) -> None:
        manual = ex.manual()
        manual.add_cost(CostRow("C-300", {M: 450}))
        manual.add_cost(CostRow("B-200", {CostHead.DIRECT: 7}))
        manual.add_bom_lines([BomLine("A-100", "1", "B-200", 1)])
        manual.add_item(Item("B-200", "部品B（登録）"))
        manual.add_volume(Volume("K-200", 6000))
        v = overlay_manual(ex.imported_2026_10(), manual)
        # 値のある費目はマスタ登録、空の費目は BOM
        self.assertEqual((v.cost_value("C-300", M), v.cost_value("B-200", M), v.cost_value("B-200", CostHead.DIRECT)),
                         (d(450), d(1600), d(7)))
        self.assertIs(v.cost_row("C-300").source, Source.MANUAL)
        # 構成は親ごとにマスタ登録の行だけ
        self.assertEqual([(l.child, l.quantity) for l in v.children("A-100")], [("B-200", d(1))])
        self.assertEqual((v.items["B-200"].name, v.volume("K-200").annual), ("部品B（登録）", d(6000)))
        self.assertEqual(set(v.manual_overlaps),
                         {ManualOverlap("cost", "C-300"), ManualOverlap("bom_line", "A-100"),
                          ManualOverlap("item", "B-200"), ManualOverlap("volume", "K-200")})


if __name__ == "__main__":
    unittest.main()
