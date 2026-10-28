"""画面の2段目の API（取り込み・手入力・部品から見る・モデルチェンジ・版の削除）。ソケットは開かない。データはすべて架空。"""

import base64
import io
import json
import unittest
from pathlib import Path

from bomcost import Store
from bomcost.api import Api, Response
from bomcost.importer import load_openpyxl
from test_app import DemoDbCase

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

ITEM_CSV = "品番,品名,区分,登録者,備考\nQ-1,製品Q,P,例,\nQ-2,部品2,B,例,メモ\n"
BOM_CSV = "親品番,子品番,員数,単位\nQ-1,Q-2,2,個\nQ-1,Q-3,abc,個\n"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def csv_file(name: str, text: str, **extra: object) -> dict:
    return {"filename": name, "content": b64(text.encode("utf-8")), **extra}


def xlsx_file(name: str, rows: list[list[object]], **extra: object) -> dict:
    wb = load_openpyxl().Workbook()
    ws = wb.active
    ws.title = "一覧"
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return {"filename": name, "content": b64(out.getvalue()), **extra}


class EmptyDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.api = Api(self.store)

    def tearDown(self) -> None:
        self.store.close()

    def call(self, method: str, path: str, query: str = "", body: object = None) -> Response:
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self.api.handle(method, path, query, raw)

    def ok(self, method: str, path: str, query: str = "", body: object = None) -> object:
        response = self.call(method, path, query, body)
        self.assertLess(response.status, 300, response.body.decode("utf-8"))
        return response.json()

    def assert_error(self, response: Response, low: int, words: str) -> None:
        self.assertTrue(400 <= response.status < 500, response.body.decode("utf-8"))
        if low:
            self.assertEqual(response.status, low)
        self.assertIn(words, response.json()["error"])


