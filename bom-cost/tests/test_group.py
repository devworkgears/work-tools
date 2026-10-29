"""製品群の API（製品の一覧・部品の検索・検討名ごとの替える・足す）。samples/ の CSV を取り込んだ DB で確かめる。ソケットは開かない。"""

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from bomcost import BomLine, Item, Version, overlay_manual
from bomcost.api import Api
from bomcost.app import static_response
from bomcost.demo import build_demo
from bomcost.store import Store
from bomcost import group
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

    def test_words_width_and_hyphen(self) -> None:
        rows = lambda q: [(x["product"], x["part"]) for x in self.parts("&q=A-100&part=" + quote(q))["rows"]]
        self.assertEqual(rows("部品"), [("A-100", "B-200"), ("A-100", "C-300")])
        # 空白で区切った語は全部を含む物だけ（全角の空白も区切り）
        self.assertEqual(rows("部品　ｂ"), [("A-100", "B-200")])
        self.assertEqual(rows("部品 B Z"), [])
        # 全角半角・ハイフンの有無は同じに扱う
        self.assertEqual(rows("Ｃ３００"), [("A-100", "C-300")])
        # 中ほどの一致も消さない。品番が語で始まるかの判定も全角・ハイフンを同じに扱う
        self.assertEqual(rows("00"), [("A-100", "B-200"), ("A-100", "C-300")])
        self.assertTrue(group._code_starts("C-300", "部品 ｃ３"))
        self.assertFalse(group._code_starts("B-200", "部品 ｃ３"))


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
                         [("A-100", "1", "土台"), ("C-300", "-3", "削除"), ("F-600", "3", "追加")])
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
                          ("K-200", "ADD", "追加 F-600 ×1", "3000", "3250", "250")])
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

    def test_costs_follow_registered_values_and_records_stay(self) -> None:
        sid = self.change("A-100", kind="REPLACE", part="B-200", replacement="X-610", quantity="2")["scenario_id"]
        self.change("K-200", kind="ADD", part="F-600", quantity="1")
        self.ok("PUT", "/api/manual/items", body={"code": "X-610", "name": "試作品", "costs": {"MATERIAL": "300"}})
        r = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")
        a = r["rows"][0]
        # 200 − 2×1,600 ＋ 2×300（記録した時は X-610 が 200 で −2,800）
        self.assertEqual((a["cost_before"], a["cost_after"], a["difference"]), ("200", "-2400", "-2600"))
        self.assertEqual((a["part_name"], a["replacements"]),
                         ("部品B", [{"code": "X-610", "quantity": "2", "name": "試作品", "kind": ""}]))
        self.assertEqual((r["difference"], a["sequence"], a["scenario_id"]), ("-2350", 1, sid))
        # 記録の値は書き換えない
        record = self.store.load_scenario(sid).history[0]
        self.assertEqual((str(record.cost_after), str(record.difference)), ("-2600", "-2800"))
        # 構成の行は区分も持つ（②の構成の表の列）
        tree = self.ok("GET", "/api/group/tree", "version=2026-10&product=A-100&by_balloon=1")
        self.assertEqual([(c["balloon"], c["code"], c["kind"]) for c in tree["children"]],
                         [("1", "B-200", "B"), ("2", "C-300", "B")])
        # 製品で絞れる
        only = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}&product=K-200")
        self.assertEqual(([x["product"] for x in only["rows"]], only["difference"]), (["K-200"], "250"))


