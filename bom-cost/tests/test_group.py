"""製品群の API（製品の一覧・部品の検索・検討名ごとの替える・足す）。samples/ の CSV を取り込んだ DB で確かめる。ソケットは開かない。"""

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from bomcost import BomLine, Item, Version
from bomcost.api import Api
from bomcost.app import static_response
from bomcost.demo import build_demo
from bomcost.store import Store
from bomcost.group import Group, _Structure
from test_screens import SAMPLES, EmptyDbCase, b64

NAME = "群の検討"


class SampleDbCase(EmptyDbCase):
    """SampleProcedure と同じ手順で samples/ を版 2026-10 に取り込み、手入力の試作品 X-610（材料費 200）を足す。"""

    def setUp(self) -> None:
        super().setUp()

        def sample(name: str, table: str, columns: dict) -> dict:
            return {"filename": name, "content": b64((SAMPLES / name).read_bytes()), "table": table, "columns": columns}

        self.ok("POST", "/api/import", body={"version_id": "2026-10", "files": [
            sample("部品マスタ.csv", "item", {"code": "品番", "name": "品名", "kind": "区分", "lifecycle": "状態",
                                              "has_children": "子部品有無"}),
            sample("構成マスタ.csv", "bom_line", {"parent": "親品番", "level": "レベル", "balloon": "風船番号",
                                                "child": "子品番", "quantity": "員数"}),
            sample("原価.csv", "cost", {"mbom_code": "MBOM品番", "material": "材料費", "direct": "直接費"}),
            sample("年間台数.csv", "volume", {"product": "製品品番", "annual": "年間生産台数"}),
        ]})
        self.ok("PUT", "/api/manual/items", body={"code": "X-610", "name": "試作品", "costs": {"MATERIAL": "200"}})

    def products(self, query: str = "") -> dict:
        return self.ok("GET", "/api/group/products", "version=2026-10" + query)

    def parts(self, query: str) -> dict:
        return self.ok("GET", "/api/group/parts", "version=2026-10" + query)

    def change(self, product: str, **change: str) -> dict:
        return self.ok("POST", "/api/group/changes", body={"version_id": "2026-10", "name": NAME, "product": product,
                                                           **change})

    def by_code(self, query: str = "") -> dict[str, dict]:
        return {p["code"]: p for p in self.products(f"&name={NAME}{query}")["products"]}


class Products(SampleDbCase):
    def test_lists_items_of_product_kind(self) -> None:
        p = self.products()
        # 区分の記号は構成の一番上の親（V-110）に多い記号 P を製品とみなす
        self.assertEqual(p["kind"], "P")
        self.assertEqual([(x["code"], x["name"], x["has_structure"]) for x in p["products"]],
                         [("A-100", "土台の製品", True), ("K-200", "既存品", False), ("V-110", "新しい製品", True)])
        self.assertEqual((p["count"], p["limit"], p["truncated"]), (3, 500, False))
        self.assertNotIn("current", p["products"][0])  # 検討名が無ければ原価は出さない

    def test_filter_by_code_or_name_and_limit(self) -> None:
        self.assertEqual([x["code"] for x in self.products("&q=新しい")["products"]], ["V-110"])
        self.assertEqual([x["code"] for x in self.products("&q=a-1")["products"]], ["A-100"])
        cut = self.products("&limit=2")
        self.assertEqual(([x["code"] for x in cut["products"]], cut["count"], cut["truncated"]),
                         (["A-100", "K-200"], 3, True))
        self.assertEqual([x["code"] for x in self.products("&kind=B")["products"]], ["B-200", "C-300", "F-600"])

    def test_costs_before_any_change(self) -> None:
        rows = self.by_code()
        self.assertEqual({c: (r["current"], r["after"], r["difference"], r["change_count"], r["scenario_id"])
                          for c, r in rows.items()},
                         {"A-100": ("200", "200", "0", 0, None), "K-200": ("3000", "3000", "0", 0, None),
                          "V-110": ("-50", "-50", "0", 0, None)})
        self.assertEqual(self.store.list_scenarios(), [])  # 引くだけでは試算を作らない

    def test_input_errors(self) -> None:
        self.assert_error(self.call("GET", "/api/group/products"), 400, "版")
        self.assert_error(self.call("GET", "/api/group/products", "version=nope"), 404, "nope")
        self.assert_error(self.call("GET", "/api/group/products", "version=2026-10&limit=0"), 400, "limit")
        self.assert_error(self.call("GET", "/api/group/parts", "version=2026-10"), 400, "part")