class Import(EmptyDbCase):
    def test_preview_csv_and_xlsx(self) -> None:
        p = self.ok("POST", "/api/import/preview", body=csv_file("品目.csv", ITEM_CSV))
        self.assertEqual(p["headers"], ["品番", "品名", "区分", "登録者", "備考"])
        self.assertEqual(p["row_count"], 2)
        self.assertEqual(p["sample"][0][:2], ["Q-1", "製品Q"])
        self.assertEqual([t["id"] for t in p["tables"]], ["item", "bom_line", "cost", "volume"])
        self.assertTrue(all(t["saved"] is None for t in p["tables"]))
        self.assertIsNone(p["suggested"])

        x = self.ok("POST", "/api/import/preview",
                    body=xlsx_file("台数.xlsx", [["製品品番", "年間生産台数", "集計年度"], ["Q-1", 1200, 2026]]))
        self.assertEqual((x["headers"], x["sheets"], x["sheet"]), (["製品品番", "年間生産台数", "集計年度"], ["一覧"], "一覧"))

    def test_import_saves_map_and_reuses_it(self) -> None:
        body = {
            "version_id": "2026-11",
            "note": "試し",
            "files": [
                csv_file("品目.csv", ITEM_CSV, table="item", columns={"code": "品番", "name": "品名", "kind": "区分"}),
                csv_file("構成.csv", BOM_CSV, table="bom_line",
                         columns={"parent": "親品番", "child": "子品番", "quantity": "員数"}),
                xlsx_file("台数.xlsx", [["製品品番", "年間生産台数", "集計年度"], ["Q-1", 1200, 2026]],
                          table="volume", columns={"product": "製品品番", "annual": "年間生産台数"}),
            ],
        }
        response = self.call("POST", "/api/import", body=body)
        self.assertEqual(response.status, 201, response.body.decode("utf-8"))
        result = response.json()
        self.assertEqual(result["counts"], {"items": 2, "lines": 1, "costs": 0, "volumes": 1})
        # 数量が読めない行は警告に出て、取り込みは続く
        self.assertEqual(result["warning_count"], 1)
        self.assertEqual(result["warnings"][0]["row"], 3)
        self.assertIn("読み飛ばした", result["warnings"][0]["message"])

        versions = self.ok("GET", "/api/versions")
        self.assertEqual([(v["version_id"], v["warning_count"]) for v in versions], [("2026-11", 1)])
        info = self.ok("GET", "/api/versions/info", "version=2026-11")
        self.assertEqual(info["note"], "試し")
        self.assertIn("読み飛ばした", info["warnings"][0]["message"])

        # 同じ見出しの別のファイル: 保存した対応づけが候補に出て、選び直さずに取り込める
        other = "品番,品名,区分,登録者,備考\nR-1,製品R,P,例,\n"
        p = self.ok("POST", "/api/import/preview", body=csv_file("別の品目.csv", other))
        self.assertEqual(p["suggested"], "item")
        self.assertEqual(next(t for t in p["tables"] if t["id"] == "item")["saved"],
                         {"code": "品番", "name": "品名", "kind": "区分"})
        response = self.call("POST", "/api/import", body={"version_id": "2026-12",
                                                          "files": [csv_file("別の品目.csv", other)]})
        self.assertEqual(response.status, 201, response.body.decode("utf-8"))
        items = {i["code"] for i in self.ok("GET", "/api/items", "version=2026-12")}
        self.assertEqual(items, {"R-1"})
        xlsx_again = xlsx_file("台数2.xlsx", [["製品品番", "年間生産台数", "集計年度"], ["R-1", 10, 2027]])
        self.assertEqual(self.ok("POST", "/api/import/preview", body=xlsx_again)["suggested"], "volume")

    def test_import_errors(self) -> None:
        missing = {"version_id": "v1", "files": [csv_file("品目.csv", ITEM_CSV, table="item", columns={"code": "品番"})]}
        self.assert_error(self.call("POST", "/api/import", body=missing), 400, "必須の列が対応づいていない: 名称")
        no_map = {"version_id": "v1", "files": [csv_file("品目.csv", ITEM_CSV, table="item")]}
        self.assert_error(self.call("POST", "/api/import", body=no_map), 400, "対応づけが無い")
        bad_header = {"version_id": "v1", "files": [csv_file("品目.csv", ITEM_CSV, table="item",
                                                             columns={"code": "品番", "name": "名前"})]}
        self.assert_error(self.call("POST", "/api/import", body=bad_header), 400, "列「名前」")
        ok = {"version_id": "v1", "files": [csv_file("品目.csv", ITEM_CSV, table="item",
                                                      columns={"code": "品番", "name": "品名"})]}
        self.assertEqual(self.call("POST", "/api/import", body=ok).status, 201)
        self.assert_error(self.call("POST", "/api/import", body=ok), 409, "版 v1 はもうある")
        self.assert_error(self.call("POST", "/api/import", body={**ok, "version_id": "manual"}), 400, "手入力の版")
        self.assert_error(self.call("POST", "/api/import/preview", body={"filename": "a.txt", "content": b64(b"x")}),
                          400, "読めないファイルの種類")
        self.assert_error(self.call("POST", "/api/import/preview", body={"filename": "a.xlsx", "content": b64(b"x")}),
                          400, "Excel のファイルとして読めない")
        self.assert_error(self.call("POST", "/api/import/preview", body={"filename": "a.csv", "content": "@@"}),
                          400, "base64")

    def test_conflict_is_a_warning(self) -> None:
        text = "親品番,風船番号,子品番,員数\nQ-1,1,Q-2,2\nQ-1,1,Q-2,3\n"
        body = {"version_id": "c1", "files": [csv_file("構成.csv", text, table="bom_line",
                                                       columns={"parent": "親品番", "balloon": "風船番号",
                                                                "child": "子品番", "quantity": "員数"})]}
        result = self.ok("POST", "/api/import", body=body)
        self.assertEqual(result["warning_count"], 1)
        self.assertIn("数量が食い違う", result["warnings"][0]["message"])
        self.assertEqual(self.ok("GET", "/api/versions")[0]["warning_count"], 1)


