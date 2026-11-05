"""グループの中身（/api/groups/contents）: 取り込んだ表を取り込んだまま返す。samples/ の CSV を取り込んだ DB で確かめる。"""

import unittest
from decimal import Decimal

from bomcost.app import static_response
from bomcost.model import BomLine, Item, Version
from test_group import SampleDbCase


class Contents(SampleDbCase):
    def contents(self, table: str, q: str = "", group: str = "2026-10") -> dict:
        return self.ok("GET", "/api/groups/contents", f"group={group}&table={table}&q={q}")

    def test_four_tables_as_imported(self) -> None:
        item = self.contents("item")
        self.assertEqual([c["label"] for c in item["columns"]],
                         ["品番", "名称", "記号・寸法", "区分", "状態", "子部品の有無"])
        self.assertEqual([r["cells"]["code"] for r in item["rows"]],
                         ["A-100", "V-110", "K-200", "B-200", "C-300", "F-600"])
        self.assertEqual(item["rows"][0]["cells"]["has_children"], "有")
        # マスタ登録だけの品目（X-610）は出さない
        self.assertNotIn("X-610", [r["cells"]["code"] for r in item["rows"]])
        lines = self.contents("bom_line")
        self.assertEqual([c["key"] for c in lines["columns"]], ["parent", "balloon", "child", "quantity"])
        self.assertEqual(lines["rows"][0]["cells"], {"parent": "A-100", "balloon": "1", "child": "B-200", "quantity": "2"})
        cost = self.contents("cost")
        self.assertEqual([c["key"] for c in cost["columns"]][:3], ["code", "mbom_code", "material"])
        b200 = next(r["cells"] for r in cost["rows"] if r["cells"]["code"] == "B-200")
        self.assertEqual((b200["material"], b200["direct"]), ("1600", None))
        volume = self.contents("volume")
        self.assertEqual([r["cells"] for r in volume["rows"]], [{"product": "K-200", "annual": "5000"}])
        self.assertTrue(next(c for c in volume["columns"] if c["key"] == "annual")["number"])
        self.assertEqual({t["id"]: (t["imported"], t["count"]) for t in item["tables"]},
                         {"item": (True, 6), "bom_line": (True, 5), "cost": (True, 5), "volume": (True, 1)})

    def test_master_mark(self) -> None:
        self.ok("PUT", "/api/manual/items", body={"group_id": "2026-10", "code": "B-200", "name": "部品B改",
                                                  "costs": {"INDIRECT": "7"}})
        self.ok("PUT", "/api/manual/volumes", body={"group_id": "2026-10", "product": "K-200", "annual": "10"})
        manual = self.store.load_manual("2026-10")
        manual.add_bom_lines([BomLine("A-100", None, "X-610", Decimal(1))])
        self.store.save_manual(manual, "2026-10")

        def marked(table: str, key: str) -> list[str]:
            return [r["cells"][key] for r in self.contents(table)["rows"] if r["master"]]

        self.assertEqual(marked("item", "code"), ["B-200"])
        self.assertEqual(marked("cost", "code"), ["B-200"])
        self.assertEqual(marked("volume", "product"), ["K-200"])
        self.assertEqual(marked("bom_line", "child"), ["B-200", "C-300"])  # 親品番 A-100 の行
        # 値は取り込んだまま（重ねた後の値でない）
        b200 = next(r["cells"] for r in self.contents("item")["rows"] if r["cells"]["code"] == "B-200")
        self.assertEqual(b200["name"], "部品B")
        self.assertEqual(self.contents("volume")["rows"][0]["cells"]["annual"], "5000")

    def test_filter_by_code_or_name(self) -> None:
        self.assertEqual([r["cells"]["code"] for r in self.contents("item", "部品")["rows"]], ["B-200", "C-300", "F-600"])
        self.assertEqual([r["cells"]["code"] for r in self.contents("item", "b-2")["rows"]], ["B-200"])
        # 構成・原価は品番の名称でも当たる
        lines = self.contents("bom_line", "部品C")
        self.assertEqual([(r["cells"]["parent"], r["cells"]["child"]) for r in lines["rows"]],
                         [("A-100", "C-300"), ("V-110", "C-300")])
        self.assertEqual(lines["matched"], 2)
        self.assertEqual([r["cells"]["code"] for r in self.contents("cost", "土台")["rows"]], ["A-100"])
        self.assertEqual(self.contents("volume", "ZZZ")["rows"], [])

    def test_rows_limit(self) -> None:
        bom = Version("big")
        bom.add_items(Item(f"P-{n:04d}", f"品{n}") for n in range(620))
        self.store.create_group("D-9", group_id="big", bom=bom, bom_tables=["item"])
        r = self.contents("item", group="big")
        self.assertEqual((len(r["rows"]), r["matched"], r["limit"]), (500, 620, 500))
        self.assertEqual(r["rows"][-1]["cells"]["code"], "P-0499")
        self.assertEqual(len(self.contents("item", "P-00", group="big")["rows"]), 100)

    def test_tables_not_imported_and_errors(self) -> None:
        group = self.new_group("D-2")
        r = self.contents("cost", group=group)
        self.assertEqual(r["rows"], [])
        self.assertEqual({t["id"]: t["imported"] for t in r["tables"]},
                         {"item": False, "bom_line": False, "cost": False, "volume": False})
        self.assert_error(self.call("GET", "/api/groups/contents", "group=NOPE&table=item"), 404, "NOPE")
        self.assert_error(self.call("GET", "/api/groups/contents", "table=item"), 400, "グループ")
        self.assert_error(self.call("GET", "/api/groups/contents", "group=2026-10&table=x"), 400, "table")
        # table を省けば品目
        self.assertEqual(self.ok("GET", "/api/groups/contents", "group=2026-10")["table"], "item")


class Screen(unittest.TestCase):
    def test_band_and_contents_wiring(self) -> None:
        index = static_response("/").body.decode("utf-8")
        screens = static_response("/screens.js").body.decode("utf-8")
        trial = static_response("/trial.js").body.decode("utf-8")
        # 帯は［グループ］［データ取込］［マスタ登録］の順で、品目照会は中身の中
        order = [index.index(f'data-screen="{s}"') for s in ("groups", "import", "manual")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('data-screen="lookup"', index)
        groups = index[index.index('id="screen-groups"'):]
        self.assertLess(groups.index('id="group-contents"'), groups.index('id="look-body"'))
        self.assertIn('"/api/groups/contents', screens.replace("`", '"'))
        # 中身を出している間は［絞り込む］が主ボタン。直す・消すは置かない
        draw = screens[screens.index("async function drawContents"):]
        self.assertEqual(draw.count('class: "primary"'), 1)
        self.assertNotIn("消す", draw)
        self.assertIn('createButton.classList.toggle("primary", !screenState.contents)', screens)
        # ②の簡易照会からの［品目照会で開く］は中身の照会へ
        self.assertIn("openContents(group, { code })", trial)


if __name__ == "__main__":
    unittest.main()