class Parts(SampleDbCase):
    def test_product_by_part_rows_with_usage(self) -> None:
        r = self.parts("&part=部品B")
        self.assertEqual([(x["product"], x["part"], x["part_name"], x["usage"]) for x in r["rows"]],
                         [("A-100", "B-200", "部品B", "2"), ("V-110", "B-200", "部品B", "2")])
        self.assertFalse(r["truncated"])
        # 構成の深い所と直下の両方にある部品は、1台あたりの使用数を足し合わせる（V-110: 3 − 1）
        c = self.parts("&part=c-3")
        self.assertEqual([(x["product"], x["usage"]) for x in c["rows"]], [("A-100", "3"), ("V-110", "2")])

    def test_with_product_filter_and_limit(self) -> None:
        self.assertEqual([x["product"] for x in self.parts("&part=部品B&q=V-")["rows"]], ["V-110"])
        cut = self.parts("&part=部品&limit=2")
        self.assertEqual((len(cut["rows"]), cut["truncated"]), (2, True))
        # 製品そのもの（組立品として下に入る A-100 は別）は自分の行に出さない
        self.assertEqual([(x["product"], x["part"]) for x in self.parts("&part=A-100")["rows"]], [("V-110", "A-100")])


class Changes(SampleDbCase):
    def test_replace_creates_scenario_and_changes_cost(self) -> None:
        r = self.parts("&part=部品B&q=A-100")["rows"][0]
        first = self.change(r["product"], kind="REPLACE", part=r["part"], replacement="X-610", quantity=r["usage"])
        self.assertTrue(first["created"])
        sid = first["scenario_id"]
        summary = first["summary"]
        self.assertEqual((summary["scenario"]["name"], summary["scenario"]["base"], summary["total"]),
                         (NAME, "A-100", "-2600"))  # 200 − 2×1,600 ＋ 2×200
        rows = self.by_code()
        self.assertEqual((rows["A-100"]["current"], rows["A-100"]["after"], rows["A-100"]["difference"],
                          rows["A-100"]["change_count"], rows["A-100"]["scenario_id"]), ("200", "-2600", "-2800", 1, sid))
        self.assertEqual((rows["V-110"]["after"], rows["V-110"]["change_count"]), ("-50", 0))
        self.assertEqual(self.parts(f"&part=部品B&name={NAME}")["rows"][0]["replaced_by"], ["X-610"])

        # 同じ検討名・製品の2件目は同じ試算に重なる
        second = self.change("A-100", kind="ADD", part="F-600", quantity="1")
        self.assertEqual((second["created"], second["scenario_id"], second["summary"]["total"]), (False, sid, "-2350"))
        # 構成の無い製品にも足せる
        added = self.change("K-200", kind="ADD", part="F-600", quantity="1")
        self.assertNotEqual(added["scenario_id"], sid)
        rows = self.by_code()
        self.assertEqual((rows["A-100"]["change_count"], rows["K-200"]["after"], rows["K-200"]["difference"]),
                         (2, "3250", "250"))

        # 取り消しは今の undo
        self.ok("POST", f"/api/scenarios/{sid}/undo", body={})
        self.ok("POST", f"/api/scenarios/{sid}/undo", body={})
        a = self.by_code()["A-100"]
        self.assertEqual((a["after"], a["difference"], a["change_count"], a["scenario_id"]), ("200", "0", 0, sid))

    def test_other_name_or_version_is_another_scenario(self) -> None:
        sid = self.change("A-100", kind="ADD", part="F-600")["scenario_id"]
        other = self.ok("POST", "/api/group/changes", body={"version_id": "2026-10", "name": "別の検討", "product": "A-100",
                                                             "kind": "ADD", "part": "F-600"})
        self.assertNotEqual(other["scenario_id"], sid)
        self.assertEqual(self.ok("GET", "/api/group/names", "version=2026-10"), [NAME, "別の検討"])
        # 試算は今の形のまま（製品から見る画面でも開ける）
        tree = self.ok("GET", f"/api/scenarios/{sid}/tree")
        self.assertEqual(tree["view"]["total"], "450")

    def test_failed_change_leaves_no_new_scenario(self) -> None:
        bad = self.call("POST", "/api/group/changes", body={"version_id": "2026-10", "name": NAME, "product": "A-100",
                                                             "kind": "REPLACE", "part": "B-200", "replacement": "NOPE"})
        self.assert_error(bad, 400, "NOPE")
        self.assertEqual(self.store.list_scenarios(), [])
        self.assert_error(self.call("POST", "/api/group/changes", body={"version_id": "2026-10", "product": "A-100",
                                                                        "kind": "ADD", "part": "F-600"}), 400, "検討名")
        self.assert_error(self.call("POST", "/api/group/changes", body={"version_id": "2026-10", "name": NAME,
                                                                        "product": "ZZ", "kind": "ADD", "part": "F-600"}),
                          400, "ZZ")
        # 既にある試算への変更が通らなくても、試算は消さない
        sid = self.change("A-100", kind="ADD", part="F-600")["scenario_id"]
        self.assert_error(self.call("POST", "/api/group/changes", body={"version_id": "2026-10", "name": NAME,
                                                                        "product": "A-100", "kind": "MOVE", "part": "F-600"}),
                          400, "変更の種類")
        self.assertEqual([s.scenario_id for s in self.store.list_scenarios()], [sid])