class SampleProcedure(EmptyDbCase):
    """見本の CSV で試す手順: 空の DB に samples/ の CSV を取り込み、手入力で X-610 を入れ、部品から見る画面で 60 円。"""

    def test_from_empty_db_to_60_yen(self) -> None:
        def sample(name: str, table: str, columns: dict) -> dict:
            return {"filename": name, "content": b64((SAMPLES / name).read_bytes()), "table": table, "columns": columns}

        result = self.ok("POST", "/api/import", body={"version_id": "2026-10", "files": [
            sample("部品マスタ.csv", "item", {"code": "品番", "name": "品名", "kind": "区分", "lifecycle": "状態",
                                              "has_children": "子部品有無"}),
            sample("構成マスタ.csv", "bom_line", {"parent": "親品番", "level": "レベル", "balloon": "風船番号",
                                                "child": "子品番", "quantity": "員数"}),
            sample("原価.csv", "cost", {"mbom_code": "MBOM品番", "material": "材料費", "direct": "直接費"}),
            sample("年間台数.csv", "volume", {"product": "製品品番", "annual": "年間生産台数"}),
        ]})
        self.assertEqual(result["counts"], {"items": 6, "lines": 5, "costs": 5, "volumes": 1})
        self.assertEqual(result["warning_count"], 5)  # 架空の品番は11桁でないので MBOM の変換をしない

        self.ok("PUT", "/api/manual/items", body={"code": "X-610", "name": "試作品", "costs": {"MATERIAL": "200"}})
        self.ok("PUT", "/api/manual/volumes", body={"product": "V-110", "annual": "10000"})
        self.ok("PUT", "/api/molds", body={"part": "X-610", "amount": "2400000", "years": "2"})
        self.ok("PUT", "/api/reuse", body={"part": "X-610", "product": "K-200", "quantity": "2", "replaced_part": "F-600"})
        created = self.ok("POST", "/api/scenarios", body={"name": "V-110 検討", "base": "A-100", "product_code": "V-110",
                                                          "version_id": "2026-10"})
        sid = created["scenario"]["scenario_id"]
        for change in ({"kind": "ADD", "part": "F-600"}, {"kind": "REMOVE", "part": "C-300"},
                       {"kind": "REPLACE", "part": "F-600", "replacement": "X-610"}):
            self.ok("POST", f"/api/scenarios/{sid}/changes", body={**change, "quantity": "1"})
        view = self.ok("GET", "/api/parts/view", f"part=X-610&scenario={sid}")
        self.assertEqual((view["annual_count"], view["per_part"]), ("20000", "60"))


