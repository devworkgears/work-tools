"""品目照会の API（/api/lookup）。samples/ の CSV を取り込んだ DB で確かめる。ソケットは開かない。"""

import unittest
from decimal import Decimal

from bomcost.app import static_response
from bomcost.group import Group, _Structure
from bomcost.lookup import Lookup, _used_in
from bomcost.model import BomLine, Item, Version
from test_group import SampleDbCase


class LookupCase(SampleDbCase):
    def look(self, q: str, version: str = "2026-10") -> dict:
        return self.ok("GET", "/api/lookup", f"group={version}&q={q}")

    def item(self, q: str, version: str = "2026-10") -> dict:
        found = self.look(q, version)
        self.assertEqual(found["candidates"], [])
        return found["item"]


class Basics(LookupCase):
    def test_bom_item(self) -> None:
        b = self.item("B-200")
        self.assertEqual((b["code"], b["name"], b["kind"], b["in_bom"], b["item_source"], b["is_product"]),
                         ("B-200", "部品B", "B", True, "bom", False))
        self.assertTrue(self.item("A-100")["is_product"])

    def test_registration_only_item(self) -> None:
        x = self.item("X-610")
        self.assertEqual((x["name"], x["in_bom"], x["item_source"], x["item_source_label"]),
                         ("試作品", False, "master", "マスタ登録"))
        self.assertEqual(x["used_in"], [])
        self.assertEqual(x["structure"], [])
        material = next(c for c in x["costs"] if c["head"] == "MATERIAL")
        self.assertEqual((material["value"], material["source"]), ("200", "master"))

    def test_name_search_and_candidates(self) -> None:
        # 名称の一部で1つに決まればその品
        self.assertEqual(self.item("新しい")["code"], "V-110")
        # 何件も当たれば候補だけ
        many = self.look("部品")
        self.assertIsNone(many["item"])
        self.assertEqual([c["code"] for c in many["candidates"]], ["B-200", "C-300", "F-600"])
        self.assertEqual(many["candidates"][0]["name"], "部品B")

    def test_input_errors(self) -> None:
        self.assert_error(self.call("GET", "/api/lookup", "q=B-200"), 400, "グループ")
        self.assert_error(self.call("GET", "/api/lookup", "group=2026-10"), 400, "q")
        self.assert_error(self.call("GET", "/api/lookup", "group=2026-10&q=ZZZ"), 404, "ZZZ")
        self.assert_error(self.call("GET", "/api/lookup", "group=nothing&q=B-200"), 404, "nothing")


class Costs(LookupCase):
    def test_master_over_bom_by_head(self) -> None:
        # マスタ登録に値のある費目はマスタ登録、空の費目は BOM
        self.ok("PUT", "/api/manual/items", body={"group_id": "2026-10", "code": "B-200", "name": "部品B",
                                                  "costs": {"MATERIAL": "9", "INDIRECT": "7"}})
        costs = {c["head"]: (c["value"], c["source"], c["source_label"]) for c in self.item("B-200")["costs"]}
        self.assertEqual({k: costs[k] for k in ("MATERIAL", "DIRECT", "INDIRECT", "ADJUSTMENT")}, {
            "MATERIAL": ("9", "master", "マスタ登録"),
            "DIRECT": (None, None, "未登録"),
            "INDIRECT": ("7", "master", "マスタ登録"),
            "ADJUSTMENT": (None, None, "未登録"),
        })
        material = next(c for c in self.item("C-300")["costs"] if c["head"] == "MATERIAL")
        self.assertEqual(material["source"], "bom")  # 登録の無い品は BOM のまま
        # 値は overlay_master と同じ
        overlaid = self.store.load_group("2026-10")
        self.assertEqual({h: v for h, (v, _, _) in costs.items()},
                         {h.name: None if (x := overlaid.cost_value("B-200", h)) is None else str(x)
                          for h in overlaid.cost_row("B-200").values})


class UsedIn(LookupCase):
    def test_products_and_direct_parents(self) -> None:
        used = {u["product"]: u for u in self.item("C-300")["used_in"]}
        self.assertEqual(sorted(used), ["A-100", "V-110"])
        self.assertEqual(used["A-100"]["quantity_per_unit"], "3")
        self.assertEqual([(p["code"], p["quantity"]) for p in used["A-100"]["parents"]], [("A-100", "3")])
        # V-110 は A-100 の下の 3 と、V-110 の直下で抜く -1
        self.assertEqual(used["V-110"]["quantity_per_unit"], "2")
        self.assertEqual(sorted((p["code"], p["quantity"]) for p in used["V-110"]["parents"]),
                         [("A-100", "3"), ("V-110", "-1")])

    def test_structure_below(self) -> None:
        rows = self.item("V-110")["structure"]
        self.assertEqual([(r["level"], r["code"], r["quantity"], r["usage"], r["has_children"]) for r in rows], [
            (1, "A-100", "1", "1", True),
            (2, "B-200", "2", "2", False),
            (2, "C-300", "3", "3", False),
            (1, "F-600", "1", "1", False),
            (1, "C-300", "-1", "-1", False),
        ])
        self.assertEqual(self.item("B-200")["structure"], [])