class Tree(SampleDbCase):
    def tree(self, query: str) -> dict:
        return self.ok("GET", "/api/group/tree", "version=2026-10" + query)

    def test_version_structure_without_scenario(self) -> None:
        r = self.tree("&product=A-100")
        self.assertIsNone(r["scenario_id"])
        self.assertEqual((r["view"]["mode"], r["node"]["code"]), ("bulk", "A-100"))
        self.assertEqual([(c["code"], c["name"], c["quantity"], c["cost"], c["amount"], c["has_children"], c["role"])
                          for c in r["children"]],
                         [("B-200", "部品B", "2", "1600", "3200", False, ""),
                          ("C-300", "部品C", "3", "500", "1500", False, "")])
        # 検討名があっても試算が無ければ版の構成。見るだけで試算は作らない
        self.assertIsNone(self.tree(f"&product=A-100&name={NAME}")["scenario_id"])
        self.assertEqual(self.store.list_scenarios(), [])
        # 子を持つ品目は code でさらに開ける
        v = self.tree("&product=V-110")
        self.assertTrue(next(c for c in v["children"] if c["code"] == "A-100")["has_children"])
        self.assertEqual([c["code"] for c in self.tree("&product=V-110&code=A-100")["children"]], ["B-200", "C-300"])

    def test_scenario_structure_matches_product_screen_tree(self) -> None:
        sid = self.change("A-100", kind="REPLACE", part="C-300", replacement="F-600", quantity="3")["scenario_id"]
        r = self.tree(f"&product=A-100&name={NAME}")
        self.assertEqual((r["scenario_id"], r["view"]["mode"]), (sid, "diff"))
        self.assertEqual([(c["code"], c["quantity"], c["role"]) for c in r["children"]],
                         [("A-100", "1", "土台"), ("C-300", "-3", "抜く"), ("F-600", "3", "足す")])
        same = self.ok("GET", f"/api/scenarios/{sid}/tree", "mode=diff&head=MATERIAL")
        self.assertEqual((r["node"], r["children"]), (same["node"], same["children"]))
        inner = self.tree(f"&product=A-100&name={NAME}&code=A-100")
        self.assertEqual(inner["children"], self.ok("GET", f"/api/scenarios/{sid}/tree", "code=A-100")["children"])

    def test_input_errors(self) -> None:
        self.assert_error(self.call("GET", "/api/group/tree", "version=2026-10"), 400, "製品")
        self.assert_error(self.call("GET", "/api/group/tree", "version=2026-10&product=ZZ"), 400, "ZZ")
        self.assert_error(self.call("GET", "/api/group/tree", "version=2026-10&product=A-100&code=ZZ"), 400, "ZZ")


