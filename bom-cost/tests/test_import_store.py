"""品番の変換・取り込み（CSV／.xlsx・列の対応づけ）・SQLite への保存と読み戻し。データはすべて架空。"""

import csv
import io
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import example_data as ex
from bomcost import (
    MANUAL_VERSION_ID,
    Allocation,
    BomLine,
    Change,
    ChangeKind,
    ColumnMap,
    CostHead,
    CostRow,
    ImportWarning,
    Item,
    ManualOverlap,
    MissingColumnsError,
    MoldCost,
    PartMapRow,
    ReuseTarget,
    Scenario,
    Scope,
    ScopePolicy,
    Source,
    Store,
    Version,
    VersionImport,
    VersionInUseError,
    Volume,
    VolumeKind,
    allocate_mold_cost,
    base_code,
    calculate_cost,
    mbom_to_ebom,
    overlay_manual,
    read_table,
)

M = CostHead.MATERIAL
d = Decimal

ITEM_HEADERS = ["品番", "名称", "区分", "状態", "子あり", "設計者", "備考"]
BOM_HEADERS = ["親品番", "レベル", "子品番", "数量", "風船No", "図面"]
COST_HEADERS = ["MBOM品番", "材料費", "直接費", "間接費", "調整費", "償却費", "製造原価", "登録日"]

ITEM_MAP = {"code": "品番", "name": "名称", "kind": "区分", "lifecycle": "状態", "has_children": "子あり"}
BOM_MAP = {"parent": "親品番", "level": "レベル", "child": "子品番", "quantity": "数量", "balloon": "風船No"}
COST_MAP = {
    "mbom_code": "MBOM品番", "material": "材料費", "direct": "直接費", "indirect": "間接費",
    "adjustment": "調整費", "amortization": "償却費", "manufacturing": "製造原価",
}