class Summary(LookupCase):
    """②の簡易照会の枠の追加分（brief=1&summary=1）。"""

    def summary(self, code: str) -> dict:
        found = self.look(f"{code}&brief=1&summary=1")
        return found["item"]["summary"]

    def test_part_parents_and_counts(self) -> None:
        s = self.summary("C-300")
        self.assertEqual([(p["code"], p["name"], p["quantity"]) for p in s["parents"]],
                         [("A-100", "土台の製品", "3"), ("V-110", "新しい製品", "-1")])
        self.assertEqual((s["product_count"], s["child_count"], s["is_product"], s["annual_volume"]), (2, 0, False, None))
        self.assertEqual(s["calculated"], self.item("C-300")["calculated"])
        self.assertEqual(len(s["calculated"]), 8)
        self.assertEqual(s["imported"], {"value": None, "difference": None})

    def test_product_children_and_volume(self) -> None:
        a = self.summary("A-100")
        self.assertEqual([(p["code"], p["quantity"]) for p in a["parents"]], [("V-110", "1")])
        self.assertEqual((a["product_count"], a["child_count"], a["is_product"], a["annual_volume"]), (1, 2, True, None))
        k = self.summary("K-200")
        self.assertEqual((k["parents"], k["product_count"], k["child_count"], k["annual_volume"]), ([], 0, 0, "5000"))

    def test_imported_manufacturing_and_difference(self) -> None:
        self.ok("PUT", "/api/manual/items", body={"group_id": "2026-10", "code": "B-200", "name": "部品B", "costs": {"MANUFACTURING": "1700"}})
        s = self.summary("B-200")
        computed = next(c["value"] for c in s["calculated"] if c["head"] == "MANUFACTURING")
        self.assertEqual(s["imported"]["value"], "1700")
        self.assertEqual(Decimal(s["imported"]["difference"]), Decimal(computed) - 1700)

    def test_brief_without_summary_is_unchanged(self) -> None:
        self.assertNotIn("summary", self.look("B-200&brief=1")["item"])
        self.assertNotIn("summary", self.item("B-200"))

    def test_product_count_matches_used_in_on_samples(self) -> None:
        for code in ("A-100", "V-110", "K-200", "B-200", "C-300", "F-600", "X-610"):
            with self.subTest(code=code):
                self.assertEqual(self.summary(code)["product_count"], len(self.item(code)["used_in"]))

    def test_product_count_matches_used_in_on_deeper_structure(self) -> None:
        # 組立品の共有・製品の下の製品・区分が製品でない一番上・区分が空の一番上を混ぜる
        v = Version("t")
        v.add_items([Item("P1", "", "P"), Item("P2", "", "P"), Item("P3", "", "P"), Item("T1", "", "Z"),
                     Item("S1", "", "B"), Item("S2", "", "B"), Item("L1", "", "B")])
        v.add_bom_lines([BomLine("P1", "1", "S1", 2), BomLine("P1", "2", "S2", 1), BomLine("S1", "1", "L1", 3),
                     BomLine("S2", "1", "S1", 1), BomLine("P2", "1", "P1", 1), BomLine("P3", "1", "S2", 4),
                     BomLine("T1", "1", "L1", 1), BomLine("E1", "1", "S2", 1)])
        look = Lookup(self.api)
        structure = _Structure(v)
        kind = Group.product_kind(v, structure)
        costs = [{"head": "MANUFACTURING", "value": None}]
        counts = {}
        for code in ("P1", "P2", "P3", "T1", "E1", "S1", "S2", "L1"):
            counts[code] = look.summary(v, code, costs)["product_count"]
            with self.subTest(code=code):
                self.assertEqual(counts[code], len(_used_in(v, structure, code, kind)))
        self.assertEqual(counts["L1"], 4)  # P1・P2・P3・E1（T1 は区分が製品でない）


class History(LookupCase):
    def test_changes_that_touch_the_item(self) -> None:
        self.change("A-100", kind="REPLACE", part="B-200", replacement="F-600", quantity="2")
        self.change("A-100", kind="REMOVE", part="C-300", quantity="1")
        self.change("K-200", kind="ADD", part="X-610", quantity="1")
        self.change("V-110", kind="SET_QUANTITY", part="F-600", quantity="2")

        def roles(code: str) -> list[tuple[str, str, str]]:
            return [(c["product"], c["role"]) for c in self.item(code)["changes"]]

        self.assertEqual(roles("B-200"), [("A-100", "置換元")])
        self.assertEqual(roles("F-600"), [("A-100", "置換先"), ("V-110", "員数変更")])
        self.assertEqual(roles("C-300"), [("A-100", "削除")])
        self.assertEqual(roles("X-610"), [("K-200", "追加")])
        self.assertEqual(roles("K-200"), [])
        change = self.item("B-200")["changes"][0]
        self.assertEqual((change["kind"], change["replacement"], change["group_id"]), ("REPLACE", "F-600", "2026-10"))


class Screen(unittest.TestCase):
    def test_screen_files(self) -> None:
        index = static_response("/").body.decode("utf-8")
        self.assertIn('data-screen="lookup"', index)
        # 帯の［マスタ登録］の隣
        self.assertLess(index.index('data-screen="manual"'), index.index('data-screen="lookup"'))
        self.assertLess(index.index('data-screen="lookup"'), index.index('data-screen="group"'))
        self.assertIn("lookup.js", index)
        self.assertEqual(static_response("/lookup.js").status, 200)


if __name__ == "__main__":
    unittest.main()