class AddScenarios(SampleDbCase):
    def add(self, **extra: str) -> dict:
        return self.ok("POST", "/api/group/scenarios", body={"version_id": "2026-10", "name": NAME, **extra})

    def test_creates_one_per_listed_product_once(self) -> None:
        self.change("A-100", kind="ADD", part="F-600")
        r = self.add()
        self.assertEqual((r["created"], r["existing"], r["truncated"]), (2, 1, False))
        self.assertEqual(sorted(s.base for s in self.store.list_scenarios()), ["A-100", "K-200", "V-110"])
        again = self.add()
        self.assertEqual((again["created"], again["existing"]), (0, 3))
        # 作った試作品は製品の一覧と開いた構成に出る（変更はまだ無い）
        rows = self.by_code()
        self.assertEqual((rows["V-110"]["scenario_id"] is not None, rows["V-110"]["change_count"],
                          rows["V-110"]["difference"]), (True, 0, "0"))
        tree = self.ok("GET", "/api/group/tree", f"version=2026-10&product=V-110&name={NAME}")
        self.assertEqual([(c["code"], c["role"]) for c in tree["children"]], [("V-110", "土台")])

    def test_only_filtered_products_and_limit(self) -> None:
        r = self.add(q="V-")
        self.assertEqual((r["created"], [s.base for s in self.store.list_scenarios()]), (1, ["V-110"]))
        cut = self.ok("POST", "/api/group/scenarios", body={"version_id": "2026-10", "name": "別", "limit": 2})
        self.assertEqual((cut["created"], cut["truncated"]), (2, True))

    def test_input_errors(self) -> None:
        self.assert_error(self.call("POST", "/api/group/scenarios", body={"version_id": "2026-10"}), 400, "検討名")
        self.assert_error(self.call("POST", "/api/group/scenarios", body={"name": NAME}), 400, "版")


class Trials(SampleDbCase):
    """試作キーの一覧と、試作キーの試算がある製品だけに絞る trial。"""

    def test_trial_keys_with_product_count_and_version(self) -> None:
        self.assertEqual(self.ok("GET", "/api/group/trials"), [])
        self.ok("POST", "/api/group/scenarios", body={"version_id": "2026-10", "name": NAME, "q": "-1"})
        self.change("K-200", kind="ADD", part="F-600")
        self.ok("POST", "/api/group/changes", body={"version_id": "2026-10", "name": "別の検討", "product": "A-100",
                                                     "kind": "ADD", "part": "F-600"})
        expected = [{"name": NAME, "version_id": "2026-10", "count": 3},
                    {"name": "別の検討", "version_id": "2026-10", "count": 1}]
        self.assertEqual(self.ok("GET", "/api/group/trials"), expected)
        self.assertEqual(self.ok("GET", "/api/group/trials", "version=2026-10"), expected)
        self.assertEqual(self.ok("GET", "/api/group/trials", "version=nope"), [])

    def test_trial_flag_limits_products_and_parts(self) -> None:
        self.ok("POST", "/api/group/scenarios", body={"version_id": "2026-10", "name": NAME, "q": "V-",
                                                      "head": "MANUFACTURING"})
        p = self.products(f"&name={NAME}&trial=1&head=MANUFACTURING")
        self.assertEqual(([x["code"] for x in p["products"]], p["count"], p["head_label"]), (["V-110"], 1, "製造原価"))
        self.assertIsNotNone(p["products"][0]["scenario_id"])
        self.assertEqual([x["product"] for x in self.parts(f"&part=部品B&name={NAME}&trial=1")["rows"]], ["V-110"])
        # 付けなければ今のまま全部の製品
        self.assertEqual(len(self.products(f"&name={NAME}")["products"]), 3)
        self.assert_error(self.call("GET", "/api/group/products", "version=2026-10&trial=1"), 400, "検討名")
        self.assert_error(self.call("GET", "/api/group/parts", "version=2026-10&part=B&trial=1"), 400, "検討名")