class PartView(DemoDbCase):
    def setUp(self) -> None:
        super().setUp()
        # 図5 の手順4（F-600 を試作品 X-610 に替える）を済ませてから部品から見る
        self.ok("POST", self.url("/changes"), body={"kind": "REPLACE", "part": "F-600", "replacement": "X-610"})

    def view(self, allocation: str = "", extra: str = "") -> dict:
        query = f"part=X-610&scenario={self.scenario_id}" + (f"&allocation={allocation}" if allocation else "") + extra
        return self.ok("GET", "/api/parts/view", query)

    def test_by_usage(self) -> None:
        v = self.view()
        rows = {r["product"]: r for r in v["rows"]}
        self.assertEqual(set(rows), {"V-110", "K-200"})
        v110, k200 = rows["V-110"], rows["K-200"]
        self.assertEqual((v110["volume_kind"], v110["annual"], v110["quantity_per_unit"]), ("新製品の計画", "10000", "1"))
        self.assertEqual((k200["volume_kind"], k200["annual"], k200["quantity_per_unit"]), ("量産の実績", "5000", "2"))
        self.assertEqual((v["annual_count"], v["per_part"]), ("20000", "60"))
        # 型費は償却費なので費目の後には足さず、償却費込みの差だけに入る
        self.assertEqual((v110["current"], v110["after"], v110["mold_per_unit"]), (None, "4600", "60"))
        self.assertEqual((k200["current"], k200["part_difference"], k200["after"], k200["difference"],
                          k200["difference_with_amortization"]), ("3000", "-100", "2900", "-100", "20"))
        self.assertEqual(v["mold"]["amount"], "2400000")
        p = v["premises"]
        self.assertEqual((p["version_id"], p["head_label"], p["allocation_label"]), ("2026-10", "材料費", "使う数で分ける"))
        self.assertEqual({x["product"]: x["kind"] for x in p["volumes"]}, {"V-110": "新製品の計画", "K-200": "量産の実績"})
        # 費目5つを並べる。材料費以外は未登録
        self.assertEqual(list(k200["heads"]), ["MATERIAL", "DIRECT", "INDIRECT", "ADJUSTMENT", "MANUFACTURING"])
        self.assertEqual(k200["heads"]["MATERIAL"]["after"], "2900")
        self.assertIsNone(k200["heads"]["DIRECT"]["current"])
        self.assertEqual(v["part_costs"]["MATERIAL"], "200")

    def test_other_allocations_match_design_table(self) -> None:
        for allocation, expected in (("NEW_PRODUCTS_ONLY", {"V-110": "120", "K-200": "0"}),
                                     ("NONE", {"V-110": "0", "K-200": "0"})):
            with self.subTest(allocation=allocation):
                v = self.view(allocation)
                self.assertEqual({r["product"]: r["mold_per_unit"] for r in v["rows"]}, expected)
                self.assertEqual(v["premises"]["allocation"], allocation)
        none = {r["product"]: r for r in self.view("NONE")["rows"]}
        self.assertEqual((none["V-110"]["after"], none["K-200"]["after"]), ("4600", "2900"))

    def test_head_switch_and_errors(self) -> None:
        v = self.view(extra="&head=DIRECT")
        self.assertEqual(v["premises"]["head_label"], "直接費")
        self.assertIsNone({r["product"]: r for r in v["rows"]}["K-200"]["current"])
        self.assertEqual(self.call("GET", "/api/parts/view", "part=X-610&scenario=1&allocation=ALL").status, 400)
        self.assertEqual(self.call("GET", "/api/parts/view", "part=NOPE&version=2026-10").status, 400)
        self.assertEqual(self.call("GET", "/api/parts/view", "part=X-610").status, 400)
        # 試算を選ばなければ、V-110 の版の構成には X-610 が無いので流用先だけ
        alone = self.ok("GET", "/api/parts/view", "part=X-610&version=2026-10")
        self.assertEqual([r["product"] for r in alone["rows"]], ["K-200"])


