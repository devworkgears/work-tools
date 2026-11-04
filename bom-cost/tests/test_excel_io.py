"""Excel の出入口: 取込の表の列・②の費目のブック（書き出し → 打ち替え → 確認 → 決定）・③の分析用のブック。

ソケットは開かない。品番・数値はすべて架空。
"""

import base64
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from bomcost import CostHead, CostRow, Item, BomLine
from bomcost.analysisbook import FACTOR, SHEETS
from bomcost.app import static_response
from bomcost.importer import load_openpyxl
from test_group import NAME, breakdown_version
from test_screens import EmptyDbCase


class ImportTables(EmptyDbCase):
    def test_tables_and_required_fields_without_a_file(self) -> None:
        tables = self.ok("GET", "/api/import/tables")
        self.assertEqual([t["id"] for t in tables], ["item", "bom_line", "cost", "volume"])
        required = {t["id"]: [f["label"] for f in t["fields"] if f["required"]] for t in tables}
        self.assertEqual(required, {"item": ["品番", "名称"], "bom_line": ["親品番", "子品番", "数量"],
                                    "cost": ["MBOM 品番"], "volume": ["製品の品番", "年間台数"]})
        self.assertIn("記号・寸法", [f["label"] for f in tables[0]["fields"]])

    def test_screen_shows_fields_before_preview(self) -> None:
        index = static_response("/").body.decode("utf-8")
        screens = static_response("/screens.js").body.decode("utf-8")
        self.assertIn('id="import-fields"', index)
        self.assertIn("/api/import/tables", screens)
        self.assertIn('name === "import") await enterImport()', screens)