class ManualSplit(SampleDbCase):
    """手入力の一覧を製品と部品に分ける。製品の表には試算（新製品）も並ぶ。"""

    def put(self, code: str, **extra: str) -> dict:
        return self.ok("PUT", "/api/manual/items", body={"code": code, "name": f"品目{code}", **extra})

    def test_role_decides_kind_and_table(self) -> None:
        self.put("N-900", role="product")
        data = self.put("Q-901", role="part", kind="B")
        self.assertEqual(data["product_kind"], "P")  # 版で製品とみなす記号に合わせる
        by_code = {i["code"]: (i["kind"], i["role"]) for i in data["items"]}
        self.assertEqual(by_code, {"X-610": ("", "part"), "N-900": ("P", "product"), "Q-901": ("B", "part")})
        # 製品として登録したものは製品群の製品の一覧にも出る
        self.assertIn("N-900", [x["code"] for x in self.products()["products"]])

    def test_part_cannot_take_product_kind_and_role_is_checked(self) -> None:
        self.assert_error(self.call("PUT", "/api/manual/items", body={"code": "Q", "name": "q", "role": "part",
                                                                      "kind": "P"}), 400, "製品の記号")
        self.assert_error(self.call("PUT", "/api/manual/items", body={"code": "Q", "name": "q", "role": "x"}),
                          400, "role")

    def test_without_role_keeps_kind_as_sent(self) -> None:
        data = self.put("R-902", kind="P")
        self.assertEqual({i["code"]: i["role"] for i in data["items"]}["R-902"], "product")

    def test_trials_are_listed_as_products(self) -> None:
        self.change("V-110", kind="ADD", part="F-600")
        self.ok("POST", "/api/group/scenarios", body={"version_id": "2026-10", "name": NAME, "q": "A-"})
        trials = self.ok("GET", "/api/manual")["trials"]
        self.assertEqual([(t["code"], t["name"], t["base"], t["trial_name"], t["version_id"]) for t in trials],
                         [(f"V-110#{NAME}", "新しい製品", "V-110", NAME, "2026-10"),
                          (f"A-100#{NAME}", "土台の製品", "A-100", NAME, "2026-10")])


class EmptyManual(EmptyDbCase):
    def test_product_kind_without_versions(self) -> None:
        data = self.ok("PUT", "/api/manual/items", body={"code": "N-1", "name": "n", "role": "product"})
        self.assertEqual((data["product_kind"], data["items"][0]["kind"], data["trials"]), ("製品", "製品", []))


class DemoTree(unittest.TestCase):
    def test_demo_a100_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.sqlite"
            build_demo(path)
            with Store(path) as store:
                raw = Api(store).handle("GET", "/api/group/tree", "version=2026-10&product=A-100", b"")
                self.assertEqual(raw.status, 200, raw.body.decode("utf-8"))
                r = raw.json()
        self.assertEqual([(c["code"], c["quantity"], c["cost"], c["amount"], c["has_children"], c["own_missing"])
                          for c in r["children"]],
                         [("B-200", "2", "1600", "3200", False, False), ("C-300", "3", "500", "1500", False, False)])