class TrialHeadAndWords(SampleDbCase):
    """②の差は製品の行と同じ費目でそろえる・変更の語は 置換・削除・追加・員数変更。"""

    def test_changes_follow_requested_head_like_product_rows(self) -> None:
        self.change("A-100", kind="REPLACE", part="B-200", replacement="X-610", quantity="2", head="MANUFACTURING")
        self.change("K-200", kind="ADD", part="F-600", quantity="1", head="MANUFACTURING")
        # 費目を付けなければ今まで通り試算の費目（ここでは製造原価で、サンプルは未登録）
        plain = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")
        self.assertEqual([x["difference"] for x in plain["rows"]], [None, None])
        for head in ("MATERIAL", "MANUFACTURING"):
            changes = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}&head={head}")
            products = self.products(f"&name={NAME}&head={head}")
            by_product = {p["code"]: p["difference"] for p in products["products"]}
            for row in changes["rows"]:
                self.assertEqual(row["difference"], by_product[row["product"]], (head, row["product"]))
            self.assertEqual(products["difference"], {"MATERIAL": "-2550", "MANUFACTURING": None}[head])
            self.assertEqual(products["missing_count"], {"MATERIAL": 0, "MANUFACTURING": 3}[head])
        # 製品の行の差の合計は表に出した製品の差の合計（未登録の製品は除いて数える）
        self.ok("PUT", "/api/manual/items", body={"code": "K-200", "name": "既存品", "kind": "P",
                                                 "costs": {"MANUFACTURING": "100"}})
        self.ok("PUT", "/api/manual/items", body={"code": "F-600", "name": "部品F", "kind": "B",
                                                 "costs": {"MANUFACTURING": "40"}})
        products = self.products(f"&name={NAME}&head=MANUFACTURING")
        # K-200 は ＋40、V-110 は F-600 を含むので 0、A-100 だけ未登録
        self.assertEqual((products["difference"], products["missing_count"]), ("40", 1))
        k200 = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}&head=MANUFACTURING&product=K-200")
        self.assertEqual(k200["rows"][0]["difference"], "40")

    def test_place_quantity_change(self) -> None:
        r = self.change("A-100", kind="SET_QUANTITY", part="C-300", parent="A-100", balloon="2", quantity="1")
        self.assertEqual(r["summary"]["total"], "-800")  # 200 − 3×500 ＋ 1×500
        # 組立品の下の場所も、その親の使用数をかけて変える（V-110 の中の A-100 は1台に1つ）
        v = self.change("V-110", kind="SET_QUANTITY", part="C-300", parent="A-100", balloon="2", quantity="1")
        self.assertEqual(v["summary"]["total"], "-1050")  # −50 − 2×500
        rows = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}&product=A-100")["rows"]
        self.assertEqual([(x["kind"], x["kind_label"], x["text"], x["parent"], x["balloon"], x["quantity"]) for x in rows],
                         [("SET_QUANTITY", "員数変更", "員数変更 #2 C-300 → 1", "A-100", "2", "1")])
        # 同じ場所はもう置換・員数変更できない。場所は追加・削除には付けられない
        self.assert_error(self.call("POST", "/api/group/changes", body={
            "version_id": "2026-10", "name": NAME, "product": "A-100", "kind": "REPLACE", "part": "C-300",
            "parent": "A-100", "balloon": "2", "replacement": "F-600"}), 400, "もう置換した")
        self.assert_error(self.call("POST", "/api/group/changes", body={
            "version_id": "2026-10", "name": NAME, "product": "A-100", "kind": "ADD", "part": "C-300",
            "parent": "A-100"}), 400, "置換だけ")
        self.assert_error(self.call("POST", "/api/group/changes", body={
            "version_id": "2026-10", "name": NAME, "product": "A-100", "kind": "SET_QUANTITY", "part": "B-200",
            "parent": "A-100", "balloon": "9", "quantity": "1"}), 400, "構成に無い")

    def test_words_of_each_kind_and_compare_uses_current_costs(self) -> None:
        self.change("A-100", kind="REPLACE", part="B-200", replacement="X-610", quantity="2")
        self.change("A-100", kind="REMOVE", part="C-300", quantity="1")
        self.change("A-100", kind="ADD", part="F-600", quantity="1")
        self.change("A-100", kind="SET_QUANTITY", part="F-600", quantity="2")
        rows = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")["rows"]
        self.assertEqual([(x["kind_label"], x["text"]) for x in rows],
                         [("置換", "B-200 → X-610（×2）"), ("削除", "削除 C-300 ×1"), ("追加", "追加 F-600 ×1"),
                          ("員数変更", "員数変更 F-600 → 2")])
        # ③の変更の一覧の差も、記録した時点でなく今の登録値で当て直した値
        self.ok("PUT", "/api/manual/items", body={"code": "X-610", "name": "試作品", "costs": {"MATERIAL": "300"}})
        r = self.ok("GET", "/api/group/compare", f"version=2026-10&names={quote(NAME)}&q=A-100&ignore_amortization=1")
        changes = r["rows"][1]["changes"]
        self.assertEqual((changes[0]["kind_label"], changes[0]["difference"]), ("置換", "-2600"))
        self.assertEqual([c["text"] for c in changes][1:], ["削除 C-300 ×1", "追加 F-600 ×1", "員数変更 F-600 → 2"])