class HeadsBook(EmptyDbCase):
    """②の費目のブック。P-1・P-2 に検討案の試算がある（P-1 は償却費 20・直接費 15、P-2 は償却費 8・直接費は未登録）。"""

    def setUp(self) -> None:
        super().setUp()
        v = breakdown_version()
        v.add_costs([CostRow("P-1", {CostHead.AMORTIZATION: 20, CostHead.DIRECT: 15}),
                     CostRow("P-2", {CostHead.AMORTIZATION: 8})])
        self.store.save_version(v)
        self.folder = tempfile.TemporaryDirectory()
        self.api.master_dir = Path(self.folder.name)
        self.api.open_file = None
        self.body = {"version_id": "2026-11", "name": NAME, "head": "MANUFACTURING"}
        for product in ("P-1", "P-2"):
            self.ok("POST", "/api/group/heads/bulk", body={**self.body, "items": [
                {"product": product, "values": {"PROCESSING": "1"}}]})

    def tearDown(self) -> None:
        self.folder.cleanup()
        super().tearDown()

    def write(self) -> Path:
        r = self.ok("POST", "/api/group/heads/book", body=self.body)
        self.assertFalse(r["opened"])
        return Path(r["path"])

    def edit(self, path: Path, cells: dict[tuple[int, int], object]) -> None:
        wb = load_openpyxl().load_workbook(path)
        for (row, column), value in cells.items():
            wb["費目"].cell(row, column).value = value
        wb.save(path)

    def records(self) -> list[dict]:
        return self.ok("GET", "/api/group/changes", f"version=2026-11&name={quote(NAME)}")["rows"]

    def test_book_has_after_values_and_reimports_unchanged(self) -> None:
        path = self.write()
        self.assertEqual(path.parent, Path(self.folder.name))
        wb = load_openpyxl().load_workbook(path, read_only=True)
        try:
            rows = [list(r) for r in wb["費目"].iter_rows(values_only=True)]
            self.assertEqual(wb.sheetnames, ["費目", "前提"])
        finally:
            wb.close()
        self.assertEqual(rows, [["製品の品番", "名称", "社内加工費", "償却費", "直接費"],
                                ["P-1", "製品1", 1, 20, 15], ["P-2", "製品2", 1, 8, None]])
        check = self.ok("POST", "/api/group/heads/book/check", body=self.body)
        self.assertEqual((check["changed"], check["unchanged"], check["errors"]), (0, 2, []))
        self.assert_error(self.call("POST", "/api/group/heads/book/apply", body=self.body), 400, "変える値が無い")

    def test_changed_value_goes_into_change_log_like_head_edit(self) -> None:
        path = self.write()
        before = {(r["product"], r["sequence"]) for r in self.records()}
        self.edit(path, {(2, 5): 25, (3, 5): 7})  # P-1 の直接費 15 → 25、P-2 の直接費（未登録）→ 7
        check = self.ok("POST", "/api/group/heads/book/check", body=self.body)
        self.assertEqual([(c["row"], c["product"], c["head"], c["current"], c["value"]) for c in check["changes"]],
                         [(2, "P-1", "DIRECT", "15", "25"), (3, "P-2", "DIRECT", None, "7")])
        self.assertEqual((check["products"], check["changed"], check["unchanged"]), (2, 2, 0))
        self.assertEqual(len(self.records()), len(before))  # 確認では何も入れない
        self.ok("POST", "/api/group/heads/book/apply", body=self.body)
        added = [r for r in self.records() if (r["product"], r["sequence"]) not in before]
        # 変わった費目だけを製品ごとに1回の費目変更で（/api/group/heads/bulk と同じ記録）
        self.assertEqual([(r["product"], r["kind"], [(h["id"], h["value"]) for h in r["heads"]]) for r in added],
                         [("P-1", "SET_HEAD", [("DIRECT", "25")]), ("P-2", "SET_HEAD", [("DIRECT", "7")])])
        after = self.ok("GET", "/api/group/heads/all", f"version=2026-11&name={quote(NAME)}")["rows"]
        self.assertEqual([r["after"]["DIRECT"] for r in after], ["25", "7"])
        # 入れた後はブックと同じ
        self.assertEqual(self.ok("POST", "/api/group/heads/book/check", body=self.body)["changed"], 0)

    def test_row_errors_have_numbers_and_block_apply(self) -> None:
        path = self.write()
        wb = load_openpyxl().load_workbook(path)
        ws = wb["費目"]
        ws.cell(2, 3).value = "たくさん"
        ws.cell(3, 3).value = -1
        ws.append(["P-1", "", 2, 20, 15])
        ws.append(["N-6", "", 2, None, None])
        wb.save(path)
        check = self.ok("POST", "/api/group/heads/book/check", body=self.body)
        self.assertEqual([(e["row"], e["message"]) for e in check["errors"]], [
            (2, "社内加工費を数として読めない: 'たくさん'"), (3, "社内加工費は 0 以上にする"),
            (4, "製品 P-1 は 2 行目にもある"), (5, f"製品 N-6 は検討案 {NAME} の試算に無い")])
        before = len(self.records())
        r = self.call("POST", "/api/group/heads/book/apply", body=self.body)
        self.assertEqual(r.status, 400)
        self.assertEqual(len(r.json()["errors"]), 4)
        self.assertEqual(len(self.records()), before)
        # 登録のある値を空にした行も誤り（直した P-1 の変更は確認には出る）
        path = self.write()
        self.edit(path, {(2, 3): 3, (3, 4): None})
        check = self.ok("POST", "/api/group/heads/book/check", body=self.body)
        self.assertEqual([(e["row"], e["message"]) for e in check["errors"]], [(3, "償却費が空（費目変更で未登録には戻せない）")])
        self.assertEqual(check["changed"], 1)

    def test_uploaded_file_other_trial_and_missing_book(self) -> None:
        self.assert_error(self.call("POST", "/api/group/heads/book/check", body=self.body), 404, "ブックがまだ無い")
        self.assert_error(self.call("POST", "/api/group/heads/book/open", body=self.body), 404, "ブックがまだ無い")
        path = self.write()
        self.edit(path, {(2, 3): 9})
        content = base64.b64encode(path.read_bytes()).decode("ascii")
        path.unlink()
        upload = {**self.body, "filename": "打ち替え.xlsx", "content": content}
        check = self.ok("POST", "/api/group/heads/book/check", body=upload)
        self.assertEqual((check["source"], check["changed"]), ("打ち替え.xlsx", 1))
        other = {**upload, "name": "別の検討"}
        self.ok("POST", "/api/group/heads/bulk", body={**other, "items": [{"product": "P-1", "values": {"DIRECT": "1"}}]})
        errors = self.ok("POST", "/api/group/heads/book/check", body=other)["errors"]
        self.assertEqual([(e["sheet"], e["row"]) for e in errors], [("前提", None)])
        self.assertIn(f"検討案 {NAME} のブック", errors[0]["message"])
        self.assert_error(self.call("POST", "/api/group/heads/book/check", body={**upload, "content": "aGVsbG8="}), 400,
                          "Excel のブックとして読めない")
        self.assert_error(self.call("POST", "/api/group/heads/book", body={**self.body, "name": "無い検討"}), 400,
                          "試算のある製品が無い")

    def test_trial_screen_wiring(self) -> None:
        trial = static_response("/trial.js").body.decode("utf-8")
        for path in ("/api/group/heads/book", "/api/group/heads/book/open", "/api/group/heads/book/check",
                     "/api/group/heads/book/apply"):
            self.assertIn(f'"{path}"', trial)
        self.assertIn("① Excel で開く", trial)
        # ②の主ボタンは［③原価を見る →］だけ
        self.assertEqual(trial.count('class: "primary"'), 2)  # 行の変更の［実行］と③へ


