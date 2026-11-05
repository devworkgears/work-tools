"""グループの新しく作る・流用・取り込み（その表がまだ無いグループだけ）・取り込みの確認の差。データはすべて架空。"""

import unittest
from decimal import Decimal

from bomcost.app import static_response
from test_screens import EmptyDbCase, csv_file

# 製品 P-1 = #1 組立 S-1 ×1（S-1 = #1 B-2 ×3）・#2 B-1 ×2
ITEMS = "品番,品名,区分\nP-1,製品,P\nS-1,組立,B\nB-1,部品1,B\nB-2,部品2,B\n"
BOM = "親品番,風船番号,子品番,員数\nP-1,1,S-1,1\nP-1,2,B-1,2\nS-1,1,B-2,3\n"
COSTS = "MBOM品番,材料費\nB-1,10\nB-2,20\n"
VOLUMES = "製品品番,年間台数\nP-1,100\n"
MAPS = {
    "item": {"code": "品番", "name": "品名", "kind": "区分"},
    "bom_line": {"parent": "親品番", "balloon": "風船番号", "child": "子品番", "quantity": "員数"},
    "cost": {"mbom_code": "MBOM品番", "material": "材料費"},
    "volume": {"product": "製品品番", "annual": "年間台数"},
}
ALL = {"item": ITEMS, "bom_line": BOM, "cost": COSTS, "volume": VOLUMES}