class PairViewData(SampleDbCase):
    """②の左右の表示に要る、変更の行の場所・代わりの部品（名称・区分）・員数がそろって返ること。"""

    def test_each_kind_lines_up_with_its_place(self) -> None:
        self.change("V-110", kind="REPLACE", part="B-200", parent="A-100", balloon="1",
                    replacements=[{"code": "X-610", "quantity": "1"}, {"code": "F-600", "quantity": "1"}])
        self.change("V-110", kind="SET_QUANTITY", part="F-600", parent="V-110", balloon="2", quantity="3")
        self.change("V-110", kind="REMOVE", part="C-300", quantity="1")
        self.change("V-110", kind="ADD", part="K-200", quantity="1")
        rows = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}&product=V-110")["rows"]
        self.assertEqual(
            [(x["kind"], x["parent"], x["balloon"], x["part"], x["part_name"], x["part_kind"], x["quantity"],
              [(r["code"], r["name"], r["kind"], r["quantity"]) for r in x["replacements"]]) for x in rows],
            [("REPLACE", "A-100", "1", "B-200", "部品B", "B", "1", [("X-610", "試作品", "", "1"), ("F-600", "部品F", "B", "1")]),
             ("SET_QUANTITY", "V-110", "2", "F-600", "部品F", "B", "3", []),
             ("REMOVE", None, None, "C-300", "部品C", "B", "1", []),
             ("ADD", None, None, "K-200", "既存品", "P", "1", [])])
        # 行を合わせる相手（版の構成の場所）は tree の by_balloon の行の 親・風船番号・品番 と同じ形
        tree = self.ok("GET", "/api/group/tree", "version=2026-10&product=V-110&code=A-100&by_balloon=1")
        self.assertIn(("A-100", "1", "B-200"), [(c["parent"], c["balloon"], c["code"]) for c in tree["children"]])


class UndoChange(SampleDbCase):
    def undo(self, sid: int, sequence: object) -> object:
        return self.call("POST", "/api/group/changes/undo", body={"scenario_id": sid, "sequence": sequence})

    def test_removes_one_operation_from_the_middle(self) -> None:
        sid = self.change("A-100", kind="ADD", part="F-600", quantity="1")["scenario_id"]
        self.change("A-100", kind="REPLACE", part="B-200", parent="A-100", balloon="1",
                    replacements=[{"code": "X-610", "quantity": "1"}, {"code": "F-600", "quantity": "1"}])
        self.change("A-100", kind="REPLACE", part="C-300", parent="A-100", balloon="2",
                    replacements=[{"code": "X-610", "quantity": "1"}, {"code": "F-600", "quantity": "2"}])
        self.change("A-100", kind="ADD", part="F-600", quantity="2")
        self.assertEqual([(r.sequence, r.batch) for r in self.store.load_scenario(sid).history],
                         [(1, None), (2, 2), (3, 2), (4, 4), (5, 4), (6, None)])
        # まとまりの途中の行からは取り消せない
        self.assert_error(self.undo(sid, 3), 400, "取り消す変更")
        r = self.undo(sid, 2)
        self.assertEqual(r.status, 200, r.body.decode("utf-8"))
        self.assertEqual(r.json()["removed"], 2)
        history = self.store.load_scenario(sid).history
        self.assertEqual([(h.sequence, h.batch, h.change.part, h.change.replacement) for h in history],
                         [(1, None, "F-600", None), (2, 2, "C-300", "X-610"), (3, 2, "C-300", "F-600"),
                          (4, None, "F-600", None)])
        # 残りの原価は今の構成で出し直す: 200 ＋ 250 − 3×500 ＋ 200 ＋ 2×250 ＋ 2×250
        rows = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")["rows"]
        self.assertEqual([x["sequence"] for x in rows], [1, 2, 4])
        self.assertEqual(self.by_code()["A-100"]["after"], "150")
        self.assert_error(self.undo(sid, 9), 400, "取り消す変更")
        self.assert_error(self.undo(sid, "x"), 400, "整数")
        self.assert_error(self.undo(999, 1), 404, "999")