class Manual(DemoDbCase):
    def test_trial_item_works_in_every_version(self) -> None:
        data = self.ok("PUT", "/api/manual/items", body={"code": "Y-700", "name": "試作品Y",
                                                         "costs": {"MATERIAL": "1,300", "DIRECT": ""}})
        y = next(i for i in data["items"] if i["code"] == "Y-700")
        self.assertEqual((y["costs"]["MATERIAL"], y["costs"]["DIRECT"]), ("1300", None))
        for version in ("2026-10", "2026-12"):
            self.assertIn("Y-700", {i["code"] for i in self.ok("GET", "/api/items", f"version={version}")})
        after = self.ok("POST", self.url("/changes"), body={"kind": "ADD", "part": "Y-700", "quantity": "1"})
        self.assertEqual(after["total"], "5950")
        switched = self.ok("PUT", self.url(), body={"version_id": "2026-12"})
        self.assertEqual(switched["total"], "5990")
        # 直す・消す（試作品 X-610 などほかの手入力は残る）
        self.ok("PUT", "/api/manual/items", body={"code": "Y-700", "name": "試作品Y", "costs": {"MATERIAL": "1000"}})
        self.assertEqual(self.ok("GET", self.url())["total"], "5690")
        data = self.ok("DELETE", "/api/manual/items", "code=Y-700")
        self.assertEqual({i["code"] for i in data["items"]}, {"X-610"})
        self.assertEqual([v["product"] for v in data["volumes"]], ["V-110"])

    def test_mold_volume_reuse_part_map_crud(self) -> None:
        data = self.ok("PUT", "/api/molds", body={"part": "X-610", "amount": "3000000", "years": "3"})
        self.assertEqual(data["molds"], [{"part": "X-610", "amount": "3000000", "years": "3", "annual_amount": "1000000"}])
        data = self.ok("PUT", "/api/manual/volumes", body={"product": "V-110", "annual": "8000"})
        self.assertEqual(data["volumes"], [{"product": "V-110", "annual": "8000", "kind": "新製品の計画"}])
        data = self.ok("PUT", "/api/reuse", body={"part": "X-610", "product": "K-200", "quantity": "3", "replaced_part": "F-600"})
        self.assertEqual(data["reuse"], [{"part": "X-610", "product": "K-200", "quantity": "3", "replaced_part": "F-600"}])
        data = self.ok("PUT", "/api/reuse", body={"part": "X-610", "product": "K-300", "quantity": "1",
                                                  "original": {"part": "X-610", "product": "K-200"}})
        self.assertEqual([r["product"] for r in data["reuse"]], ["K-300"])
        data = self.ok("POST", "/api/partmaps", body={"old_product": "A-100", "new_product": "V-110",
                                                      "old_part": "C-300", "new_part": "X-610"})
        self.assertEqual(len(data), 1)
        data = self.ok("PUT", "/api/partmaps", body={"original": data[0], "old_product": "A-100", "new_product": "V-110",
                                                     "old_part": "C-300", "new_part": "F-600", "new_balloon": "2"})
        self.assertEqual((data[0]["new_part"], data[0]["new_balloon"]), ("F-600", "2"))
        with Store(self.path) as other:  # 保存されている
            self.assertEqual(other.part_map("A-100", "V-110"), {"C-300": "F-600"})
            self.assertEqual(other.mold_costs()["X-610"].years, 3)
        self.assertEqual(self.ok("DELETE", "/api/partmaps",
                                 "old_product=A-100&new_product=V-110&old_part=C-300&new_part=F-600&new_balloon=2"), [])
        self.ok("DELETE", "/api/reuse", "part=X-610&product=K-300")
        self.ok("DELETE", "/api/molds", "part=X-610")
        data = self.ok("DELETE", "/api/manual/volumes", "product=V-110")
        self.assertEqual((data["molds"], data["reuse"], data["volumes"], data["part_maps"]), ([], [], [], []))

    def test_errors_in_japanese(self) -> None:
        bad = [
            (("PUT", "/api/manual/items", "", {"code": "Y", "name": "y", "costs": {"MATERIAL": "abc"}}), "材料費を数として読めない"),
            (("PUT", "/api/manual/items", "", {"code": "Y", "name": "y", "costs": {"XYZ": "1"}}), "費目が分からない"),
            (("PUT", "/api/manual/items", "", {"code": "Y", "costs": {}}), "名称が空"),
            (("PUT", "/api/manual/volumes", "", {"product": "V-110", "annual": "たくさん"}), "年間台数を数として読めない"),
            (("PUT", "/api/molds", "", {"part": "X-610", "amount": [1]}), "型費を数として読めない"),
            (("PUT", "/api/molds", "", {"part": "X-610", "amount": "1", "years": "0"}), "償却年数は正の数"),
            (("PUT", "/api/reuse", "", {"part": "X-610", "product": "K-200", "quantity": "x"}), "使用数を数として読めない"),
            (("DELETE", "/api/manual/items", "code=NOPE", None), "手入力の品目 NOPE は無い"),
            (("DELETE", "/api/partmaps", "old_product=A&new_product=B&old_part=C&new_part=D", None), "対応表の行が無い"),
            (("POST", "/api/partmaps", "", {"old_product": "A", "new_product": "B", "old_part": "C", "new_part": "C"}), "自動で対応"),
        ]
        for (method, path, query, body), words in bad:
            with self.subTest(words=words):
                response = self.call(method, path, query, body)
                self.assertTrue(400 <= response.status < 500, response.body.decode("utf-8"))
                self.assertIn(words, response.json()["error"])