class GroupCase(EmptyDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.new_group("D-1", dev_name="ユニット", note="元")
        self.ok("POST", "/api/import", body=self.files(self.source, ALL))

    @staticmethod
    def files(group: str, tables: dict[str, str]) -> dict:
        return {"group_id": group, "files": [csv_file(f"{t}.csv", text, table=t, columns=MAPS[t])
                                             for t, text in tables.items()]}

    def copy(self, tables: list[str], master: bool = False, changes: bool = False, note: str = "写し") -> dict:
        return self.ok("POST", "/api/groups", body={"from": self.source, "note": note,
                                                    "copy": {"tables": tables, "master": master, "changes": changes}})

    def change(self, group: str, **change: str) -> dict:
        return self.ok("POST", "/api/group/changes", body={"group_id": group, "product": "P-1", **change})

    def material(self, group: str) -> tuple[str, str]:
        """P-1 の材料費の今と変更後。"""
        row = next(p for p in self.ok("GET", "/api/group/products", f"group={group}&costs=1&head=MATERIAL")["products"]
                   if p["code"] == "P-1")
        return row["current"], row["after"]

    def batches(self, group: str) -> list[str]:
        return [r["text"] for r in self.ok("GET", "/api/group/changes", f"group={group}")["rows"]]


class Reimport(GroupCase):
    def test_table_already_in_is_refused(self) -> None:
        for table in ALL:
            with self.subTest(table=table):
                self.assert_error(self.call("POST", "/api/import/check", body=self.files(self.source, {table: ALL[table]})),
                                  400, "新しく作る")
                self.assert_error(self.call("POST", "/api/import", body=self.files(self.source, {table: ALL[table]})),
                                  400, "もうこのグループに入っている")
        info = self.ok("GET", "/api/groups/info", f"group={self.source}")
        self.assertEqual(info["bom_tables"], ["item", "bom_line", "cost", "volume"])

    def test_only_left_out_table_is_imported_with_diff(self) -> None:
        self.change(self.source, kind="SET_HEAD", part="P-1", target_head="DIRECT", quantity="5")
        made = self.copy(["item", "bom_line", "cost"], master=True, changes=True)
        group = made["group_id"]
        self.assertEqual((made["copied_from"], made["bom_tables"], made["scenario_count"], made["dropped"]),
                         (self.source, ["item", "bom_line", "cost"], 1, []))
        # もう写した表は取り込めず、外した年間台数だけ入る
        self.assert_error(self.call("POST", "/api/import", body=self.files(group, {"cost": COSTS})), 400, "原価")
        volumes = "製品品番,年間台数\nP-1,150\nP-9,5\n"
        check = self.ok("POST", "/api/import/check", body=self.files(group, {"volume": volumes}))
        self.assertEqual(check["diff"], [{"table": "volume", "label": "年間台数", "added": 1, "changed": 1, "removed": 0}])
        self.assertEqual((check["dropped"], check["kept_changes"], check["unused_master"]),
                         ([], 1, {"count": 0, "codes": []}))
        # 確認では何も入らない
        self.assertEqual(self.ok("GET", "/api/groups/info", f"group={group}")["bom_tables"], ["item", "bom_line", "cost"])
        done = self.call("POST", "/api/import", body=self.files(group, {"volume": volumes}))
        self.assertEqual(done.status, 201, done.body.decode("utf-8"))
        self.assertEqual(self.ok("GET", "/api/groups/info", f"group={group}")["bom_tables"],
                         ["item", "bom_line", "cost", "volume"])
        self.assertEqual(self.material(group), ("80", "80"))  # 3×20 ＋ 2×10
        self.assertEqual(self.batches(group), ["費目変更 直接費 → 5"])
        self.assert_error(self.call("POST", "/api/import", body=self.files(group, {"volume": volumes})), 400, "年間台数")

    def test_diff_counts_by_table_against_source(self) -> None:
        group = self.copy([])["group_id"]
        items = "品番,品名,区分\nP-1,製品,P\nS-1,組立 改,B\nB-1,部品1,B\nB-3,部品3,B\n"
        bom = "親品番,風船番号,子品番,員数\nP-1,1,S-1,1\nP-1,2,B-1,4\nS-1,1,B-3,3\n"
        check = self.ok("POST", "/api/import/check", body=self.files(group, {"item": items, "bom_line": bom}))
        self.assertEqual([(d["table"], d["added"], d["changed"], d["removed"]) for d in check["diff"]],
                         [("item", 1, 1, 1), ("bom_line", 1, 1, 1)])

    def test_new_group_without_source_compares_with_nothing(self) -> None:
        group = self.new_group("D-2")
        check = self.ok("POST", "/api/import/check", body=self.files(group, {"item": ITEMS}))
        self.assertEqual(check["diff"], [{"table": "item", "label": "部品マスタ（品目）", "added": 4, "changed": 0, "removed": 0}])


class CopiedChanges(GroupCase):
    def setUp(self) -> None:
        super().setUp()
        self.change(self.source, kind="REPLACE", part="B-1", replacement="B-2", quantity="2")
        self.change(self.source, kind="REPLACE", part="B-2", parent="S-1", balloon="1", replacement="B-1",
                    quantity="3")
        self.before = self.batches(self.source)
        self.group = self.copy(["item", "cost", "volume"], changes=True)["group_id"]

    def import_bom(self, bom: str) -> tuple[dict, dict]:
        body = self.files(self.group, {"bom_line": bom})
        check = self.ok("POST", "/api/import/check", body=body)
        return check, self.ok("POST", "/api/import", body=body)

    def test_change_whose_place_is_gone_is_not_copied(self) -> None:
        check, done = self.import_bom("親品番,風船番号,子品番,員数\nP-1,1,S-1,1\nP-1,2,B-1,2\nS-1,2,B-2,3\n")
        self.assertEqual([(d["product"], d["reason"]) for d in check["dropped"]],
                         [("P-1", "S-1 の #1 B-2 はグループの構成に無い")])
        self.assertIn("B-2", check["dropped"][0]["text"])
        self.assertEqual((check["kept_changes"], done["dropped"]), (1, check["dropped"]))
        # 外した変更は計算に入らない（B-1 ×2 → B-2 だけ: 3×20 ＋ 2×20）
        self.assertEqual(len(self.batches(self.group)), 1)
        self.assertEqual(self.material(self.group), ("80", "100"))
        # 流用元には残る
        self.assertEqual(self.batches(self.source), self.before)

    def test_part_and_place_inside_it_are_not_both_copied(self) -> None:
        # 新しい構成では S-1 が B-1 の中: B-1 を置換したので、その中の S-1 の場所は変えられない
        check, _ = self.import_bom("親品番,風船番号,子品番,員数\nP-1,2,B-1,2\nB-1,1,S-1,1\nS-1,1,B-2,3\n")
        self.assertEqual(len(check["dropped"]), 1)
        self.assertIn("親子に重なる", check["dropped"][0]["reason"])
        self.assertEqual(len(self.batches(self.group)), 1)
        self.assertEqual(self.batches(self.source), self.before)

    def test_copy_with_structure_refits_at_once(self) -> None:
        # 構成ごと写せば写す時に当て直す（全部効くので外れる物は無い）
        made = self.copy(["item", "bom_line", "cost", "volume"], changes=True, note="構成ごと")
        self.assertEqual((made["scenario_count"], made["dropped"]), (1, []))
        self.assertEqual(self.batches(made["group_id"]), self.before)
        self.assertEqual(self.material(made["group_id"]), self.material(self.source))


class Master(GroupCase):
    def register(self, group: str, code: str, material: str) -> None:
        self.ok("PUT", "/api/manual/items", body={"group_id": group, "code": code, "name": code, "kind": "B",
                                                  "costs": {"MATERIAL": material}})

    def test_master_beats_bom_per_head(self) -> None:
        self.assertEqual(self.material(self.source)[0], "80")
        self.register(self.source, "B-2", "99")
        self.assertEqual(self.material(self.source)[0], "317")  # 3×99 ＋ 2×10
        # 費目の値の無い登録は BOM の値のまま
        self.ok("PUT", "/api/manual/items", body={"group_id": self.source, "code": "B-1", "name": "部品1",
                                                  "costs": {"DIRECT": "4"}})
        self.assertEqual(self.material(self.source)[0], "317")

    def test_unused_master_counts_only_overridden_codes(self) -> None:
        self.register(self.source, "B-2", "99")
        self.register(self.source, "N-1", "5")  # BOM に無い部品の追加は数えない
        group = self.copy(["volume"], master=True)["group_id"]
        self.assertEqual({i["code"] for i in self.ok("GET", "/api/manual", f"group={group}")["items"]}, {"B-2", "N-1"})
        items = "品番,品名,区分\nP-1,製品,P\nS-1,組立,B\nB-1,部品1,B\n"
        bom = "親品番,風船番号,子品番,員数\nP-1,1,S-1,1\nP-1,2,B-1,2\nS-1,1,B-1,3\n"
        check = self.ok("POST", "/api/import/check", body=self.files(group, {"item": items, "bom_line": bom,
                                                                             "cost": "MBOM品番,材料費\nB-1,10\n"}))
        self.assertEqual(check["unused_master"], {"count": 1, "codes": ["B-2"]})

    def test_copy_without_master_or_changes_shares_nothing(self) -> None:
        self.register(self.source, "B-2", "99")
        self.change(self.source, kind="SET_HEAD", part="P-1", target_head="DIRECT", quantity="5")
        group = self.copy(["item", "bom_line", "cost", "volume"])["group_id"]
        self.assertEqual(self.ok("GET", "/api/manual", f"group={group}")["items"], [])
        self.assertEqual(self.batches(group), [])
        self.assertEqual(self.material(group), ("80", "80"))
        # 写した後はつながらない: 元の登録を変えても写しは変わらない
        self.register(self.source, "B-1", "1000")
        copied = self.copy(["item", "bom_line", "cost", "volume"], master=True, note="登録ごと")["group_id"]
        self.register(self.source, "B-1", "1")
        self.assertEqual(self.material(copied)[0], "2297")  # 3×99 ＋ 2×1000
        self.assertEqual(Decimal(self.material(self.source)[0]), Decimal(299))


class Groups(GroupCase):
    def test_list_info_note_and_delete(self) -> None:
        group = self.copy(["item"], note="試し")["group_id"]
        rows = {g["group_id"]: g for g in self.ok("GET", "/api/groups")}
        self.assertEqual((rows[group]["label"], rows[group]["copied_from_label"], rows[group]["bom_tables"]),
                         ("D-1 ユニット（試し）", "D-1 ユニット（元）", ["item"]))
        self.ok("PUT", "/api/groups/note", f"group={group}", body={"note": "直した"})
        self.assertEqual(self.ok("GET", "/api/groups/info", f"group={group}")["note"], "直した")
        left = self.ok("DELETE", "/api/groups", f"group={group}")
        self.assertEqual([g["group_id"] for g in left], [self.source])
        self.assert_error(self.call("GET", "/api/groups/info", f"group={group}"), 404, group)

    def test_create_errors(self) -> None:
        self.assert_error(self.call("POST", "/api/groups", body={}), 400, "開発番号")
        self.assert_error(self.call("POST", "/api/groups", body={"dev_number": "D-1", "copy": {"master": True}}),
                          400, "流用元")
        self.assert_error(self.call("POST", "/api/groups", body={"from": "NOPE"}), 404, "NOPE")
        self.assert_error(self.call("POST", "/api/groups", body={"from": self.source, "copy": {"tables": ["x"]}}),
                          400, "copy.tables")


class Screen(unittest.TestCase):
    def test_groups_and_import_check_wiring(self) -> None:
        index = static_response("/").body.decode("utf-8")
        screens = static_response("/screens.js").body.decode("utf-8")
        self.assertIn('id="screen-groups"', index)
        self.assertIn('id="import-check"', index)
        self.assertNotIn("import-dev-number", index)
        for path in ("/api/import/check", "/api/groups", "/api/groups/note", "/api/groups/info"):
            self.assertIn(f'"{path}', screens.replace("`", '"'))
        # 確認を出している間は［決定］だけが主ボタン
        check = screens[screens.index("function drawImportCheck"):screens.index("async function decideImport")]
        self.assertIn('classList.toggle("primary", !c)', check)
        self.assertEqual(check.count('class: "primary"'), 1)
        form = screens[screens.index("function drawGroupForm"):screens.index("async function deleteGroup")]
        self.assertEqual(form.count('class: "primary"'), 1)
