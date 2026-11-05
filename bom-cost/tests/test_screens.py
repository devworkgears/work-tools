"""画面の2段目の API（取り込み・手入力・部品から見る・モデルチェンジ・グループのメモ）。ソケットは開かない。データはすべて架空。"""

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

    def new_group(self, dev_number: str = "D-1", **extra: object) -> str:
        """空のグループを作り、ID を返す。"""
        return self.ok("POST", "/api/groups", body={"dev_number": dev_number, **extra})["group_id"]

    def swap_bom(self, version: object) -> None:
        """グループ version.version_id の BOM だけを書き換える（画面からはできない。変更・マスタ登録は残す下ごしらえ）。"""
        with self.store._conn:
            self.store._delete_rows(version.version_id, "bom")
            self.store._insert_rows(version.version_id, "bom", version)


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
        group = self.new_group(note="試し")
        body = {
            "group_id": group,
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

        groups = self.ok("GET", "/api/groups")
        self.assertEqual([(g["group_id"], g["warning_count"]) for g in groups], [(group, 1)])
        info = self.ok("GET", "/api/groups/info", f"group={group}")
        self.assertEqual(info["note"], "試し")
        self.assertIn("読み飛ばした", info["warnings"][0]["message"])

        # 同じ見出しの別のファイル: 保存した対応づけが候補に出て、選び直さずに取り込める
        other = "品番,品名,区分,登録者,備考\nR-1,製品R,P,例,\n"
        p = self.ok("POST", "/api/import/preview", body=csv_file("別の品目.csv", other))
        self.assertEqual(p["suggested"], "item")
        self.assertEqual(next(t for t in p["tables"] if t["id"] == "item")["saved"],
                         {"code": "品番", "name": "品名", "kind": "区分"})
        other_group = self.new_group()
        response = self.call("POST", "/api/import", body={"group_id": other_group,
                                                          "files": [csv_file("別の品目.csv", other)]})
        self.assertEqual(response.status, 201, response.body.decode("utf-8"))
        items = {i["code"] for i in self.ok("GET", "/api/items", f"group={other_group}")}
        self.assertEqual(items, {"R-1"})
        xlsx_again = xlsx_file("台数2.xlsx", [["製品品番", "年間生産台数", "集計年度"], ["R-1", 10, 2027]])
        self.assertEqual(self.ok("POST", "/api/import/preview", body=xlsx_again)["suggested"], "volume")

    def test_import_errors(self) -> None:
        group = self.new_group()
        missing = {"group_id": group, "files": [csv_file("品目.csv", ITEM_CSV, table="item", columns={"code": "品番"})]}
        self.assert_error(self.call("POST", "/api/import", body=missing), 400, "必須の列が対応づいていない: 名称")
        no_map = {"group_id": group, "files": [csv_file("品目.csv", ITEM_CSV, table="item")]}
        self.assert_error(self.call("POST", "/api/import", body=no_map), 400, "対応づけが無い")
        bad_header = {"group_id": group, "files": [csv_file("品目.csv", ITEM_CSV, table="item",
                                                             columns={"code": "品番", "name": "名前"})]}
        self.assert_error(self.call("POST", "/api/import", body=bad_header), 400, "列「名前」")
        ok = {"group_id": group, "files": [csv_file("品目.csv", ITEM_CSV, table="item",
                                                      columns={"code": "品番", "name": "品名"})]}
        self.assertEqual(self.call("POST", "/api/import", body=ok).status, 201)
        self.assert_error(self.call("POST", "/api/import", body={**ok, "group_id": "NOPE"}), 404, "NOPE")
        self.assert_error(self.call("POST", "/api/import", body={"files": ok["files"]}), 400, "グループ")
        self.assert_error(self.call("POST", "/api/import/preview", body={"filename": "a.txt", "content": b64(b"x")}),
                          400, "読めないファイルの種類")
        self.assert_error(self.call("POST", "/api/import/preview", body={"filename": "a.xlsx", "content": b64(b"x")}),
                          400, "Excel のファイルとして読めない")
        self.assert_error(self.call("POST", "/api/import/preview", body={"filename": "a.csv", "content": "@@"}),
                          400, "base64")

    def test_conflict_is_a_warning(self) -> None:
        text = "親品番,風船番号,子品番,員数\nQ-1,1,Q-2,2\nQ-1,1,Q-2,3\n"
        body = {"group_id": self.new_group(), "files": [csv_file("構成.csv", text, table="bom_line",
                                                       columns={"parent": "親品番", "balloon": "風船番号",
                                                                "child": "子品番", "quantity": "員数"})]}
        result = self.ok("POST", "/api/import", body=body)
        self.assertEqual(result["warning_count"], 1)
        self.assertIn("数量が食い違う", result["warnings"][0]["message"])
        self.assertEqual(self.ok("GET", "/api/groups")[0]["warning_count"], 1)


class SampleProcedure(EmptyDbCase):
    """見本の CSV で試す手順: 空の DB に samples/ の CSV を取り込み、手入力で X-610 を入れ、部品から見る画面で 60 円。"""

    def test_from_empty_db_to_60_yen(self) -> None:
        def sample(name: str, table: str, columns: dict) -> dict:
            return {"filename": name, "content": b64((SAMPLES / name).read_bytes()), "table": table, "columns": columns}

        group = self.new_group("D-100", dev_name="見本")
        result = self.ok("POST", "/api/import", body={"group_id": group, "files": [
            sample("部品マスタ.csv", "item", {"code": "品番", "name": "品名", "kind": "区分", "lifecycle": "状態",
                                              "has_children": "子部品有無"}),
            sample("構成マスタ.csv", "bom_line", {"parent": "親品番", "level": "レベル", "balloon": "風船番号",
                                                "child": "子品番", "quantity": "員数"}),
            sample("原価.csv", "cost", {"mbom_code": "MBOM品番", "material": "材料費", "direct": "直接費"}),
            sample("年間台数.csv", "volume", {"product": "製品品番", "annual": "年間生産台数"}),
        ]})
        self.assertEqual(result["counts"], {"items": 6, "lines": 5, "costs": 5, "volumes": 1})
        self.assertEqual(result["warning_count"], 5)  # 架空の品番は11桁でないので MBOM の変換をしない

        g = {"group_id": group}
        self.ok("PUT", "/api/manual/items", body={"code": "X-610", "name": "試作品", "costs": {"MATERIAL": "200"}, **g})
        self.ok("PUT", "/api/manual/volumes", body={"product": "V-110", "annual": "10000", **g})
        self.ok("PUT", "/api/molds", body={"part": "X-610", "amount": "2400000", "years": "2", **g})
        self.ok("PUT", "/api/reuse", body={"part": "X-610", "product": "K-200", "quantity": "2", "replaced_part": "F-600",
                                           **g})
        created = self.ok("POST", "/api/scenarios", body={"base": "A-100", "product_code": "V-110", **g})
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
        self.assertEqual((p["group_id"], p["head_label"], p["allocation_label"]), ("2026-10", "材料費", "使う数で分ける"))
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
        self.assertEqual(self.call("GET", "/api/parts/view", "part=NOPE&group=2026-10").status, 400)
        self.assertEqual(self.call("GET", "/api/parts/view", "part=X-610").status, 400)
        # 試算を選ばなければ、V-110 の版の構成には X-610 が無いので流用先だけ
        alone = self.ok("GET", "/api/parts/view", "part=X-610&group=2026-10")
        self.assertEqual([r["product"] for r in alone["rows"]], ["K-200"])


class Manual(DemoDbCase):
    G = {"group_id": "2026-10"}

    def test_trial_item_works_in_its_group(self) -> None:
        data = self.ok("PUT", "/api/manual/items", body={"code": "Y-700", "name": "試作品Y",
                                                         "costs": {"MATERIAL": "1,300", "DIRECT": ""}, **self.G})
        y = next(i for i in data["items"] if i["code"] == "Y-700")
        self.assertEqual((y["costs"]["MATERIAL"], y["costs"]["DIRECT"]), ("1300", None))
        self.assertIn("Y-700", {i["code"] for i in self.ok("GET", "/api/items", "group=2026-10")})
        self.assertNotIn("Y-700", {i["code"] for i in self.ok("GET", "/api/items", "group=2026-12")})
        after = self.ok("POST", self.url("/changes"), body={"kind": "ADD", "part": "Y-700", "quantity": "1"})
        self.assertEqual(after["total"], "5950")
        # 2026-12 のグループにも同じ登録と同じ変更を入れると、そのグループの原価で
        twelve = {"group_id": "2026-12"}
        self.ok("PUT", "/api/manual/items", body={"code": "Y-700", "name": "試作品Y", "costs": {"MATERIAL": "1,300"},
                                                  **twelve})
        sid = self.ok("POST", "/api/scenarios", body={"base": "A-100", "product_code": "V-110", **twelve})["scenario"]["scenario_id"]
        for change in ({"kind": "ADD", "part": "F-600"}, {"kind": "REMOVE", "part": "C-300"}, {"kind": "ADD", "part": "Y-700"}):
            switched = self.ok("POST", f"/api/scenarios/{sid}/changes", body={**change, "quantity": "1"})
        self.assertEqual(switched["total"], "5990")
        # 直す・消す（試作品 X-610 などほかの手入力は残る）
        self.ok("PUT", "/api/manual/items", body={"code": "Y-700", "name": "試作品Y", "costs": {"MATERIAL": "1000"},
                                                  **twelve})
        self.assertEqual(self.ok("GET", f"/api/scenarios/{sid}")["total"], "5690")
        self.assertEqual(self.ok("GET", self.url())["total"], "5950")
        data = self.ok("DELETE", "/api/manual/items", "code=Y-700&group=2026-10")
        self.assertEqual({i["code"] for i in data["items"]}, {"X-610"})
        self.assertEqual([v["product"] for v in data["volumes"]], ["V-110"])

    def test_mold_volume_reuse_part_map_crud(self) -> None:
        g = self.G
        data = self.ok("PUT", "/api/molds", body={"part": "X-610", "amount": "3000000", "years": "3", **g})
        self.assertEqual(data["molds"], [{"part": "X-610", "amount": "3000000", "years": "3", "annual_amount": "1000000"}])
        data = self.ok("PUT", "/api/manual/volumes", body={"product": "V-110", "annual": "8000", **g})
        self.assertEqual(data["volumes"], [{"product": "V-110", "annual": "8000", "kind": "新製品の計画"}])
        data = self.ok("PUT", "/api/reuse", body={"part": "X-610", "product": "K-200", "quantity": "3", "replaced_part": "F-600",
                                                  **g})
        self.assertEqual(data["reuse"], [{"part": "X-610", "product": "K-200", "quantity": "3", "replaced_part": "F-600"}])
        data = self.ok("PUT", "/api/reuse", body={"part": "X-610", "product": "K-300", "quantity": "1",
                                                  "original": {"part": "X-610", "product": "K-200"}, **g})
        self.assertEqual([r["product"] for r in data["reuse"]], ["K-300"])
        data = self.ok("POST", "/api/partmaps", body={"old_product": "A-100", "new_product": "V-110",
                                                      "old_part": "C-300", "new_part": "X-610", **g})
        self.assertEqual(len(data), 1)
        data = self.ok("PUT", "/api/partmaps", body={"original": data[0], "old_product": "A-100", "new_product": "V-110",
                                                     "old_part": "C-300", "new_part": "F-600", "new_balloon": "2", **g})
        self.assertEqual((data[0]["new_part"], data[0]["new_balloon"]), ("F-600", "2"))
        with Store(self.path) as other:  # 保存されている
            self.assertEqual(other.part_map("2026-10", "A-100", "V-110"), {"C-300": "F-600"})
            self.assertEqual(other.mold_costs("2026-10")["X-610"].years, 3)
        self.assertEqual(self.ok("DELETE", "/api/partmaps",
                                 "old_product=A-100&new_product=V-110&old_part=C-300&new_part=F-600&new_balloon=2"
                                 "&group=2026-10"), [])
        self.ok("DELETE", "/api/reuse", "part=X-610&product=K-300&group=2026-10")
        self.ok("DELETE", "/api/molds", "part=X-610&group=2026-10")
        data = self.ok("DELETE", "/api/manual/volumes", "product=V-110&group=2026-10")
        self.assertEqual((data["molds"], data["reuse"], data["volumes"], data["part_maps"]), ([], [], [], []))

    def test_edit_with_original_replaces_the_row(self) -> None:
        # 品番を変えて直すと元の行は消える（original が無ければ今どおり同じ品番を入れ替え）
        g = self.G
        data = self.ok("PUT", "/api/manual/items", body={"code": "X-611", "name": "試作品改", "costs": {"MATERIAL": "250"},
                                                         "original": {"code": "X-610"}, **g})
        items = {i["code"]: i for i in data["items"]}
        self.assertEqual(set(items), {"X-611"})
        self.assertEqual((items["X-611"]["name"], items["X-611"]["costs"]["MATERIAL"]), ("試作品改", "250"))
        data = self.ok("PUT", "/api/manual/items", body={"code": "X-611", "name": "試作品改2", "original": {"code": ""}, **g})
        self.assertEqual([i["name"] for i in data["items"]], ["試作品改2"])
        data = self.ok("PUT", "/api/manual/volumes", body={"product": "V-111", "annual": "9000",
                                                           "original": {"product": "V-110"}, **g})
        self.assertEqual(data["volumes"], [{"product": "V-111", "annual": "9000", "kind": "新製品の計画"}])
        self.ok("PUT", "/api/molds", body={"part": "X-610", "amount": "3000000", "years": "3", **g})
        data = self.ok("PUT", "/api/molds", body={"part": "X-611", "amount": "1000000", "years": "2",
                                                  "original": {"part": "X-610"}, **g})
        self.assertEqual([m["part"] for m in data["molds"]], ["X-611"])
        data = self.ok("PUT", "/api/molds", body={"part": "X-611", "amount": "2000000", "original": {"part": "X-611"}, **g})
        self.assertEqual([m["amount"] for m in data["molds"]], ["2000000"])

    def test_errors_in_japanese(self) -> None:
        g = self.G
        bad = [
            (("PUT", "/api/manual/items", "", {"code": "Y", "name": "y", "costs": {"MATERIAL": "abc"}, **g}), "材料費を数として読めない"),
            (("PUT", "/api/manual/items", "", {"code": "Y", "name": "y", "costs": {"XYZ": "1"}, **g}), "費目が分からない"),
            (("PUT", "/api/manual/items", "", {"code": "Y", "costs": {}, **g}), "名称が空"),
            (("PUT", "/api/manual/volumes", "", {"product": "V-110", "annual": "たくさん", **g}), "年間台数を数として読めない"),
            (("PUT", "/api/molds", "", {"part": "X-610", "amount": [1], **g}), "型費を数として読めない"),
            (("PUT", "/api/molds", "", {"part": "X-610", "amount": "1", "years": "0", **g}), "償却年数は正の数"),
            (("PUT", "/api/reuse", "", {"part": "X-610", "product": "K-200", "quantity": "x", **g}), "使用数を数として読めない"),
            (("DELETE", "/api/manual/items", "code=NOPE&group=2026-10", None), "手入力の品目 NOPE は無い"),
            (("DELETE", "/api/partmaps", "old_product=A&new_product=B&old_part=C&new_part=D&group=2026-10", None), "対応表の行が無い"),
            (("POST", "/api/partmaps", "", {"old_product": "A", "new_product": "B", "old_part": "C", "new_part": "C", **g}),
             "自動で対応"),
            (("PUT", "/api/molds", "", {"part": "X-610", "amount": "1"}), "グループ"),
        ]
        for (method, path, query, body), words in bad:
            with self.subTest(words=words):
                response = self.call(method, path, query, body)
                self.assertTrue(400 <= response.status < 500, response.body.decode("utf-8"))
                self.assertIn(words, response.json()["error"])


class ModelChange(DemoDbCase):
    def test_groups_and_scenario(self) -> None:
        data = self.ok("GET", "/api/modelchange", "old=A-100&new=V-110&old_group=2026-10")
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
        created = self.ok("POST", "/api/scenarios", body={"base": "A-100", "product_code": "Z-900", "group_id": "2026-12"})
        sid = created["scenario"]["scenario_id"]
        self.ok("POST", f"/api/scenarios/{sid}/changes", body={"kind": "REMOVE", "part": "B-200", "quantity": "2"})
        self.ok("POST", f"/api/scenarios/{sid}/changes", body={"kind": "ADD", "part": "X-610", "quantity": "2"})
        before = self.ok("GET", "/api/modelchange", f"scenario={sid}")
        self.assertEqual({(r["kind"], r["old_part"], r["new_part"]) for r in before["rows"]},
                         {("SAME", "C-300", "C-300"), ("REMOVED", "B-200", None), ("ADDED", None, "X-610")})
        self.ok("POST", "/api/partmaps", body={"old_product": "A-100", "new_product": "Z-900",
                                               "old_part": "B-200", "new_part": "X-610", "group_id": "2026-12"})
        after = self.ok("GET", "/api/modelchange", f"scenario={sid}")
        mapped = [r for r in after["rows"] if r["kind"] == "MAPPED"]
        self.assertEqual([(r["old_part"], r["new_part"], r["difference"], r["kind_label"]) for r in mapped],
                         [("B-200", "X-610", "0", "対応表")])
        self.assertEqual(len(after["rows"]), 2)
        self.assertEqual(len(after["part_maps"]), 1)

    def test_errors(self) -> None:
        self.assertEqual(self.call("GET", "/api/modelchange", "old=A-100&new=A-100&old_group=2026-10").status, 400)
        self.assertEqual(self.call("GET", "/api/modelchange", "old=A-100&new=NOPE&old_group=2026-10").status, 400)
        self.assertEqual(self.call("GET", "/api/modelchange", "old=A-100&new=V-110&old_group=1999").status, 404)
        self.assertEqual(self.call("GET", "/api/modelchange", "old=A-100&new=V-110").status, 400)


class GroupNote(EmptyDbCase):
    def test_note_changes_and_nothing_else(self) -> None:
        group = self.new_group(note="前")
        body = {"group_id": group,
                "files": [csv_file("構成.csv", BOM_CSV, table="bom_line",
                                   columns={"parent": "親品番", "child": "子品番", "quantity": "員数"})]}
        self.assertEqual(self.call("POST", "/api/import", body=body).status, 201)
        before = self.ok("GET", "/api/groups/info", f"group={group}")
        items_before = self.store.load_group(group, with_manual=False)

        listed = self.ok("PUT", "/api/groups/note", f"group={group}", body={"note": "後"})
        self.assertEqual([(g["group_id"], g["note"], g["label"]) for g in listed], [(group, "後", "D-1（後）")])
        after = self.ok("GET", "/api/groups/info", f"group={group}")
        self.assertEqual(after, {**before, "note": "後", "label": "D-1（後）"})
        items_after = self.store.load_group(group, with_manual=False)
        self.assertEqual(items_after.lines, items_before.lines)
        self.assertEqual(items_after.items, items_before.items)
        self.assertEqual(self.ok("PUT", "/api/groups/note", f"group={group}", body={})[0]["note"], "")

    def test_missing_group_is_404(self) -> None:
        self.assertEqual(self.call("PUT", "/api/groups/note", "group=nope", body={"note": "x"}).status, 404)
        self.assertEqual(self.call("PUT", "/api/groups/note", body={"note": "x"}).status, 400)


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
            conn.request("DELETE", "/api/groups?group=x")
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

    def test_other_host_or_origin_is_403(self) -> None:
        import http.client
        import threading

        from bomcost.app import bind, make_handler

        seen: list[str] = []

        def call(method: str, path: str, query: str, body: bytes) -> Response:
            seen.append(method)
            return Response(200, b"{}")

        server = bind(0, make_handler(call))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            cases = [
                ({}, 200),
                ({"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"}, 200),
                ({"Origin": f"http://127.0.0.1:{port}"}, 200),
                ({"Host": f"evil.example:{port}"}, 403),
                ({"Origin": "http://evil.example"}, 403),
                ({"Origin": "null"}, 403),
                ({"Origin": f"http://127.0.0.1:{port + 1}"}, 403),
            ]
            for headers, status in cases:
                with self.subTest(headers=headers):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("POST", "/api/scenarios", body=b"{}", headers=headers)
                    self.assertEqual(conn.getresponse().status, status)
                    conn.close()
            self.assertEqual(seen, ["POST"] * 3)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