class BulkReplace(SampleDbCase):
    def bulk(self, items: list[dict], **extra: str) -> object:
        return self.call("POST", "/api/group/changes/bulk",
                         body={"version_id": "2026-10", "name": NAME, "items": items, **extra})

    def test_one_place_replace_per_location_across_products(self) -> None:
        r = self.bulk([{"product": "A-100", "part": "B-200"}, {"product": "V-110", "part": "B-200"}],
                      replacement="X-610")
        self.assertEqual(r.status, 200, r.body.decode("utf-8"))
        self.assertEqual((r.json()["count"], r.json()["products"]), (2, ["A-100", "V-110"]))
        by_base = {s.base: self.store.load_scenario(s.scenario_id) for s in self.store.list_scenarios()}
        # 員数が空なら場所の員数のまま。V-110 の B-200 は組立品 A-100 の下の場所
        self.assertEqual([(c.part, c.parent, c.balloon, c.replacement, str(c.quantity)) for c in by_base["A-100"].changes],
                         [("B-200", "A-100", "1", "X-610", "2")])
        self.assertEqual([(c.parent, c.balloon) for c in by_base["V-110"].changes], [("A-100", "1")])
        rows = self.by_code()
        self.assertEqual((rows["A-100"]["difference"], rows["V-110"]["difference"]), ("-2800", "-2800"))
        # 1か所ずつ今の置換と同じ形で一覧に出る
        changes = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")["rows"]
        self.assertEqual([x["text"] for x in changes], ["#1 B-200 → X-610 ×2", "#1 B-200 → X-610 ×2"])

    def test_every_location_with_given_quantity(self) -> None:
        r = self.bulk([{"product": "V-110", "part": "C-300"}], replacement="F-600", quantity="1")
        self.assertEqual(r.status, 200, r.body.decode("utf-8"))
        changes = self.store.load_scenario(self.store.list_scenarios()[0].scenario_id).changes
        self.assertEqual([(c.parent, c.balloon, str(c.quantity)) for c in changes], [("V-110", "3", "1"), ("A-100", "2", "1")])

    def test_all_or_nothing(self) -> None:
        sid = self.change("A-100", kind="ADD", part="F-600")["scenario_id"]
        # V-110 の C-300 は員数 −1 の場所なので、員数が空だと置換できない
        bad = self.bulk([{"product": "A-100", "part": "B-200"}, {"product": "V-110", "part": "C-300"}],
                        replacement="X-610")
        self.assert_error(bad, 400, "正の数")
        self.assertEqual([s.scenario_id for s in self.store.list_scenarios()], [sid])
        self.assertEqual(len(self.store.load_scenario(sid).history), 1)
        self.assert_error(self.bulk([{"product": "A-100", "part": "B-200"}], replacement="NOPE"), 400, "NOPE")
        self.assert_error(self.bulk([{"product": "K-200", "part": "B-200"}], replacement="X-610"), 400, "K-200")
        # 同じ場所を2回は置換できない（今の置換と同じ）
        twice = self.bulk([{"product": "V-110", "part": "B-200"}, {"product": "V-110", "part": "B-200"}],
                          replacement="X-610")
        self.assert_error(twice, 400, "もう置換した")
        self.assertEqual(len(self.store.list_scenarios()), 1)
        self.assert_error(self.bulk([], replacement="X-610"), 400, "items")
        self.assert_error(self.bulk([{"product": "A-100", "part": "B-200"}]), 400, "代わりの品番")

    def test_products_saved_in_one_write(self) -> None:
        # 2つ目の製品の書き込みで落ちたら、1つ目の製品の試算も残らない
        write = self.store._write_scenario
        calls = []

        def failing(scenario):
            calls.append(scenario.base)
            if len(calls) == 2:
                raise RuntimeError("書けない")
            return write(scenario)

        self.store._write_scenario = failing
        try:
            r = self.bulk([{"product": "A-100", "part": "B-200"}, {"product": "V-110", "part": "B-200"}],
                          replacement="X-610")
        finally:
            del self.store._write_scenario
        self.assertEqual(r.status, 500)
        self.assertEqual(calls, ["A-100", "V-110"])
        self.assertEqual(self.store.list_scenarios(), [])


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

    def test_differences_changes_and_summary(self) -> None:
        names = quote(json.dumps([NAME, "別の検討", "無い検討"]))
        r = self.compare(f"&names={names}&ignore_amortization=1")
        trial, other = r["rows"][1], r["rows"][2]
        self.assertNotIn("differences", r["rows"][0])
        self.assertEqual((trial["differences"]["MATERIAL"], other["differences"]["MATERIAL"]), ("-2800", "250"))
        # 未登録（片方でも）と無視した償却費は差も None で、0 と分ける
        self.assertIsNone(trial["differences"]["DIRECT"])
        self.assertIsNone(trial["differences"]["AMORTIZATION"])
        self.assertEqual([(c["kind"], c["text"]) for c in trial["changes"]], [("REPLACE", "B-200 → X-610（×2）")])
        self.assertEqual(other["changes"][0]["difference"], "250")
        s = r["summary"]
        self.assertEqual(list(s), [NAME, "別の検討", "無い検討"])
        self.assertEqual(s[NAME]["products"], 1)
        self.assertEqual(s[NAME]["heads"]["MATERIAL"],
                         {"difference": "-2800", "down": 1, "up": 0, "same": 0, "missing": 0})
        self.assertEqual(s["別の検討"]["heads"]["MATERIAL"]["up"], 1)
        self.assertEqual(s[NAME]["heads"]["DIRECT"],
                         {"difference": "0", "down": 0, "up": 0, "same": 0, "missing": 1})
        self.assertEqual(s["無い検討"]["products"], 0)

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


