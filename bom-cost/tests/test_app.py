"""画面の API（ソケットを開かずにハンドラの関数を呼ぶ）・デモの DB・静的ファイル・待ち受けの形。データはすべて架空。"""

import contextlib
import io
import json
import socket
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import example_data as ex
from bomcost import BomLine, CostHead, CostRow, Item, Scenario, Store, Version
from bomcost.scenario import Change, ChangeKind
from bomcost.api import Api, Response
from bomcost.app import HOST, bind, main, make_handler, static_response
from bomcost.demo import DEMO_SCENARIO_NAME, build_demo
from bomcost.importer import load_openpyxl


class DemoDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "demo.sqlite"
        self.scenario_id = build_demo(self.path)
        self.store = Store(self.path)
        self.api = Api(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._dir.cleanup()

    def call(self, method: str, path: str, query: str = "", body: object = None) -> Response:
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self.api.handle(method, path, query, raw)

    def ok(self, method: str, path: str, query: str = "", body: object = None) -> dict:
        response = self.call(method, path, query, body)
        self.assertLess(response.status, 300, response.body.decode("utf-8"))
        return response.json()

    def url(self, tail: str = "") -> str:
        return f"/api/scenarios/{self.scenario_id}{tail}"


class DemoDb(DemoDbCase):
    def test_same_content_as_example_data(self) -> None:
        for version_id, expected in (("2026-10", ex.version_2026_10()), ("2026-12", ex.version_2026_12())):
            with self.subTest(version=version_id):
                v = self.store.load_version(version_id)
                self.assertEqual(set(v.items), set(expected.items))
                self.assertEqual({l.key: l.quantity for l in v.lines}, {l.key: l.quantity for l in expected.lines})
                self.assertEqual({r.code: dict(r.values) for r in v.cost_rows},
                                 {r.code: dict(r.values) for r in expected.cost_rows})
                self.assertEqual({p: x.annual for p, x in v.volumes.items()},
                                 {p: x.annual for p, x in expected.volumes.items()})
        self.assertEqual(self.store.mold_costs()["X-610"], ex.mold_x610())
        self.assertEqual(self.store.reuse_targets("X-610"), [ex.reuse_k200()])
        self.assertEqual(self.store.load_scope_policy().overrides, ex.policy().overrides)
        scenario = self.store.load_scenario(self.scenario_id)
        self.assertEqual(scenario.name, DEMO_SCENARIO_NAME)
        self.assertEqual(scenario.calculate().total, Decimal("4650"))

    def test_refuses_existing_file(self) -> None:
        with self.assertRaises(FileExistsError):
            build_demo(self.path)

    def test_demo_command(self) -> None:
        other = Path(self._dir.name) / "other.sqlite"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--demo"])  # --db が要る
            self.assertEqual(main(["--demo", "--db", str(other)]), 0)
            self.assertTrue(other.is_file())
            self.assertEqual(main(["--demo", "--db", str(other)]), 1)


class ListsAndTree(DemoDbCase):
    def test_versions_and_scenarios(self) -> None:
        versions = self.ok("GET", "/api/versions")
        self.assertEqual([v["version_id"] for v in versions], ["2026-12", "2026-10"])
        scenarios = self.ok("GET", "/api/scenarios")
        self.assertEqual([(s["name"], s["base"], s["version_id"]) for s in scenarios],
                         [("V-110 検討", "A-100", "2026-10")])
        items = {i["code"]: i["name"] for i in self.ok("GET", "/api/items", "version=2026-10")}
        self.assertEqual(items["X-610"], "試作品")

    def test_tree_root_and_children(self) -> None:
        data = self.ok("GET", self.url("/tree"))
        self.assertEqual(data["node"]["code"], "V-110")
        self.assertEqual(data["node"]["cost"], "4650")
        self.assertEqual(data["view"]["missing"], [])
        children = {c["code"]: c for c in data["children"]}
        self.assertEqual((children["A-100"]["quantity"], children["A-100"]["cost"], children["A-100"]["role"]),
                         ("1", "4900", "土台"))
        self.assertEqual((children["F-600"]["quantity"], children["F-600"]["role"]), ("1", "足す"))
        self.assertEqual((children["C-300"]["quantity"], children["C-300"]["amount"], children["C-300"]["role"]),
                         ("-1", "-500", "抜く"))
        # 親の登録値 200 は子の足し上げ 4,700 よりずっと小さい
        self.assertEqual(children["A-100"]["verdict"], "自分だけらしい")
        self.assertEqual(children["A-100"]["verdict_values"], ["200", "4700"])

        sub = self.ok("GET", self.url("/tree"), "code=A-100")
        self.assertEqual({c["code"]: (c["quantity"], c["cost"]) for c in sub["children"]},
                         {"B-200": ("2", "1600"), "C-300": ("3", "500")})

    def test_unregistered_is_not_zero(self) -> None:
        data = self.ok("GET", self.url("/tree"), "head=DIRECT")
        self.assertIsNone(data["node"]["cost"])
        self.assertTrue(all(c["cost"] is None for c in data["children"]))
        self.assertTrue(data["view"]["premises"]["has_missing"])
        self.assertIn("B-200", data["view"]["missing"])

    def test_item_detail(self) -> None:
        d = self.ok("GET", self.url("/item"), "code=A-100")
        heads = {h["id"]: h for h in d["heads"]}
        self.assertEqual([h["label"] for h in d["heads"]], ["材料費", "直接費", "間接費", "調整費", "製造原価"])
        self.assertEqual((heads["MATERIAL"]["registered"], heads["MATERIAL"]["calculated"]), ("200", "4900"))
        self.assertEqual(heads["MATERIAL"]["verdict"], "自分だけらしい")
        self.assertEqual(heads["MATERIAL"]["compared"], "4700")
        self.assertIsNone(heads["DIRECT"]["calculated"])
        self.assertEqual(heads["DIRECT"]["verdict"], "判定できない")
        self.assertEqual((d["item_source"], d["cost_source"]), ("取り込み", "取り込み"))
        x = self.ok("GET", self.url("/item"), "code=X-610")
        self.assertEqual(x["cost_source"], "手入力")

    def test_bulk_mode_shows_base_as_is(self) -> None:
        data = self.ok("GET", self.url("/tree"), "mode=bulk")
        self.assertEqual((data["node"]["code"], data["node"]["cost"]), ("A-100", "4900"))
        self.assertEqual(data["view"]["mode_label"], "一括")


class Changes(DemoDbCase):
    def test_replace_records_history_and_saves(self) -> None:
        before = self.ok("GET", self.url())
        self.assertEqual(before["total"], "4650")
        self.assertEqual(len(before["history"]), 2)
        after = self.ok("POST", self.url("/changes"),
                        body={"kind": "REPLACE", "part": "F-600", "replacement": "X-610", "quantity": "1"})
        self.assertEqual(after["total"], "4600")
        self.assertEqual(len(after["history"]), 3)
        last = after["history"][-1]
        self.assertEqual((last["cost_before"], last["cost_after"], last["difference"]), ("4650", "4600", "-50"))
        self.assertEqual((last["version_id"], last["head_label"], last["volume"]), ("2026-10", "材料費", "10000"))
        self.assertEqual(after["last"]["difference"], "-50")

        with Store(self.path) as other:  # SQLite に残っている
            reloaded = other.load_scenario(self.scenario_id)
            self.assertEqual(len(reloaded.history), 3)
            self.assertEqual(reloaded.calculate().total, Decimal("4600"))

        undone = self.ok("POST", self.url("/undo"))
        self.assertEqual(undone["total"], "4650")
        self.assertEqual(len(undone["history"]), 2)
        with Store(self.path) as other:
            self.assertEqual(len(other.load_scenario(self.scenario_id).history), 2)

    def test_add_remove_and_quantity(self) -> None:
        added = self.ok("POST", self.url("/changes"), body={"kind": "ADD", "part": "B-200", "quantity": 1})
        self.assertEqual((added["total"], added["history"][-1]["difference"]), ("6250", "1600"))
        removed = self.ok("POST", self.url("/changes"), body={"kind": "REMOVE", "part": "B-200", "quantity": "1"})
        self.assertEqual(removed["total"], "4650")
        qty = self.ok("POST", self.url("/changes"), body={"kind": "SET_QUANTITY", "part": "F-600", "quantity": "2"})
        self.assertEqual(qty["total"], "4900")

    def test_undo_without_history(self) -> None:
        self.ok("POST", self.url("/undo"))
        self.ok("POST", self.url("/undo"))
        response = self.call("POST", self.url("/undo"))
        self.assertEqual(response.status, 400)
        self.assertIn("取り消す変更が無い", response.json()["error"])

    def test_new_scenario(self) -> None:
        created = self.call("POST", "/api/scenarios", body={
            "name": "例の検討", "base": "A-100", "product_code": "Z-900", "version_id": "2026-10", "head": "MATERIAL",
        })
        self.assertEqual(created.status, 201)
        data = created.json()
        self.assertEqual((data["total"], data["history"]), ("4900", []))
        self.assertEqual(len(self.ok("GET", "/api/scenarios")), 2)


class VersionsSwitch(DemoDbCase):
    def test_switch_version_and_compare(self) -> None:
        cmp = self.ok("GET", self.url("/compare"), "version=2026-12")
        self.assertEqual((cmp["version_a"], cmp["version_b"]), ("2026-10", "2026-12"))
        self.assertEqual((cmp["total_a"], cmp["total_b"], cmp["difference"]), ("4650", "4690", "40"))
        by_part = {l["part"]: l for l in cmp["lines"]}
        self.assertEqual(by_part["A-100"]["difference"], "60")
        self.assertEqual(by_part["F-600"]["difference"], "0")
        self.assertEqual((by_part["C-300"]["amount_a"], by_part["C-300"]["amount_b"]), ("-500", "-520"))

        switched = self.ok("PUT", self.url(), body={"version_id": "2026-12"})
        self.assertEqual(switched["total"], "4690")
        self.assertEqual(switched["scenario"]["version_id"], "2026-12")
        # 履歴は変更した時の前提のまま
        self.assertEqual({r["version_id"] for r in switched["history"]}, {"2026-10"})
        with Store(self.path) as other:
            self.assertEqual(other.load_scenario(self.scenario_id).version.version_id, "2026-12")
        self.assertEqual(self.ok("GET", self.url("/tree"))["node"]["cost"], "4690")

    def test_switch_head(self) -> None:
        switched = self.ok("PUT", self.url(), body={"head": "DIRECT"})
        self.assertEqual(switched["scenario"]["head_label"], "直接費")
        self.assertIsNone(switched["total"])
        self.assertEqual(self.call("PUT", self.url(), body={"head": "AMORTIZATION"}).status, 400)


class TableMode(DemoDbCase):
    def test_rows(self) -> None:
        data = self.ok("GET", self.url("/table"))
        keys = [c["key"] for c in data["columns"]]
        self.assertEqual(keys, ["level", "parent", "balloon", "code", "name", "quantity", "usage",
                                "MATERIAL", "DIRECT", "INDIRECT", "ADJUSTMENT", "MANUFACTURING", "verdict"])
        rows = data["rows"]
        self.assertEqual([(r["level"], r["parent"], r["code"]) for r in rows], [
            (0, "", "V-110"), (1, "V-110", "A-100"), (2, "A-100", "B-200"), (2, "A-100", "C-300"),
            (1, "V-110", "F-600"), (1, "V-110", "C-300"),
        ])
        b200 = rows[2]
        self.assertEqual((b200["quantity"], b200["usage"], b200["MATERIAL"], b200["DIRECT"]), ("2", "2", "1600", None))
        self.assertEqual(rows[5]["usage"], "-1")
        self.assertIn("材料費: 自分だけらしい", rows[1]["verdict"])
        self.assertEqual(rows[0]["MATERIAL"], "4650")

    def test_balloon_toggle(self) -> None:
        merged = self.ok("GET", self.url("/table"), "by_balloon=0")["rows"]
        split = self.ok("GET", self.url("/table"), "by_balloon=1")["rows"]
        self.assertEqual([r["balloon"] for r in merged if r["parent"] == "A-100"], ["", ""])
        self.assertEqual([r["balloon"] for r in split if r["parent"] == "A-100"], ["1", "2"])

    def test_balloon_toggle_changes_rows(self) -> None:
        store = Store(":memory:")
        v = Version("T")
        v.add_items([Item("P-1", "土台"), Item("X-1", "部品")])
        v.add_bom_lines([BomLine("P-1", "1", "X-1", 1), BomLine("P-1", "2", "X-1", 2)])
        v.add_cost(CostRow("X-1", {CostHead.MATERIAL: 100}))
        store.save_version(v)
        sid = store.save_scenario(Scenario("例", "P-1", "Q-1", store.load_version("T")))
        api = Api(store)
        merged = api.handle("GET", f"/api/scenarios/{sid}/table", "mode=bulk&by_balloon=0").json()["rows"]
        split = api.handle("GET", f"/api/scenarios/{sid}/table", "mode=bulk&by_balloon=1").json()["rows"]
        self.assertEqual([(r["code"], r["quantity"]) for r in merged[1:]], [("X-1", "3")])
        self.assertEqual([(r["balloon"], r["quantity"]) for r in split[1:]], [("1", "1"), ("2", "2")])
        self.assertEqual(merged[0]["MATERIAL"], split[0]["MATERIAL"])
        store.close()

    def test_export_xlsx(self) -> None:
        response = self.call("POST", self.url("/export"), body={
            "columns": ["code", "name", "quantity", "MATERIAL", "DIRECT"], "row_ids": [2, 0],
        })
        self.assertEqual(response.status, 200)
        self.assertIn("spreadsheetml", response.content_type)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        openpyxl = load_openpyxl()
        wb = openpyxl.load_workbook(io.BytesIO(response.body))
        ws = wb["構成"]
        values = [list(r) for r in ws.iter_rows(values_only=True)]
        self.assertEqual(values[0], ["品番", "名称", "数量", "材料費", "直接費"])
        self.assertEqual(values[1][:4], ["B-200", "部品B", 2, 1600])
        self.assertIsInstance(values[1][3], (int, float))
        self.assertEqual(values[1][4], "未登録")
        self.assertEqual(values[2][:4], ["V-110", "新しい製品", 1, 4650])
        self.assertEqual(len(values), 3)
        premises = dict(wb["前提"].iter_rows(values_only=True))
        self.assertEqual(premises["計算に使った版"], "2026-10")
        self.assertEqual(premises["値の扱い"], "参考値")

    def test_export_rejects_unknown_column(self) -> None:
        response = self.call("POST", self.url("/export"), body={"columns": ["nope"]})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.call("POST", self.url("/export"), body={"row_ids": [99]}).status, 400)