class ChangeList(SampleDbCase):
    def test_all_changes_of_name_with_differences(self) -> None:
        self.change("K-200", kind="ADD", part="F-600", quantity="1")
        self.change("A-100", kind="REPLACE", part="B-200", replacement="X-610", quantity="2")
        self.ok("POST", "/api/group/changes", body={"version_id": "2026-10", "name": "別の検討", "product": "A-100",
                                                     "kind": "ADD", "part": "F-600"})
        r = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")
        self.assertEqual([(x["product"], x["kind"], x["text"], x["cost_before"], x["cost_after"], x["difference"])
                          for x in r["rows"]],
                         [("A-100", "REPLACE", "B-200 → X-610（×2）", "200", "-2600", "-2800"),
                          ("K-200", "ADD", "F-600 を 1 足す", "3000", "3250", "250")])
        self.assertEqual((r["count"], r["difference"], r["missing_count"]), (2, "-2550", 0))
        self.assertEqual((r["rows"][0]["part"], r["rows"][0]["replacement"], r["rows"][0]["quantity"]),
                         ("B-200", "X-610", "2"))
        # 取り消した変更は一覧から消える
        self.ok("POST", f"/api/scenarios/{r['rows'][1]['scenario_id']}/undo", body={})
        after = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")
        self.assertEqual((after["count"], after["difference"]), (1, "-2800"))
        self.assert_error(self.call("GET", "/api/group/changes", "version=2026-10"), 400, "検討名")

    def test_empty(self) -> None:
        r = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")
        self.assertEqual((r["rows"], r["difference"]), ([], "0"))


class Compare(SampleDbCase):
    HEADS = ["MATERIAL", "AMORTIZATION", "ADJUSTMENT", "DIRECT", "INDIRECT", "MANUFACTURING"]

    def compare(self, query: str) -> dict:
        return self.ok("GET", "/api/group/compare", "version=2026-10" + query)

    def setUp(self) -> None:
        super().setUp()
        self.change("A-100", kind="REPLACE", part="B-200", replacement="X-610", quantity="2")
        self.ok("POST", "/api/group/changes", body={"version_id": "2026-10", "name": "別の検討", "product": "A-100",
                                                     "kind": "ADD", "part": "F-600", "quantity": "1"})

    def test_base_row_then_one_row_per_name_with_scenario(self) -> None:
        names = quote(json.dumps([NAME, "別の検討", "無い検討"]))
        r = self.compare(f"&names={names}&ignore_amortization=1")
        self.assertEqual([h["id"] for h in r["heads"]], self.HEADS)
        self.assertEqual([h["label"] for h in r["heads"]], ["材料費", "償却費", "調整費", "直接費", "間接費", "製造原価"])
        self.assertEqual([(x["kind"], x["product"], x["name"]) for x in r["rows"]],
                         [("base", "A-100", None), ("trial", "A-100", NAME), ("trial", "A-100", "別の検討"),
                          ("base", "K-200", None), ("base", "V-110", None)])
        self.assertEqual([list(x["costs"]) for x in r["rows"]], [self.HEADS] * 5)
        # 元＝版の構成、新＝土台＋変更（B-200 → X-610 ×2 は 200 − 2×1,600 ＋ 2×200・F-600 を足すと ＋250）
        self.assertEqual([x["costs"]["MATERIAL"] for x in r["rows"]], ["200", "-2600", "450", "3000", "-50"])
        self.assertEqual({x["costs"]["AMORTIZATION"] for x in r["rows"]}, {None})
        self.assertIsNone(r["rows"][0]["costs"]["DIRECT"])  # 未登録は空
        self.assertEqual(r["rows"][0]["product_name"], "土台の製品")
        self.assertEqual(self.store.list_scenarios()[0].scenario_id, r["rows"][1]["scenario_id"])
        # 名前が1つなら新の行も1つ。製品の絞り込みも効く
        one = self.compare(f"&names={quote(NAME)}&q=土台&ignore_amortization=true")
        self.assertEqual([(x["kind"], x["name"]) for x in one["rows"]], [("base", None), ("trial", NAME)])

    def test_registered_values_of_replacement_move_new_row(self) -> None:
        names = quote(json.dumps([NAME]))
        before = self.compare(f"&names={names}&q=A-100&ignore_amortization=1")["rows"][1]["costs"]["MATERIAL"]
        self.ok("PUT", "/api/manual/items", body={"code": "X-610", "name": "試作品", "costs": {"MATERIAL": "300"}})
        rows = self.compare(f"&names={names}&q=A-100&ignore_amortization=1")["rows"]
        self.assertEqual((before, rows[0]["costs"]["MATERIAL"], rows[1]["costs"]["MATERIAL"]), ("-2600", "200", "-2400"))

    def test_amortization_summed_like_other_heads(self) -> None:
        for code, name, amort in (("B-200", "部品B", "10"), ("C-300", "部品C", "5"), ("X-610", "試作品", "30")):
            self.ok("PUT", "/api/manual/items", body={"code": code, "name": name, "kind": "B",
                                                     "costs": {"AMORTIZATION": amort}})
        rows = self.compare(f"&names={quote(NAME)}&q=A-100")["rows"]
        # 元: 2×10 ＋ 3×5、新: 35 − 2×10 ＋ 2×30
        self.assertEqual([x["costs"]["AMORTIZATION"] for x in rows], ["35", "75"])
        # 親の A-100 自身は償却費が未登録なので、出せても未登録ありの印は付く
        self.assertIn("AMORTIZATION", rows[0]["incomplete"])
        self.assertNotIn("MATERIAL", rows[0]["incomplete"])

    def test_amortization_missing_is_error_unless_ignored(self) -> None:
        bad = self.call("GET", "/api/group/compare", f"version=2026-10&names={quote(NAME)}")
        self.assert_error(bad, 400, "償却費")
        self.assertIn(f"A-100（{NAME}）", bad.json()["error"])
        ok = self.compare(f"&names={quote(NAME)}&ignore_amortization=1")
        self.assertEqual(len(ok["rows"]), 4)

    def test_input_errors(self) -> None:
        self.assert_error(self.call("GET", "/api/group/compare", "version=2026-10"), 400, "names")
        self.assert_error(self.call("GET", "/api/group/compare", "version=2026-10&names=%5Bx"), 400, "names")
        self.assert_error(self.call("GET", "/api/group/compare", f"names={quote(NAME)}"), 400, "版")

    def test_default_calculator_still_refuses_amortization(self) -> None:
        self.assert_error(self.call("GET", "/api/group/products", f"version=2026-10&name={NAME}&head=AMORTIZATION"),
                          400, "償却費")