class CompareTable(SampleDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.change("A-100", kind="REPLACE", part="B-200", replacement="X-610", quantity="2")
        self.ok("POST", "/api/group/changes", body={"version_id": "2026-10", "name": "別の検討", "product": "A-100",
                                                     "kind": "ADD", "part": "F-600", "quantity": "1"})

    def test_rows_per_product_with_trial_and_totals(self) -> None:
        names = quote(json.dumps([NAME, "別の検討"]))
        r = self.ok("GET", "/api/group/compare/table", f"version=2026-10&names={names}&ignore_amortization=1")
        self.assertEqual([h["id"] for h in r["heads"]], Compare.HEADS)
        # 試算がある製品だけ並べ、無い製品は数だけ
        self.assertEqual([p["product"] for p in r["products"]], ["A-100"])
        self.assertEqual(r["without_trial"], 2)
        a = r["products"][0]
        self.assertEqual((a["product_name"], a["current"]["MATERIAL"]), ("土台の製品", "200"))
        trial, other = a["trials"]
        self.assertEqual((trial["name"], trial["after"]["MATERIAL"], trial["difference"]["MATERIAL"],
                          trial["percent"]["MATERIAL"]), (NAME, "-2600", "-2800", "-1400.0"))
        self.assertEqual(other["percent"]["MATERIAL"], "125.0")
        # 未登録は値も差%も None
        self.assertEqual((a["current"]["DIRECT"], trial["percent"]["DIRECT"]), (None, None))
        t = r["totals"][NAME]
        self.assertEqual(t["MATERIAL"], {"current": "200", "after": "-2600", "difference": "-2800",
                                         "percent": "-1400.0", "excluded": 0})
        self.assertEqual(t["DIRECT"], {"current": None, "after": None, "difference": None, "percent": None,
                                       "excluded": 1})

    def test_totals_skip_products_with_missing_values(self) -> None:
        products = [
            {"current": {h.name: "100" for h in group.COMPARE_HEADS},
             "trials": [{"name": "n", "after": {h.name: "90" for h in group.COMPARE_HEADS}}]},
            {"current": {h.name: "300" for h in group.COMPARE_HEADS},
             "trials": [{"name": "n", "after": {**{h.name: "330" for h in group.COMPARE_HEADS}, "DIRECT": None}}]},
        ]
        t = group._table_totals("n", products)
        self.assertEqual(t["MATERIAL"], {"current": "400", "after": "420", "difference": "20", "percent": "5.0",
                                         "excluded": 0})
        self.assertEqual(t["DIRECT"], {"current": "100", "after": "90", "difference": "-10", "percent": "-10.0",
                                       "excluded": 1})
        self.assertIsNone(group._percent("5", "0"))
        self.assertEqual(group._percent("1", "3"), "33.3")
        self.assertEqual(group._percent("-1", "8"), "-12.5")

    def test_same_errors_as_compare(self) -> None:
        self.assert_error(self.call("GET", "/api/group/compare/table", "version=2026-10"), 400, "names")
        self.assert_error(self.call("GET", "/api/group/compare/table", f"version=2026-10&names={quote(NAME)}"),
                          400, "償却費")


class ChildrenIndex(unittest.TestCase):
    def test_children_follow_added_lines(self) -> None:
        v = Version("t")
        v.add_bom_lines([BomLine("P1", "1", "C1", 1), BomLine("P1", "2", "C2", 2), BomLine("P1", "3", "C1", 3)])
        self.assertEqual([(x.child, str(x.quantity)) for x in v.children("P1")], [("C1", "4"), ("C2", "2")])
        self.assertEqual([x.balloon for x in v.children("P1", by_balloon=True)], ["1", "2", "3"])
        self.assertEqual(v.children("C1"), ())
        # 一度引いた後に足した行も出る（直接 _lines に足す overlay_manual も同じ）
        v.add_bom_lines([BomLine("P1", "4", "C3", 1), BomLine("C1", None, "C9", 5)])
        self.assertEqual([x.child for x in v.children("P1")], ["C1", "C2", "C3"])
        self.assertEqual([x.child for x in v.children("C1")], ["C9"])
        layer = Version("m")
        layer.add_bom_lines([BomLine("Q1", None, "C1", 1)])
        merged = overlay_manual(v, layer)
        self.assertEqual([x.child for x in merged.children("Q1")], ["C1"])
        self.assertEqual([x.child for x in merged.children("P1")], ["C1", "C2", "C3"])


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
        # 帯の並びは 共通（取込・マスタ）→ 手順①②③ → その他（製品から・型費配賦・積上げ見積）
        order = [index.index(f'data-screen="{s}"')
                 for s in ("import", "manual", "group", "trial", "compare", "product", "part", "change")]
        self.assertEqual(order, sorted(order))
        more = index[index.index('id="screen-more"'):index.index("</details>")]
        for s in ("product", "part", "change"):
            self.assertIn(f'data-screen="{s}"', more)
        # 製品群は作る専用（費目・部品の検索は試作品の画面へ）
        group = index[index.index('id="screen-group"'):index.index('id="screen-trial"')]
        self.assertNotIn("group-head", group)
        self.assertNotIn("group-part", group)
        self.assertIn('data-screen="compare"', index)
        self.assertIn("compare.js", index)
        self.assertEqual(static_response("/compare.js").status, 200)

    def test_busy_hover_and_table_wiring(self) -> None:
        app = static_response("/app.js").body.decode("utf-8")
        css = static_response("/style.css").body.decode("utf-8")
        screens = static_response("/screens.js").body.decode("utf-8")
        compare = static_response("/compare.js").body.decode("utf-8")
        # 待ちの表示は guarded の中で出して finally で戻す
        guarded = app[app.index("async function guarded"):]
        self.assertLess(guarded.index("setBusy(1)"), guarded.index("finally"))
        self.assertIn("setBusy(-1)", guarded[guarded.index("finally"):])
        for name in ("busy-bar", "busy-wait", "busy-button"):
            self.assertIn(name, app)
            self.assertIn(name, css)
        self.assertIn("function codeTitle", screens)
        self.assertIn("/api/group/compare/table", compare)
        self.assertIn('button("table", "表")', compare)


if __name__ == "__main__":
    unittest.main()