class BadInput(DemoDbCase):
    def assert_error(self, response: Response, status: int, words: str) -> None:
        self.assertEqual(response.status, status, response.body.decode("utf-8"))
        message = response.json()["error"]
        self.assertIn(words, message)

    def test_errors_are_4xx_in_japanese(self) -> None:
        self.assert_error(self.call("POST", self.url("/changes"), body={"kind": "ADD", "part": "NOPE-1"}),
                          400, "品番 NOPE-1 は版 2026-10 に無い")
        self.assert_error(self.call("POST", self.url("/changes"), body={"kind": "ADD", "part": "B-200", "quantity": "abc"}),
                          400, "数量を数として読めない")
        self.assert_error(self.call("POST", self.url("/changes"), body={"kind": "ADD", "part": "B-200", "quantity": [1]}),
                          400, "数量を数として読めない")
        self.assert_error(self.call("POST", self.url("/changes"), body={"kind": "REMOVE", "part": "B-200", "quantity": "-1"}),
                          400, "正の数")
        self.assert_error(self.call("POST", self.url("/changes"), body={"kind": "REPLACE", "part": "F-600", "replacement": "ZZ"}),
                          400, "代わりの品番 ZZ")
        self.assert_error(self.call("POST", self.url("/changes"), body={"kind": "MOVE", "part": "B-200"}),
                          400, "変更の種類が分からない")
        self.assert_error(self.call("POST", self.url("/changes"), body={"kind": "ADD", "part": "V-110"}),
                          400, "試算の製品そのもの")
        self.assert_error(self.call("GET", self.url("/tree"), "head=XYZ"), 400, "費目が分からない")
        self.assert_error(self.call("GET", self.url("/tree"), "mode=other"), 400, "モードが分からない")
        self.assert_error(self.call("GET", self.url("/compare"), "version=1999-01"), 404, "版 1999-01 は無い")
        self.assert_error(self.call("GET", "/api/scenarios/999"), 404, "試算 999 は無い")
        self.assert_error(self.call("PUT", self.url(), body={"version_id": "1999-01"}), 404, "版 1999-01 は無い")
        self.assert_error(self.api.handle("POST", self.url("/changes"), "", b"{not json"), 400, "JSON として読めない")
        self.assert_error(self.call("GET", "/api/nothing"), 404, "API が無い")
        self.assert_error(self.call("DELETE", self.url()), 405, "使えない")
        self.assert_error(self.call("POST", "/api/scenarios", body={"name": "x", "base": "A-100", "product_code": "A-100",
                                                                   "version_id": "2026-10"}), 400, "別の品番")
        # 循環する構成（土台の子に試算の製品を置く）は弾く
        self.assert_error(self.call("POST", "/api/scenarios", body={"name": "x", "base": "V-110", "product_code": "A-100",
                                                                   "version_id": "2026-10"}), 400, "循環")
        # 誤りの後も同じ API で続けて使える。誤りは履歴に残らない
        data = self.ok("GET", self.url())
        self.assertEqual((data["total"], len(data["history"])), ("4650", 2))


