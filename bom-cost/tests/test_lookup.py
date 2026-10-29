"""品目照会の API（/api/lookup）。samples/ の CSV を取り込んだ DB で確かめる。ソケットは開かない。"""

import unittest

from bomcost.app import static_response
from test_group import NAME, SampleDbCase
from test_screens import csv_file


class LookupCase(SampleDbCase):
    def look(self, q: str, version: str = "2026-10") -> dict:
        return self.ok("GET", "/api/lookup", f"version={version}&q={q}")

    def item(self, q: str, version: str = "2026-10") -> dict:
        found = self.look(q, version)
        self.assertEqual(found["candidates"], [])
        return found["item"]


class Basics(LookupCase):
    def test_bom_item(self) -> None:
        b = self.item("B-200")
        self.assertEqual((b["code"], b["name"], b["kind"], b["in_bom"], b["item_source"], b["is_product"]),
                         ("B-200", "部品B", "B", True, "bom", False))
        self.assertEqual(b["entries"], [{"dev_number": "2026-10", "revision": 1}])
        self.assertTrue(self.item("A-100")["is_product"])

    def test_registration_only_item(self) -> None:
        x = self.item("X-610")
        self.assertEqual((x["name"], x["in_bom"], x["item_source"], x["item_source_label"], x["entries"]),
                         ("試作品", False, "common", "共通の登録", []))
        self.assertEqual(x["used_in"], [])
        self.assertEqual(x["structure"], [])
        material = next(c for c in x["costs"] if c["head"] == "MATERIAL")
        self.assertEqual((material["value"], material["source"]), ("200", "common"))

    def test_name_search_and_candidates(self) -> None:
        # 名称の一部で1つに決まればその品
        self.assertEqual(self.item("新しい")["code"], "V-110")
        # 何件も当たれば候補だけ
        many = self.look("部品")
        self.assertIsNone(many["item"])
        self.assertEqual([c["code"] for c in many["candidates"]], ["B-200", "C-300", "F-600"])
        self.assertEqual(many["candidates"][0]["name"], "部品B")

    def test_input_errors(self) -> None:
        self.assert_error(self.call("GET", "/api/lookup", "q=B-200"), 400, "版")
        self.assert_error(self.call("GET", "/api/lookup", "version=2026-10"), 400, "q")
        self.assert_error(self.call("GET", "/api/lookup", "version=2026-10&q=ZZZ"), 404, "ZZZ")
        self.assert_error(self.call("GET", "/api/lookup", "version=nothing&q=B-200"), 404, "nothing")


class Costs(LookupCase):
    def test_sources_mixed_by_head(self) -> None:
        # 開発番号の登録: 材料費は BOM に隠れ、直接費は採る。共通の登録: 直接費は開発番号に隠れ、間接費は採る
        self.ok("PUT", "/api/manual/items", body={"dev_number": "2026-10", "code": "B-200", "name": "部品B",
                                                  "costs": {"MATERIAL": "9", "DIRECT": "30"}})
        self.ok("PUT", "/api/manual/items", body={"code": "B-200", "name": "部品B",
                                                  "costs": {"DIRECT": "99", "INDIRECT": "7"}})
        costs = {c["head"]: (c["value"], c["source"], c["source_label"]) for c in self.item("B-200")["costs"]}
        self.assertEqual(costs, {
            "MATERIAL": ("1600", "bom", "BOM"),
            "DIRECT": ("30", "dev", "開発番号の登録"),
            "INDIRECT": ("7", "common", "共通の登録"),
            "ADJUSTMENT": (None, None, "未登録"),
            "AMORTIZATION": (None, None, "未登録"),
            "MANUFACTURING": (None, None, "未登録"),
        })
        # 値は overlay_master と同じ
        overlaid = self.store.load_version("2026-10")
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


class History(LookupCase):
    def test_costs_across_revisions(self) -> None:
        self.ok("POST", "/api/import", body={"dev_number": "2026-10", "cost_only": True, "files": [
            csv_file("原価2.csv", "MBOM品番,材料費,直接費\nB-200,1700,40\n", table="cost",
                     columns={"mbom_code": "MBOM品番", "material": "材料費", "direct": "直接費"})]})
        revisions = self.item("B-200")["revisions"]
        self.assertEqual([(r["revision"], r["found"], r["costs"]["MATERIAL"], r["costs"]["DIRECT"]) for r in revisions],
                         [(1, True, "1600", None), (2, True, "1700", "40")])
        # 新しい版から引いても同じ並び
        newest = revisions[-1]["version_id"]
        self.assertEqual([r["revision"] for r in self.item("B-200", newest)["revisions"]], [1, 2])

    def test_changes_that_touch_the_item(self) -> None:
        self.change("A-100", kind="REPLACE", part="B-200", replacement="F-600", quantity="2")
        self.change("A-100", kind="REMOVE", part="C-300", quantity="1")
        self.change("K-200", kind="ADD", part="X-610", quantity="1")
        self.change("V-110", kind="SET_QUANTITY", part="F-600", quantity="2")

        def roles(code: str) -> list[tuple[str, str, str]]:
            return [(c["name"], c["product"], c["role"]) for c in self.item(code)["changes"]]

        self.assertEqual(roles("B-200"), [(NAME, "A-100", "置換元")])
        self.assertEqual(roles("F-600"), [(NAME, "A-100", "置換先"), (NAME, "V-110", "員数変更")])
        self.assertEqual(roles("C-300"), [(NAME, "A-100", "削除")])
        self.assertEqual(roles("X-610"), [(NAME, "K-200", "追加")])
        self.assertEqual(roles("K-200"), [])
        change = self.item("B-200")["changes"][0]
        self.assertEqual((change["kind"], change["replacement"], change["version_id"]), ("REPLACE", "F-600", "2026-10"))


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
