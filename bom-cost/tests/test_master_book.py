"""マスタ登録の Excel のブック（書き出し → 打ち込み → 確認 → 取り込み）。ソケットは開かない。データはすべて架空。"""

import base64
import unittest
from pathlib import Path
from unittest import mock

from bomcost import Store
from bomcost.api import Api
from bomcost.importer import load_openpyxl
from bomcost.masterbook import SHEETS
from test_app import DemoDbCase


class MasterBook(DemoDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.api.open_file = None
        for code, name in (("MP-1", "新製品1"), ("MP-2", "新製品2"), ("MP-3", "新製品3")):
            self.ok("PUT", "/api/manual/items", body={"role": "product", "code": code, "name": name,
                                                      "costs": {"MATERIAL": "100"}})
        for code, name in (("MQ-1", "部品1"), ("MQ-2", "部品2"), ("MQ-3", "部品3")):
            self.ok("PUT", "/api/manual/items", body={"role": "part", "code": code, "name": name, "kind": "B",
                                                      "costs": {"MATERIAL": "10", "DIRECT": "2"}})
        for code in ("MP-1", "MP-2", "MP-3"):
            self.ok("PUT", "/api/manual/volumes", body={"product": code, "annual": "1000"})
            self.ok("PUT", "/api/molds", body={"part": code.replace("P", "Q"), "amount": "50000", "years": "2"})
            self.ok("PUT", "/api/reuse", body={"part": code.replace("P", "Q"), "product": code, "quantity": "1"})
        for i in (1, 2, 3):
            self.ok("POST", "/api/partmaps", body={"old_product": "MP-1", "new_product": "MP-2",
                                                    "old_part": f"MQ-{i}", "new_part": f"MR-{i}"})

    def write_book(self) -> Path:
        result = self.ok("POST", "/api/master/book")
        self.assertFalse(result["opened"])
        path = Path(result["path"])
        self.assertEqual(path.parent, self.path.parent)
        self.assertTrue(path.is_file())
        return path

    def edit(self, path: Path, fn) -> None:
        wb = load_openpyxl().load_workbook(path)
        fn(wb)
        wb.save(path)

    def sheet_codes(self, path: Path, sheet: str) -> list:
        # read_only は閉じるまでファイルを掴んだままで、Windows では後の上書き・削除が失敗する
        wb = load_openpyxl().load_workbook(path, read_only=True)
        try:
            return [row[0] for row in wb[sheet].iter_rows(min_row=2, values_only=True)]
        finally:
            wb.close()

    def manual(self) -> dict:
        return self.ok("GET", "/api/manual")

    def test_book_has_six_sheets_with_screen_headers_and_no_trials(self) -> None:
        path = self.write_book()
        wb = load_openpyxl().load_workbook(path, read_only=True)
        try:
            self.assertEqual(wb.sheetnames, list(SHEETS))
        finally:
            wb.close()
        m = self.manual()
        self.assertTrue(m["trials"])
        products = self.sheet_codes(path, "製品")
        self.assertEqual([c for c in products if c.startswith("MP-")], ["MP-1", "MP-2", "MP-3"])
        for trial in m["trials"]:
            self.assertNotIn(trial["code"], products + self.sheet_codes(path, "部品"))
        # 書き出したまま取り込めば、何も変わらない
        check = self.ok("POST", "/api/master/check")
        self.assertEqual((check["added"], check["changed"], check["errors"]), (0, 0, []))

    def test_round_trip_each_sheet(self) -> None:
        path = self.write_book()
        before = self.manual()

        def change(wb) -> None:
            def row_of(ws, key):
                return next(r for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value == key)

            p = wb["製品"]  # MP-1 変更・MP-2 同じ・MP-3 消す・MP-9 追加
            p.cell(row_of(p, "MP-1"), 3).value = 120
            p.delete_rows(row_of(p, "MP-3"))
            p.append(["MP-9", "新製品9", 300])
            q = wb["部品"]
            q.cell(row_of(q, "MQ-1"), 2).value = "部品1改"
            q.delete_rows(row_of(q, "MQ-3"))
            q.append(["MQ-9", "部品9", "B", 7.5])
            v = wb["年間台数"]
            v.cell(row_of(v, "MP-1"), 2).value = 2400
            v.delete_rows(row_of(v, "MP-3"))
            v.append(["MP-9", 500])
            m = wb["型費"]
            m.cell(row_of(m, "MQ-1"), 3).value = 3
            m.delete_rows(row_of(m, "MQ-3"))
            m.append(["MQ-9", 90000])  # 償却年数の空欄は 2 年
            r = wb["流用先"]
            r.cell(row_of(r, "MQ-1"), 4).value = "F-600"
            r.delete_rows(row_of(r, "MQ-3"))
            r.append(["MQ-9", "MP-9", 2])
            t = wb["部品の対応表"]
            t.delete_rows(next(r for r in range(2, t.max_row + 1) if t.cell(r, 3).value == "MQ-3"))
            t.append(["MP-1", "MP-2", "MQ-9", 3, "MR-9", 4])

        self.edit(path, change)
        check = self.ok("POST", "/api/master/check")
        self.assertEqual(check["errors"], [])
        by_sheet = {s["sheet"]: (s["added"], s["changed"], s["unchanged"]) for s in check["sheets"]}
        # 変わらない行は、デモの登録があるシートでは MP-2 などより多い
        for sheet in SHEETS:
            added, changed, unchanged = by_sheet[sheet]
            self.assertEqual((added, changed), (1, 0 if sheet == "部品の対応表" else 1), sheet)
            self.assertGreaterEqual(unchanged, 1, sheet)
        self.assertEqual(self.manual(), before)  # 確認では何も保存しない

        done = self.ok("POST", "/api/master/apply")
        self.assertEqual((done["added"], done["changed"]), (check["added"], check["changed"]))
        m = self.manual()
        self.assertEqual(done["manual"], m)
        items = {i["code"]: i for i in m["items"]}
        self.assertEqual(items["MP-1"]["costs"]["MATERIAL"], "120")
        self.assertEqual(items["MP-1"]["role"], "product")
        self.assertEqual((items["MP-9"]["role"], items["MP-9"]["costs"]["MATERIAL"]), ("product", "300"))
        self.assertEqual(items["MQ-1"]["name"], "部品1改")
        self.assertEqual((items["MQ-9"]["kind"], items["MQ-9"]["costs"]["MATERIAL"]), ("B", "7.5"))
        volumes = {v["product"]: v["annual"] for v in m["volumes"]}
        self.assertEqual((volumes["MP-1"], volumes["MP-9"]), ("2400", "500"))
        molds = {x["part"]: x for x in m["molds"]}
        self.assertEqual((molds["MQ-1"]["years"], molds["MQ-9"]["years"]), ("3", "2"))
        reuse = {(t["part"], t["product"]): t for t in m["reuse"]}
        self.assertEqual(reuse[("MQ-1", "MP-1")]["replaced_part"], "F-600")
        self.assertEqual(reuse[("MQ-9", "MP-9")]["quantity"], "2")
        maps = {(r["old_part"], r["new_part"], r["old_balloon"], r["new_balloon"]) for r in m["part_maps"]}
        self.assertIn(("MQ-9", "MR-9", "3", "4"), maps)
        # 消した行は登録に残る（6シートとも）
        self.assertIn("MP-3", items)
        self.assertIn("MQ-3", items)
        self.assertIn("MP-3", volumes)
        self.assertIn("MQ-3", molds)
        self.assertIn(("MQ-3", "MP-3"), reuse)
        self.assertIn(("MQ-3", "MR-3", None, None), maps)
        # 入れた後は同じブックの差が無い
        again = self.ok("POST", "/api/master/check")
        self.assertEqual((again["added"], again["changed"]), (0, 0))

    def test_row_errors_block_everything(self) -> None:
        path = self.write_book()
        before = self.manual()

        def change(wb) -> None:
            wb["製品"].append(["MP-8", "新製品8", 10])  # 正しい行も入らない
            wb["部品"].append(["MQ-8", "部品8", "B", "abc"])
            wb["部品"].append(["MP-1", "製品と重なる", "B"])
            wb["年間台数"].append(["MP-8", -1])
            wb["型費"].append(["MQ-8", None])
            wb["流用先"].append(["MQ-8", "MP-8", 0])
            wb["部品の対応表"].append(["MP-1", "MP-1", "MQ-1", "MR-1"])
            wb["製品"].append(["MP-8", "二度目", 10])

        self.edit(path, change)
        check = self.ok("POST", "/api/master/check")
        errors = {(e["sheet"], e["row"]) for e in check["errors"]}
        n_products = len(self.sheet_codes(path, "製品"))
        self.assertIn(("製品", n_products + 1), errors)  # 同じ品番が2回
        self.assertEqual({s for s, _ in errors}, set(SHEETS))
        self.assertTrue(all(e["message"] for e in check["errors"]))

        response = self.call("POST", "/api/master/apply")
        self.assertEqual(response.status, 400)
        self.assertEqual(len(response.json()["errors"]), len(check["errors"]))
        self.assertEqual(self.manual(), before)

    def test_uploaded_book_and_missing_parts(self) -> None:
        self.assert_status(self.call("POST", "/api/master/check"), 404)
        path = self.write_book()

        def change(wb) -> None:
            wb["年間台数"].append(["MP-7", 70])
            del wb["型費"]

        self.edit(path, change)
        content = base64.b64encode(path.read_bytes()).decode("ascii")
        path.unlink()
        body = {"filename": "別の場所.xlsx", "content": content}
        check = self.ok("POST", "/api/master/check", body=body)
        self.assertEqual(check["errors"], [{"sheet": "型費", "row": None, "message": "シートが無い"}])
        self.assert_status(self.call("POST", "/api/master/apply", body=body), 400)
        bad = self.call("POST", "/api/master/check", body={"filename": "x.xlsx", "content": "aGVsbG8="})
        self.assert_status(bad, 400)

    def test_open_and_locked_book(self) -> None:
        opened: list[Path] = []
        self.api.open_file = opened.append
        result = self.ok("POST", "/api/master/book")
        self.assertTrue(result["opened"])
        self.assertEqual(opened, [Path(result["path"])])
        with mock.patch("bomcost.masterbook.os.replace", side_effect=PermissionError):
            response = self.call("POST", "/api/master/book")
        self.assert_status(response, 409)
        self.assertIn("開いたまま", response.json()["error"])
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.suffix == ".tmp"], [])

    def test_open_only_does_not_rewrite(self) -> None:
        self.assert_status(self.call("POST", "/api/master/open"), 404)
        path = self.write_book()
        self.edit(path, lambda wb: wb["製品"].append(["MP-9", "新製品9", 1]))
        edited = path.read_bytes()
        opened: list[Path] = []
        self.api.open_file = opened.append
        result = self.ok("POST", "/api/master/open")
        self.assertTrue(result["opened"])
        self.assertEqual(opened, [path])
        self.assertEqual(path.read_bytes(), edited)
        self.assertEqual(self.ok("POST", "/api/master/check")["added"], 1)

    def assert_status(self, response, status: int) -> None:
        self.assertEqual(response.status, status, response.body.decode("utf-8"))


class MemoryDb(unittest.TestCase):
    def test_no_folder_for_memory_db(self) -> None:
        with Store(":memory:") as store:
            response = Api(store).handle("POST", "/api/master/book")
        self.assertEqual(response.status, 400)
        self.assertIn("置き場", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