class ProductRule(unittest.TestCase):
    def test_kind_text_and_empty_kind(self) -> None:
        v = Version("t")
        v.add_items([Item("P1", "製品1", "製品"), Item("S1", "組立", "製品"), Item("C1", "部品", "部品")])
        v.add_bom_lines([BomLine("P1", None, "S1", 1), BomLine("S1", None, "C1", 2), BomLine("T9", None, "C1", 1)])
        s = _Structure(v)
        kind = Group.product_kind(v, s)
        self.assertEqual(kind, "製品")
        # 区分が空（品目に無い）なら構成の一番上の親を製品にする
        self.assertEqual(Group.product_codes(v, s, kind), ["P1", "S1", "T9"])
        self.assertEqual(Group.product_codes(v, s, None), ["T9"])

    def test_screen_files(self) -> None:
        index = static_response("/").body.decode("utf-8")
        self.assertIn('data-screen="group"', index)
        self.assertIn("group.js", index)
        self.assertEqual(static_response("/group.js").status, 200)
        self.assertIn('data-screen="trial"', index)
        self.assertIn("trial.js", index)
        self.assertEqual(static_response("/trial.js").status, 200)
        # 帯の並びは 製品群・試作品・比べる・製品から
        order = [index.index(f'data-screen="{s}"') for s in ("group", "trial", "compare", "product")]
        self.assertEqual(order, sorted(order))
        # 製品群は作る専用（費目・部品の検索は試作品の画面へ）
        group = index[index.index('id="screen-group"'):index.index('id="screen-trial"')]
        self.assertNotIn("group-head", group)
        self.assertNotIn("group-part", group)
        self.assertIn('data-screen="compare"', index)
        self.assertIn("compare.js", index)
        self.assertEqual(static_response("/compare.js").status, 200)


if __name__ == "__main__":
    unittest.main()