class ModelChange(DemoDbCase):
    def test_versions_and_scenario(self) -> None:
        data = self.ok("GET", "/api/modelchange", "old=A-100&new=V-110&old_version=2026-10")
        rows = {(r["kind"], r["old_part"], r["new_part"]): r for r in data["rows"]}
        c300 = rows[("SAME", "C-300", "C-300")]
        self.assertEqual((c300["kind_label"], c300["old_quantity"], c300["new_quantity"], c300["difference"]),
                         ("同じ品番", "3", "2", "-1"))
        self.assertEqual(rows[("ADDED", None, "F-600")]["new_name"], "部品F")

        by_scenario = self.ok("GET", "/api/modelchange", f"scenario={self.scenario_id}")
        self.assertEqual((by_scenario["old_product"], by_scenario["new_product"]), ("A-100", "V-110"))
        self.assertEqual({(r["kind"], r["old_part"], r["new_part"]) for r in by_scenario["rows"]},
                         set(rows))

    def test_part_map_changes_result(self) -> None:
        created = self.ok("POST", "/api/scenarios", body={"name": "Z 検討", "base": "A-100", "product_code": "Z-900",
                                                          "version_id": "2026-10"})
        sid = created["scenario"]["scenario_id"]
        self.ok("POST", f"/api/scenarios/{sid}/changes", body={"kind": "REMOVE", "part": "B-200", "quantity": "2"})
        self.ok("POST", f"/api/scenarios/{sid}/changes", body={"kind": "ADD", "part": "X-610", "quantity": "2"})
        before = self.ok("GET", "/api/modelchange", f"scenario={sid}")
        self.assertEqual({(r["kind"], r["old_part"], r["new_part"]) for r in before["rows"]},
                         {("SAME", "C-300", "C-300"), ("REMOVED", "B-200", None), ("ADDED", None, "X-610")})
        self.ok("POST", "/api/partmaps", body={"old_product": "A-100", "new_product": "Z-900",
                                               "old_part": "B-200", "new_part": "X-610"})
        after = self.ok("GET", "/api/modelchange", f"scenario={sid}")
        mapped = [r for r in after["rows"] if r["kind"] == "MAPPED"]
        self.assertEqual([(r["old_part"], r["new_part"], r["difference"], r["kind_label"]) for r in mapped],
                         [("B-200", "X-610", "0", "対応表")])
        self.assertEqual(len(after["rows"]), 2)
        self.assertEqual(len(after["part_maps"]), 1)

    def test_errors(self) -> None:
        self.assertEqual(self.call("GET", "/api/modelchange", "old=A-100&new=A-100&old_version=2026-10").status, 400)
        self.assertEqual(self.call("GET", "/api/modelchange", "old=A-100&new=NOPE&old_version=2026-10").status, 400)
        self.assertEqual(self.call("GET", "/api/modelchange", "old=A-100&new=V-110&old_version=1999").status, 404)
        self.assertEqual(self.call("GET", "/api/modelchange", "old=A-100&new=V-110").status, 400)


class DeleteVersion(DemoDbCase):
    def test_unused_version_deletes_and_used_one_does_not(self) -> None:
        response = self.call("DELETE", "/api/versions", "version=2026-10")
        self.assertEqual(response.status, 400)
        self.assertIn("試算が使っているので消せない", response.json()["error"])
        remaining = self.ok("DELETE", "/api/versions", "version=2026-12")
        self.assertEqual([v["version_id"] for v in remaining], ["2026-10"])
        self.assertEqual(self.call("DELETE", "/api/versions", "version=2026-12").status, 404)
        self.assertIn("manual は消せない", self.call("DELETE", "/api/versions", "version=manual").json()["error"])


class ServerLimits(unittest.TestCase):
    def test_delete_passes_and_oversized_body_is_413(self) -> None:
        import http.client
        import threading

        from bomcost.app import MAX_BODY_BYTES, bind, make_handler

        seen: list[str] = []

        def call(method: str, path: str, query: str, body: bytes) -> Response:
            seen.append(method)
            return Response(200, b"{}")

        server = bind(0, make_handler(call))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("DELETE", "/api/versions?version=x")
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.putrequest("POST", "/api/import")
            conn.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
            self.assertIn("大きすぎる", json.loads(response.read().decode("utf-8"))["error"])
            conn.close()
            self.assertEqual(seen, ["DELETE"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