class AnalysisBook(EmptyDbCase):
    """③の分析用のブック。版は breakdown_version に製品の償却費・直接費と、組立品 S-2（下位込みの登録値あり）を足したもの。"""

    def setUp(self) -> None:
        super().setUp()
        v = breakdown_version()
        m, a, d = CostHead.MATERIAL, CostHead.AMORTIZATION, CostHead.DIRECT
        v.add_items([Item("S-2", "組立2", "1"), Item("P-3", "製品3", "2")])
        v.add_bom_lines([BomLine("S-2", "1", "N-6", 2), BomLine("P-3", "1", "B-1", 1)])
        v.add_costs([CostRow("P-1", {a: 20, d: 15}), CostRow("P-2", {a: 8}), CostRow("S-2", {m: 50}),
                     CostRow("P-3", {a: 1})])
        self.store.save_version(v)
        self.ok("PUT", "/api/manual/volumes", body={"product": "P-1", "annual": "100"})
        base = {"version_id": "2026-11", "name": NAME, "product": "P-1", "head": "MANUFACTURING"}
        self.ok("POST", "/api/group/changes", body={**base, "kind": "REPLACE", "part": "N-8", "parent": "P-1",
                                                    "balloon": "3", "replacement": "N-6B", "quantity": "2"})
        self.ok("POST", "/api/group/changes", body={**base, "kind": "REPLACE", "part": "N-6", "parent": "S-1",
                                                    "balloon": "1", "replacement": "N-8", "quantity": "4"})
        self.ok("POST", "/api/group/changes", body={**base, "kind": "ADD", "part": "S-2", "quantity": "3"})
        self.ok("POST", "/api/group/heads/bulk", body={**base, "items": [
            {"product": "P-1", "values": {"PROCESSING": "40"}}, {"product": "P-2", "values": {"DIRECT": "7"}}]})

    def book(self, query: str = "") -> dict[str, list[dict]]:
        r = self.call("GET", "/api/group/compare/analysis/xlsx",
                      f"version=2026-11&names={quote(json.dumps([NAME]))}" + query)
        self.assertEqual(r.status, 200, r.body[:300])
        self.disposition = r.headers["Content-Disposition"]
        wb = load_openpyxl().load_workbook(io.BytesIO(r.body))
        self.tables = {ws.title: list(ws.tables) for ws in wb.worksheets}
        sheets = {}
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            sheets[ws.title] = [dict(zip(rows[0], r)) for r in rows[1:]]
        return sheets

    def analysis(self, products: list[str], query: str = "") -> dict:
        return self.ok("GET", "/api/group/compare/analysis", f"version=2026-11&cost=manufacturing"
                       f"&names={quote(json.dumps([NAME]))}&products={quote(json.dumps(products))}" + query)

    @staticmethod
    def dec(value: object) -> Decimal:
        return Decimal(0) if value is None else Decimal(str(value))

    def assert_sums_match_analysis(self, sheets: dict[str, list[dict]], query: str = "") -> None:
        products = [p["品番"] for p in sheets["製品"]]
        a = self.analysis(products, query)
        for label, series in (("現行", a["series"][0]), (NAME, a["series"][1])):
            structure = [r for r in sheets["構成"] if r["検討案"] == label]
            cost = {(r["製品"], r["費目"]): self.dec(r["値"]) for r in sheets["製品原価"] if r["検討案"] == label}
            for code in products:
                part = sum((self.dec(r["製造原価への寄与"]) for r in structure if r["製品"] == code), Decimal(0))
                # 構成の寄与の計は製品原価の材料費小計 × 係数（float のセルなので桁をそろえて比べる）
                self.assertAlmostEqual(float(part), float(cost[(code, "材料費小計")] * FACTOR), places=6)
            parts = sum((self.dec(r["製造原価への寄与"]) for r in structure), Decimal(0))
            heads = sum(((cost[(c, "社内加工費")] + cost[(c, "償却費")]) * FACTOR + cost[(c, "直接費")]
                         for c in products), Decimal(0))
            total = Decimal(series["total"])
            self.assertAlmostEqual(float(parts + heads), float(total), places=6)
            self.assertAlmostEqual(float(sum((cost[(c, "製造原価")] for c in products), Decimal(0))), float(total),
                                   places=6)

    def test_sheets_are_tables_with_numbers(self) -> None:
        sheets = self.book()
        self.assertEqual(list(sheets), list(SHEETS))
        self.assertEqual(self.tables, {s: [t] for s, (t, _) in SHEETS.items()})
        self.assertIn("filename*=UTF-8''" + quote(f"原価分析_2026-11第1版_{NAME}.xlsx"), self.disposition)
        # 試算は P-1・P-2 だけ（P-3 は試算なしなので範囲の外）
        self.assertEqual([p["品番"] for p in sheets["製品"]], ["P-1", "P-2"])
        self.assertEqual(sheets["製品"][0], {"品番": "P-1", "名称": "製品1", "開発番号": "2026-11", "年間台数": 100})
        self.assertEqual(len(sheets["製品原価"]), 2 * 2 * 8)
        self.assertEqual(sheets["検討案"], [{"案名": NAME, "版": "2026-11（第1版）", "変更の件数": 5}])
        self.assertEqual([(r["製品"], r["種類"], r["場所"], r["元"], r["先"], r["値"]) for r in sheets["変更"]], [
            ("P-1", "置換", "P-1 #3", "N-8", "N-6B", 2), ("P-1", "置換", "S-1 #1", "N-6", "N-8", 4),
            ("P-1", "追加", None, "S-2", None, 3), ("P-1", "費目変更", None, "社内加工費", None, 40),
            ("P-2", "費目変更", None, "直接費", None, 7)])
        current = [r for r in sheets["構成"] if r["検討案"] == "現行" and r["製品"] == "P-1"]
        self.assertEqual([(r["親品番"], r["部品"], r["階層"], r["員数"], r["1台あたりの数"], r["材料費"]) for r in current], [
            (None, "P-1", 0, 1, 1, 0), ("P-1", "S-1", 1, 2, 2, 0), ("S-1", "N-6", 2, 4, 8, 80), ("P-1", "N-6", 1, 3, 3, 30),
            ("P-1", "N-8", 1, 2, 2, 40), ("P-1", "B-1", 1, 1, 1, 500), ("P-1", "U-1", 1, 1, 1, None)])
        b1 = next(r for r in current if r["部品"] == "B-1")
        self.assertEqual((b1["材料費の単価"], b1["年間台数"]), (500, 100))
        self.assertAlmostEqual(b1["製造原価への寄与"], float(500 * FACTOR))
        self.assertAlmostEqual(b1["年間の金額"], float(500 * FACTOR * 100))
        self.assertIsInstance(sheets["製品原価"][0]["値"], (int, float))
        premises = {r["項目"]: r["値"] for r in sheets["前提"]}
        self.assertAlmostEqual(premises["材料費から製造原価への係数"], 1.1433)
        self.assertEqual(premises["償却費"], "含めた")

    def test_trial_rows_add_changes_below_place(self) -> None:
        trial = [r for r in self.book()["構成"] if r["検討案"] == NAME and r["製品"] == "P-1"]
        extra = [(r["親品番"], r["部品"], r["階層"], r["員数"], r["1台あたりの数"], r["材料費"]) for r in trial[7:]]
        # 版の構成（土台 ×1）の後ろに変更の行。場所の置換は抜く分が負の数、足した組立品 S-2 は下位込みなので中の N-6 は 0
        self.assertEqual(extra, [("P-1", "N-8", 1, -2, -2, -40), ("P-1", "N-6B", 1, 2, 2, 24),
                                 ("S-1", "N-6", 2, -4, -8, -80), ("S-1", "N-8", 2, 4, 8, 160),
                                 ("P-1", "S-2", 1, 3, 3, 150), ("S-2", "N-6", 2, 2, 6, 0)])

    def test_sums_match_analysis(self) -> None:
        self.assert_sums_match_analysis(self.book())

    def test_ignore_amortization_and_unregistered(self) -> None:
        self.store.save_version(breakdown_version(), replace=True)  # 償却費・直接費とも未登録
        self.assert_error(self.call("GET", "/api/group/compare/analysis/xlsx",
                                    f"version=2026-11&names={quote(json.dumps([NAME]))}"), 400, "償却費")
        sheets = self.book("&ignore_amortization=1")
        self.assertEqual({r["項目"]: r["値"] for r in sheets["前提"]}["償却費"], "無視した（0 として計算）")
        self.assertTrue(all(r["値"] is None for r in sheets["製品原価"] if r["費目"] == "償却費"))
        self.assert_sums_match_analysis(sheets, "&ignore_amortization=1")

    def test_filter_and_compare_screen(self) -> None:
        self.assertEqual([p["品番"] for p in self.book("&q=P-2")["製品"]], ["P-2"])
        compare = static_response("/compare.js").body.decode("utf-8")
        self.assertIn("/api/group/compare/analysis/xlsx", compare)
        self.assertIn("Excel に出す（分析用）", compare)
        self.assertIn("/api/group/compare/table/xlsx", compare)


if __name__ == "__main__":
    unittest.main()
