"""製品群の API（製品の一覧・部品の検索・検討名ごとの替える・足す）。samples/ の CSV を取り込んだ DB で確かめる。ソケットは開かない。"""

import unittest

from bomcost import BomLine, Item, Version
from bomcost.app import static_response
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

    def test_open_scenario_gets_or_creates(self) -> None:
        body = {"version_id": "2026-10", "name": NAME, "product": "A-100"}
        first = self.ok("POST", "/api/group/scenario", body=body)
        self.assertTrue(first["created"])
        again = self.ok("POST", "/api/group/scenario", body=body)
        self.assertEqual((again["created"], again["scenario_id"]), (False, first["scenario_id"]))
        # 開いた試算に、製品群からの変更が重なる
        self.assertEqual(self.change("A-100", kind="ADD", part="F-600")["scenario_id"], first["scenario_id"])


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


if __name__ == "__main__":
    unittest.main()