class RegisteredValueOfTrialProduct(unittest.TestCase):
    def test_registered_value_of_trial_product_is_not_used(self) -> None:
        version = ex.version_2026_10()
        version.add_cost(CostRow("V-110", {CostHead.MATERIAL: 9999}))
        scenario = Scenario("t", "A-100", "V-110", version, CostHead.MATERIAL, ex.policy())
        scenario.apply(Change(ChangeKind.ADD, "F-600"))
        scenario.apply(Change(ChangeKind.REMOVE, "C-300"))
        self.assertEqual(scenario.calculate().total, Decimal("4650"))
        scenario.apply(Change(ChangeKind.REPLACE, "F-600", replacement="X-610"))
        self.assertEqual(scenario.calculate().total, Decimal("4600"))


class StaticAndServer(unittest.TestCase):
    def test_favicon_is_empty_204(self) -> None:
        response = static_response("/favicon.ico")
        self.assertEqual((response.status, response.body), (204, b""))

    def test_static_files(self) -> None:
        index = static_response("/")
        self.assertEqual(index.status, 200)
        self.assertIn("text/html", index.content_type)
        self.assertIn("BOM 原価モック", index.body.decode("utf-8"))
        for name in ("/app.js", "/screens.js", "/style.css"):
            self.assertEqual(static_response(name).status, 200)
        for bad in ("/../app.py", "/nothing.js", "/..%2Fapp.py", "/sub/x.js"):
            self.assertEqual(static_response(bad).status, 404, bad)

    def test_no_external_resources(self) -> None:
        web = Path(__file__).resolve().parent.parent / "bomcost" / "web"
        for path in web.iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)(src|href)=[\"']?(https?:)?//", path.name)
            self.assertNotIn("@import", text, path.name)

    def test_binds_localhost_and_falls_back_when_port_is_used(self) -> None:
        handler = make_handler(lambda *args: Response(200, b"{}"))
        with socket.socket() as busy:
            busy.bind((HOST, 0))
            busy.listen()
            port = busy.getsockname()[1]
            server = bind(port, handler)
            try:
                self.assertEqual(server.server_address[0], "127.0.0.1")
                self.assertNotEqual(server.server_address[1], port)
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