ITEM_ROWS = [
    ["AB123456", "製品甲", "S1", "Q", 1, "x", ""],
    ["AB12345601", "製品甲の派生", "S1", "Q", 1, "x", ""],
    ["12340000", "部品乙", "Z9", "D", 0, "y", "知らない記号"],
]
BOM_ROWS = [
    ["AB123456", 1, "12340000", 2, 1, "dwg"],
    ["AB12345601", 1, "AB123456", 1, 1, "dwg"],
    ["AB12345601", 1, "12340000", -1, 2, "dwg"],
]
COST_ROWS = [
    ["AB123456000", "1,600", 10, "", 0, 5, 1615, "x"],
    ["AB123456010", 1600.5, "", "", "", "", "", "x"],
    ["12340000000", "0.1", 1, 1, 1, 1, 4.1, "x"],
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
        self.assertEqual(mbom_to_ebom("AB123456000"), "AB123456")
        self.assertEqual(mbom_to_ebom("AB123456010"), "AB12345601")
        self.assertEqual(mbom_to_ebom(12345678000), "12345678")
        self.assertEqual(mbom_to_ebom("1234567899" + "0"), "1234567899")

    def test_ten_digit_with_00_is_base(self) -> None:
        self.assertEqual(base_code("AB12345600"), "AB123456")
        self.assertEqual(base_code("AB12345601"), "AB12345601")
        self.assertEqual(base_code("AB123456"), "AB123456")

    def test_not_eleven_digits_rejected(self) -> None:
        for code in ("AB12345600", "AB1234560000", "AB123456011"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                mbom_to_ebom(code)

    def test_cost_row_from_mbom_keeps_original(self) -> None:
        row = CostRow.from_mbom("AB123456010", {M: 1})
        self.assertEqual((row.code, row.mbom_code), ("AB12345601", "AB123456010"))


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
        self.assertEqual(v.items["12340000"].kind, "Z9")
        self.assertEqual(v.items["12340000"].lifecycle, "D")
        self.assertIs(v.items["AB123456"].has_children, True)
        self.assertEqual(v.cost_value("AB123456", M), d("1600"))
        self.assertIsNone(v.cost_value("AB123456", CostHead.INDIRECT))
        self.assertEqual(v.cost_value("AB12345601", M), d("1600.5"))
        self.assertEqual(v.cost_value("12340000", M), d("0.1"))
        self.assertEqual(v.children("AB12345601", by_balloon=True)[1].quantity, d(-1))
        self.assertEqual(v.source_name, "item.csv, bom.csv, cost.csv")
        # 11桁でない MBOM 品番は変換せずに取り込み、警告に出る
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
        with Store(":memory:") as store:
            store.save_version(imp.version)
            back = store.load_version("t")
        self.assertEqual(back.conflicts, imp.version.conflicts)

    def test_bad_row_is_skipped_with_warning(self) -> None:
        path = self.dir / "bom.csv"
        write_csv(path, BOM_HEADERS, [["P", 1, "C", "abc", 1, ""], ["P", 1, "D", 1, 2, ""]], "utf-8")
        imp = VersionImport(Version("t"))
        imp.add_file(path, ColumnMap("bom_line", BOM_HEADERS, BOM_MAP))
        self.assertEqual([l.child for l in imp.version.lines], ["D"])
        self.assertEqual(imp.warnings[0].row, 2)


class ColumnMapping(TempDir):
    def test_saved_map_applies_to_same_headers_only(self) -> None:
        with Store(":memory:") as store:
            store.save_column_map(ColumnMap("bom_line", BOM_HEADERS, BOM_MAP))
            path = self.dir / "bom.csv"
            write_csv(path, BOM_HEADERS, BOM_ROWS, "utf-8")
            table = read_table(path)
            found = store.find_column_map(table.headers)
            self.assertIsNotNone(found)
            self.assertEqual((found.table, dict(found.columns)), ("bom_line", BOM_MAP))
            imp = VersionImport(Version("t"))
            imp.add_table(table, found)
            self.assertEqual(len(imp.version.lines), 3)
            # 並びが違えば当たらない
            self.assertIsNone(store.find_column_map(list(reversed(BOM_HEADERS))))
            self.assertIsNone(store.find_column_map(BOM_HEADERS + ["増えた列"]))
            self.assertIsNone(store.find_column_map(BOM_HEADERS, table="cost"))

    def test_missing_columns_listed(self) -> None:
        with self.assertRaises(MissingColumnsError) as caught:
            ColumnMap("bom_line", ["親品番", "子品番", "その他"], {"parent": "親品番", "child": "子品番"})
        self.assertEqual(caught.exception.missing, ("数量",))
        cm = ColumnMap("cost", COST_HEADERS, COST_MAP)
        with self.assertRaises(MissingColumnsError) as caught:
            cm.check(["材料費", "直接費"])
        self.assertEqual(caught.exception.missing, ("MBOM 品番",))
        with self.assertRaises(MissingColumnsError) as caught:
            ColumnMap("volume", ["x"], {})
        self.assertEqual(caught.exception.missing, ("製品の品番", "年間台数"))

    def test_extra_columns_ignored(self) -> None:
        path = self.dir / "item.csv"
        write_csv(path, ITEM_HEADERS, ITEM_ROWS, "utf-8")
        imp = VersionImport(Version("t"))
        imp.add_file(path, ColumnMap("item", ITEM_HEADERS, {"code": "品番", "name": "名称"}))
        self.assertEqual(imp.version.items["12340000"], Item("12340000", "部品乙"))
        self.assertEqual(imp.warnings, [])


class ManualVersion(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.store.save_version(ex.imported_2026_10())
        self.store.save_version(ex.imported_2026_12())
        self.store.save_manual(ex.manual())
        self.store.save_scope_policy(ex.policy())

    def tearDown(self) -> None:
        self.store.close()

    def test_manual_overlays_any_version(self) -> None:
        v10 = self.store.load_version("2026-10")
        v12 = self.store.load_version("2026-12")
        for v in (v10, v12):
            self.assertIs(v.items["X-610"].source, Source.MANUAL)
            self.assertEqual(v.cost_value("X-610", M), d(200))
            self.assertIs(v.volume("V-110").kind, VolumeKind.PLAN)
        self.assertIs(v10.volume("K-200").kind, VolumeKind.ACTUAL)
        self.assertNotIn("X-610", self.store.load_version("2026-10", with_manual=False).items)

    def test_reimport_keeps_manual(self) -> None:
        self.store.save_version(ex.imported_2026_10(), replace=True)
        self.assertEqual(self.store.load_version("2026-10").cost_value("X-610", M), d(200))
        with self.assertRaises(ValueError):
            self.store.save_version(ex.imported_2026_10())

    def test_manual_wins_and_overlap_reported(self) -> None:
        manual = self.store.load_manual()
        manual.add_cost(CostRow("C-300", {M: 450}))
        manual.add_bom_lines([BomLine("A-100", "1", "B-200", 1)])
        self.store.save_manual(manual)
        v = self.store.load_version("2026-10")
        self.assertEqual(v.cost_value("C-300", M), d(450))
        self.assertIs(v.cost_row("C-300").source, Source.MANUAL)
        self.assertEqual([l.child for l in v.children("A-100")], ["B-200"])
        self.assertEqual(set(v.manual_overlaps),
                         {ManualOverlap("cost", "C-300"), ManualOverlap("bom_line", "A-100")})

    def test_manual_cannot_be_deleted_or_saved_as_import(self) -> None:
        with self.assertRaises(ValueError):
            self.store.delete_version(MANUAL_VERSION_ID)
        with self.assertRaises(ValueError):
            self.store.save_version(self.store.load_version("2026-12"), replace=True)
        self.assertEqual([i.version_id for i in self.store.list_versions()], ["2026-12", "2026-10"])


class SaveAndLoad(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "bom.sqlite"
        with Store(self.path) as store:
            imported = ex.imported_2026_10()
            imported.imported_at = datetime(2026, 10, 1, 9, 30)
            imported.source_name = "cost.csv"
            store.save_version(imported)
            store.save_version(ex.imported_2026_12())
            store.save_manual(ex.manual())
            store.save_scope_policy(ex.policy())
            store.save_mold_cost(ex.mold_x610())
            store.add_reuse_target(ex.reuse_k200())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_example_values_after_reload(self) -> None:
        with Store(self.path) as store:
            v10, v12 = store.load_version("2026-10"), store.load_version("2026-12")
            policy = store.load_scope_policy()
            self.assertEqual(calculate_cost(v10, "A-100", M, policy).total, d(4900))
            self.assertEqual(calculate_cost(v10, "V-110", M, policy).total, d(4650))
            self.assertEqual(calculate_cost(v12, "V-110", M, policy).total, d(4690))
            s = Scenario("V-110 検討", "A-100", "V-110", v10, M, policy)
            for change in (Change(ChangeKind.ADD, "F-600"), Change(ChangeKind.REMOVE, "C-300"),
                           Change(ChangeKind.REPLACE, "F-600", replacement="X-610")):
                s.apply(change)
            mold = store.mold_costs()["X-610"]
            shares = allocate_mold_cost(v10, mold, Allocation.BY_USAGE, store.reuse_targets(), s.overrides())
            self.assertEqual((shares.annual_count, shares.per_part), (d(20000), d(60)))
            self.assertEqual(s.calculate().total + shares.share_for("V-110").per_unit, d(4660))
            info = next(i for i in store.list_versions() if i.version_id == "2026-10")
            self.assertEqual((info.version_id, info.imported_at, info.source_name),
                             ("2026-10", datetime(2026, 10, 1, 9, 30), "cost.csv"))

    def test_decimal_and_blank_round_trip(self) -> None:
        v = Version("t")
        v.add_bom_lines([BomLine("P", None, "Q", "0.1"), BomLine("P", "2", "R", -2)])
        v.add_cost(CostRow("Q", {M: "1600.5", CostHead.DIRECT: "", CostHead.INDIRECT: 0}, mbom_code="Q0"))
        v.add_volume(Volume("P", "0.1"))
        with Store(self.path) as store:
            store.save_version(v)
            back = store.load_version("t", with_manual=False)
        self.assertEqual([(l.key, l.quantity) for l in back.lines], [(l.key, l.quantity) for l in v.lines])
        self.assertEqual(str(back.lines[0].quantity), "0.1")
        self.assertEqual(back.lines[1].quantity, d(-2))
        row = back.cost_row("Q")
        self.assertEqual(str(row.value(M)), "1600.5")
        self.assertIsNone(row.value(CostHead.DIRECT))
        self.assertIsNone(row.value(CostHead.MANUFACTURING))
        self.assertEqual(row.value(CostHead.INDIRECT), d(0))
        self.assertEqual(row.mbom_code, "Q0")
        self.assertEqual(back.volume("P").annual, d("0.1"))

    def test_settings_round_trip(self) -> None:
        with Store(self.path) as store:
            store.add_part_map("A-100", "V-110", "C-300", "X-610")
            store.set_label("kind", "P", "製品")
            policy = ScopePolicy({CostHead.DIRECT: Scope.OWN_ONLY})
            policy.set_item("A-100", M, Scope.OWN_ONLY)
            store.save_scope_policy(policy)
        with Store(self.path) as store:
            self.assertEqual(store.part_map("A-100", "V-110"), {"C-300": "X-610"})
            self.assertEqual(store.labels("kind"), {"P": "製品"})
            loaded = store.load_scope_policy()
            self.assertIs(loaded.scope_for("A-100", M), Scope.OWN_ONLY)
            self.assertIs(loaded.scope_for("B-200", M), Scope.INCLUDES_CHILDREN)
            self.assertIs(loaded.scope_for("B-200", CostHead.DIRECT), Scope.OWN_ONLY)
            self.assertEqual(store.reuse_targets("X-610"), [ex.reuse_k200()])
            self.assertEqual(store.mold_costs(), {"X-610": ex.mold_x610()})


class VersionsAndScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.store.save_version(ex.imported_2026_10())
        self.store.save_version(ex.imported_2026_12())
        self.store.save_manual(ex.manual())
        self.store.save_scope_policy(ex.policy())

    def tearDown(self) -> None:
        self.store.close()

    def scenario(self) -> Scenario:
        s = Scenario("V-110 検討", "A-100", "V-110", self.store.load_version("2026-10"), M,
                     self.store.load_scope_policy(), note="架空")
        times = [datetime(2026, 10, 29, 10, i) for i in range(3)]
        s.apply(Change(ChangeKind.ADD, "F-600"), times[0])
        s.apply(Change(ChangeKind.REMOVE, "C-300", "1.5"), times[1])
        s.apply(Change(ChangeKind.REPLACE, "F-600", replacement="X-610"), times[2])
        return s

    def test_scenario_round_trip(self) -> None:
        s = self.scenario()
        scenario_id = self.store.save_scenario(s)
        back = self.store.load_scenario(scenario_id)
        self.assertEqual(back.history, s.history)
        self.assertEqual((back.name, back.note, back.base, back.version.version_id),
                         ("V-110 検討", "架空", "A-100", "2026-10"))
        self.assertEqual((back.product_code, back.head), (s.product_code, s.head))
        self.assertEqual(back.calculate().total, s.calculate().total)
        self.assertEqual(self.store.load_scenario(scenario_id).calculate().total, s.calculate().total)
        self.assertEqual(back.compare_versions(self.store.load_version("2026-10"),
                                               self.store.load_version("2026-12")).total_b,
                         s.compare_versions(self.store.load_version("2026-10"),
                                            self.store.load_version("2026-12")).total_b)
        # 取り消して保存し直すと履歴も縮む
        back.undo()
        self.store.save_scenario(back)
        self.assertEqual(len(self.store.load_scenario(scenario_id).history), 2)

    def test_versions_are_not_deleted_while_used(self) -> None:
        self.store.save_scenario(self.scenario())
        with self.assertRaises(VersionInUseError) as caught:
            self.store.delete_version("2026-10")
        self.assertIn("V-110 検討", str(caught.exception))
        self.assertEqual(caught.exception.scenarios, ("V-110 検討",))
        self.store.delete_version("2026-12")
        self.assertEqual([i.version_id for i in self.store.list_versions()], ["2026-10"])
        with self.assertRaises(KeyError):
            self.store.load_version("2026-12")

    def test_versions_are_kept_without_limit(self) -> None:
        for n in range(15):
            self.store.save_version(Version(f"v{n:02d}", datetime(2027, 1, 1 + n)))
        self.assertEqual(len(self.store.list_versions()), 17)


class MissingColumns(unittest.TestCase):
    """設計書の ER 図にあって落ちていた欄（台数・載せ方・風船番号・検証の結果）。"""

    setUp = VersionsAndScenarios.setUp
    tearDown = VersionsAndScenarios.tearDown
    scenario = VersionsAndScenarios.scenario

    def test_change_keeps_volume_and_allocation(self) -> None:
        s = self.scenario()  # 台数・載せ方を渡さない呼び方
        self.assertEqual([(r.volume, r.allocation) for r in s.history], [(None, None)] * 3)
        s.apply(Change(ChangeKind.ADD, "F-600"), datetime(2026, 10, 29, 11, 0),
                volume="1200", allocation=Allocation.BY_USAGE)
        scenario_id = self.store.save_scenario(s)
        back = self.store.load_scenario(scenario_id)
        self.assertEqual(back.history, s.history)
        self.assertEqual([(r.volume, r.allocation) for r in back.history],
                         [(None, None)] * 3 + [(Decimal(1200), Allocation.BY_USAGE)])

    def test_part_map_balloons(self) -> None:
        self.store.add_part_map("A-100", "A-200", "C-300", "C-310", "12", "15")
        self.store.add_part_map("A-100", "A-200", "D-400", "D-410")
        self.assertEqual(self.store.part_map("A-100", "A-200"), {"C-300": "C-310", "D-400": "D-410"})
        self.assertEqual(
            self.store.part_map_rows("A-100", "A-200"),
            [PartMapRow("C-300", "C-310", "12", "15"), PartMapRow("D-400", "D-410", None, None)],
        )

    def test_version_warnings_round_trip(self) -> None:
        imp = VersionImport(Version("2027-01", datetime(2027, 1, 5)))
        imp.warnings.append(ImportWarning("bom.csv", 3, "数量が空"))
        imp.warnings.append(ImportWarning("cost.csv", None, "見出しが多い"))
        self.store.save_version(imp.version, warnings=imp.warnings)
        self.assertEqual(self.store.load_version("2027-01").warnings, tuple(imp.warnings))
        counts = {i.version_id: i.warning_count for i in self.store.list_versions()}
        self.assertEqual((counts["2027-01"], counts["2026-10"]), (2, 0))
        self.assertEqual(self.store.load_version("2026-10").warnings, ())
        # 入れ替えると検証の結果も入れ替わる
        self.store.save_version(Version("2027-01", datetime(2027, 1, 6)), replace=True)
        self.assertEqual(self.store.load_version("2027-01").warnings, ())


if __name__ == "__main__":
    unittest.main()
