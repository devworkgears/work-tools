"""設計検討（/v2/）: 3つの入力が1つの変更の集まりに書くこと・後勝ち・上書きの印・読み直しの確認・計算・版。データは架空。"""

import base64
import io
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from bomcost.api import Api
from bomcost.app import static_response
from bomcost.importer import load_openpyxl
from bomcost.model import BomLine, CostHead, CostRow, Item, Version, Volume
from bomcost.store import Store
from bomcost.study import (
    after_parts,
    GRID,
    HEADS,
    NEGATIVE,
    NO_PLACE,
    NOT_TARGET,
    OTHER_PART,
    REPLACE,
    replace_groups,
    REWRITE,
    UNREGISTERED,
    StudyStore,
    calculate,
    check_changes,
    product_lines,
    replace_overridden,
    where_used,
)

ALL = ("item", "bom_line", "cost", "volume")
# 設計検討の見本（samples_v2）の品番
P1, P2 = "PR1001-001", "PR1002-001"
OLD, NEW = "KB0120-001", "KB0121-001"  # 全製品に効く共通部品と、置き換え先


def bom(k121_cost: str = "120", lines: list[BomLine] | None = None) -> Version:
    """P1・P2 は組立品 U10（K120×2）を持つ。P3 は K120 を直に持つ。"""
    v = Version("t")
    for code, name in (("P1", "製品1"), ("P2", "製品2"), ("P3", "製品3"), ("U10", "組立"), ("K120", "ブラケット"),
                       ("K121", "ブラケット改"), ("B1", "ボルト"), ("S50", "ステー"), ("Q9", "外の製品")):
        v.add_item(Item(code, name))
    v.add_bom_lines(lines if lines is not None else [
        BomLine("P1", "10", "U10", 1), BomLine("P1", "20", "S50", 1), BomLine("P1", "31", "B1", 4),
        BomLine("U10", "12", "K120", 2), BomLine("U10", "13", "B1", 4),
        BomLine("P2", "10", "U10", 1), BomLine("P2", "20", "S50", 2),
        BomLine("P3", "5", "K120", 1),
        BomLine("Q9", "1", "K120", 3),
    ])
    for code, material in (("K120", "100"), ("K121", k121_cost), ("B1", "5"), ("S50", "30")):
        v.add_cost(CostRow(code, {CostHead.MATERIAL: material}))
    v.add_volume(Volume("P1", 1000))
    v.add_volume(Volume("P2", 500))
    return v


class StudyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.store = StudyStore(self.conn)
        self.store.replace_bom(bom(), ALL)
        self.number = self.store.create_study("ステー変更")
        self.store.save_targets(self.number, ["P1", "P2", "P3"], "P1")

    def tearDown(self) -> None:
        self.conn.close()

    def changes(self, plan: str = "A") -> dict[tuple, tuple[Decimal, str]]:
        ws = self.store.workspace(self.number)
        return {c.place: (c.delta, c.source) for c in ws.plan_changes(plan)}


class Numbering(StudyCase):
    def test_number_is_given_on_create_and_plan_a_exists(self) -> None:
        self.assertEqual(self.number, "D-0001")
        self.assertEqual(self.store.create_study(), "D-0002")
        self.assertEqual(self.store.workspace(self.number).plan_ids(), ["A"])

    def test_unknown_number_is_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.store.workspace("D-9999")


class ReplaceInput(StudyCase):
    def test_replace_writes_minus_and_plus_for_every_target(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.assertEqual(self.changes(), {
            ("P1", "U10", "12", "K120"): (Decimal(-2), REPLACE),
            ("P1", "U10", "12", "K121"): (Decimal(2), REPLACE),
            ("P2", "U10", "12", "K120"): (Decimal(-2), REPLACE),
            ("P2", "U10", "12", "K121"): (Decimal(2), REPLACE),
            ("P3", "P3", "5", "K120"): (Decimal(-1), REPLACE),
            ("P3", "P3", "5", "K121"): (Decimal(1), REPLACE),
        })

    def test_removing_a_row_removes_its_changes(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_replaces(self.number, "A", [])
        self.assertEqual(self.changes(), {})

    def test_plans_are_separate(self) -> None:
        self.store.save_replaces(self.number, "B", [("S50", "K121")])
        self.assertEqual(self.changes("A"), {})
        self.assertIn(("P2", "P2", "20", "S50"), self.changes("B"))
        self.assertEqual(self.store.workspace(self.number).plan_ids(), ["A", "B"])

    def test_same_old_and_new_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save_replaces(self.number, "A", [("K120", "K120")])

    def test_row_with_a_balloon_replaces_only_that_group(self) -> None:
        # 置換は「元品番 × 風船番号」のまとまりで選ぶ（製品ごとに選ぶのは グリッド画面・書き換え）
        self.assertEqual(replace_groups(self.store.workspace(self.number).bom, ["P1", "P2", "P3"], "K120"),
                         [("5", ["P3"]), ("12", ["P1", "P2"])])
        self.store.save_replaces(self.number, "A", [("K120", "K121", "12")])
        self.assertEqual({k[0] for k in self.changes()}, {"P1", "P2"})
        self.assertEqual(self.store.workspace(self.number).replaces["A"], [("K120", "K121", "12")])

    def test_all_row_and_balloon_row_of_the_same_part_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save_replaces(self.number, "A", [("K120", "K121"), ("K120", "K122", "12")])
        with self.assertRaises(ValueError):
            self.store.save_replaces(self.number, "A", [("K120", "K121", "12"), ("K120", "K122", "12")])
        self.store.save_replaces(self.number, "A", [("K120", "K121", "12"), ("K120", "K122", "5")])
        self.assertEqual({(k[0], k[3]) for k in self.changes()},
                         {("P1", "K120"), ("P1", "K121"), ("P2", "K120"), ("P2", "K121"), ("P3", "K120"), ("P3", "K122")})


class LaterWriteWins(StudyCase):
    def test_grid_over_replace_does_not_add_and_marks_the_replace_row(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_grid(self.number, "A", ["P2"], ["K121"], [{"product": "P2", "part": "K121", "value": "1"}])
        got = self.changes()
        self.assertEqual(got[("P2", "U10", "12", "K121")], (Decimal(1), GRID))
        self.assertEqual(got[("P1", "U10", "12", "K121")], (Decimal(2), REPLACE))
        ws = self.store.workspace(self.number)
        self.assertEqual(replace_overridden(ws, "A"), {("K120", "K121", None): True})

    def test_replace_alone_is_not_marked(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.assertEqual(replace_overridden(self.store.workspace(self.number), "A"), {("K120", "K121", None): False})

    def test_running_replace_again_keeps_hand_edits(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_grid(self.number, "A", ["P2"], ["K121"], [{"product": "P2", "part": "K121", "value": "1"}])
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.assertEqual(self.changes()[("P2", "U10", "12", "K121")], (Decimal(1), GRID))

    def test_rewrite_over_grid_replaces_the_amount(self) -> None:
        self.store.save_grid(self.number, "A", ["P1"], ["S50"], [{"product": "P1", "part": "S50", "value": "-1"}])
        rows = [{"parent": "P1", "balloon": "10", "part": "U10", "quantity": "1"},
                {"parent": "P1", "balloon": "20", "part": "S50", "quantity": "3"},
                {"parent": "P1", "balloon": "31", "part": "B1", "quantity": "4"},
                {"parent": "U10", "balloon": "12", "part": "K120", "quantity": "2"},
                {"parent": "U10", "balloon": "13", "part": "B1", "quantity": "4"}]
        self.store.save_rewrite(self.number, "A", "P1", rows)
        self.assertEqual(self.changes(), {("P1", "P1", "20", "S50"): (Decimal(2), REWRITE)})

    def test_rewrite_part_change_is_minus_old_plus_new_at_the_same_place(self) -> None:
        rows = [{"parent": "P3", "balloon": "5", "part": "K121", "quantity": "1"}]
        self.store.save_rewrite(self.number, "A", "P3", rows)
        self.assertEqual(self.changes(), {("P3", "P3", "5", "K120"): (Decimal(-1), REWRITE),
                                          ("P3", "P3", "5", "K121"): (Decimal(1), REWRITE)})

    def test_rewrite_leaves_untouched_rows_from_other_inputs(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        rows = [{"parent": "P3", "balloon": "5", "part": "K121", "quantity": "1"}]
        self.store.save_rewrite(self.number, "A", "P3", rows)
        self.assertEqual(self.changes()[("P3", "P3", "5", "K121")], (Decimal(1), REPLACE))

    def test_grid_empty_cell_deletes(self) -> None:
        self.store.save_grid(self.number, "A", ["P1"], ["S50"], [{"product": "P1", "part": "S50", "value": "-1"}])
        self.store.save_grid(self.number, "A", ["P1"], ["S50"], [{"product": "P1", "part": "S50", "value": None}])
        self.assertEqual(self.changes(), {})

    def test_grid_new_part_goes_to_the_minus_place_above_in_the_same_column(self) -> None:
        self.store.save_grid(self.number, "A", ["P1"], ["K120", "K121"], [
            {"product": "P1", "part": "K121", "value": "2", "row": 1},
            {"product": "P1", "part": "K120", "value": "0", "row": 0},  # 今 2 → 0
        ])
        self.assertEqual(self.changes(), {("P1", "U10", "12", "K120"): (Decimal(-2), GRID),
                                          ("P1", "U10", "12", "K121"): (Decimal(2), GRID)})

    def test_grid_stores_header_and_rows(self) -> None:
        self.store.save_grid(self.number, "A", ["P1", "P2"], ["B1"], [])
        info = self.store.workspace(self.number).plans[0]
        self.assertEqual((info.grid_columns, info.grid_rows), (["P1", "P2"], ["B1"]))


class Checks(StudyCase):
    def problems(self) -> dict[tuple, tuple[str, ...]]:
        return {c.change.place: c.problems for c in check_changes(self.store.workspace(self.number))}

    def by_part(self, product: str, part: str) -> tuple[str, ...]:
        found = [c.problems for c in check_changes(self.store.workspace(self.number))
                 if c.change.product == product and c.change.part == part]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_same_checks_whichever_input_wrote(self) -> None:
        self.store.save_grid(self.number, "A", ["P1", "Q9"], ["S50", "X1", "K120"], [
            {"product": "P1", "part": "S50", "value": "-1"},  # 1 個しか無い所を -1 個に
            {"product": "P1", "part": "X1", "value": "1"},  # 品目に無い
            {"product": "Q9", "part": "K120", "value": "2"},  # 対象品番でない列（今 3 → 2）
        ])
        self.store.save_rewrite(self.number, "A", "P2", [
            {"parent": "P2", "balloon": "10", "part": "U10", "quantity": "1"},
            {"parent": "P2", "balloon": "20", "part": "S50", "quantity": "-1"},
            {"parent": "U10", "balloon": "12", "part": "K120", "quantity": "2"},
            {"parent": "U10", "balloon": "13", "part": "B1", "quantity": "4"}])
        got = self.problems()
        self.assertEqual(got[("P1", "P1", "20", "S50")], (NEGATIVE,))
        self.assertEqual(self.by_part("P1", "X1"), (UNREGISTERED,))
        self.assertEqual(got[("Q9", "Q9", "1", "K120")], (NOT_TARGET,))
        self.assertEqual(got[("P2", "P2", "20", "S50")], (NEGATIVE,))

    def test_reading_the_bom_again_finds_lost_and_changed_places(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_grid(self.number, "A", ["P1"], ["S50"], [{"product": "P1", "part": "S50", "value": "-1"}])
        changed = [BomLine("P1", "10", "U10", 1), BomLine("P1", "31", "B1", 4),
                   BomLine("U10", "12", "K125", 2), BomLine("U10", "13", "B1", 4),
                   BomLine("P2", "10", "U10", 1), BomLine("P2", "20", "S50", 2), BomLine("P3", "5", "K120", 1)]
        self.store.replace_bom(bom(lines=changed), ["bom_line"])
        got = self.problems()
        self.assertEqual(got[("P1", "U10", "12", "K120")], (OTHER_PART,))
        self.assertEqual(got[("P1", "P1", "20", "S50")], (NO_PLACE,))
        self.assertEqual(got[("P3", "P3", "5", "K120")], ())
        ws = self.store.workspace(self.number)
        result = calculate(ws, CostHead.MATERIAL)
        p1 = next(r for r in result["rows"] if r["product"] == "P1")
        # K120 の引きと S50 の引きは計算に入らない。置換の組の相手の K121 の足しも一緒に外れる
        self.assertEqual(p1["plans"]["A"], p1["now"])
        self.assertTrue(p1["incomplete"]["A"])

    def test_parent_outside_the_product_is_no_place(self) -> None:
        self.store.save_rewrite(self.number, "A", "P3", [
            {"parent": "P3", "balloon": "5", "part": "K120", "quantity": "1"},
            {"parent": "U99", "balloon": "1", "part": "B1", "quantity": "1"}])
        self.assertEqual(self.problems()[("P3", "U99", "1", "B1")], (NO_PLACE,))


class ApplyReplace(StudyCase):
    def test_replace_moves_the_after_quantity_in_every_target_and_undoes_by_replacing_back(self) -> None:
        self.store.apply_replace(self.number, "A", "K120", "K121")
        self.assertEqual(self.changes(), {
            ("P1", "U10", "12", "K120"): (Decimal(-2), REPLACE), ("P1", "U10", "12", "K121"): (Decimal(2), REPLACE),
            ("P2", "U10", "12", "K120"): (Decimal(-2), REPLACE), ("P2", "U10", "12", "K121"): (Decimal(2), REPLACE),
            ("P3", "P3", "5", "K120"): (Decimal(-1), REPLACE), ("P3", "P3", "5", "K121"): (Decimal(1), REPLACE)})
        self.assertEqual(self.store.workspace(self.number).replaces, {})  # 決まりとしては残さない
        parts = {code: (users, was) for code, users, was in after_parts(self.store.workspace(self.number), "A")}
        self.assertEqual(parts["K121"], (["P1", "P2", "P3"], ["K120"]))
        self.assertNotIn("K120", parts)
        # 後から直した数量も、そのまま置き換えに乗る（変えた後の構成で動く）
        self.store.save_grid(self.number, "A", ["P1"], ["K121"], [{"product": "P1", "part": "K121", "value": "3"}])
        self.store.apply_replace(self.number, "A", "K121", "K120")
        self.assertEqual(self.changes(), {("P1", "U10", "12", "K120"): (Decimal(1), REPLACE)})
        with self.assertRaises(ValueError):
            self.store.apply_replace(self.number, "A", "ZZ1", "K121")


    def test_revert_leaves_the_products_that_already_used_the_new_part(self) -> None:
        # 今の部品表で S50 を使う P1・P2 と、P3 の K120 を S50 に替えた所が1行にまとまる
        self.store.apply_replace(self.number, "A", "K120", "S50")
        self.assertEqual(dict((c, u) for c, u, _ in after_parts(self.store.workspace(self.number), "A"))["S50"],
                         ["P1", "P2", "P3"])
        self.store.revert_replace(self.number, "A", "S50")
        self.assertEqual(self.changes(), {})
        with self.assertRaises(ValueError):
            self.store.revert_replace(self.number, "A", "S50")


class Calculation(StudyCase):
    def test_now_and_plans_and_annual_impact(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_grid(self.number, "B", ["P2"], ["S50"], [{"product": "P2", "part": "S50", "value": "1"}])
        result = calculate(self.store.workspace(self.number), CostHead.MATERIAL)
        rows = {r["product"]: r for r in result["rows"]}
        self.assertEqual(result["plans"], ["A", "B"])
        # P1 = U10(2×100 + 4×5) + 30 + 4×5
        self.assertEqual(rows["P1"]["now"], Decimal(270))
        self.assertEqual(rows["P1"]["plans"], {"A": Decimal(310), "B": Decimal(270)})
        self.assertEqual(rows["P2"]["plans"], {"A": Decimal(320), "B": Decimal(250)})
        self.assertEqual(result["impact"]["A"], Decimal(40) * 1000 + Decimal(40) * 500)
        self.assertTrue(result["impact_incomplete"]["A"])  # P3 は年間台数が無い
        # 台数の加重平均は、年間の影響に入れた P1・P2 の台数（1000＋500）で割る。台数の無い P3 は分母に入れない
        self.assertEqual(result["weighted"]["A"], Decimal(40))
        self.assertEqual(result["weighted"]["B"], Decimal(-30) * 500 / 1500)  # P2 は 280 → 250、P1 は差 0 でも分母に入る

    def test_weighted_is_none_without_any_annual(self) -> None:
        self.store.save_targets(self.number, ["P3"])
        result = calculate(self.store.workspace(self.number), CostHead.MATERIAL)
        self.assertEqual(result["weighted"], {p: None for p in result["plans"]})

    def test_annual_from_conditions_overrides_the_bom(self) -> None:
        self.store.save_targets(self.number, ["P1"])
        self.store.save_conditions(self.number, {"P1": "10"})
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        result = calculate(self.store.workspace(self.number), CostHead.MATERIAL)
        self.assertEqual(result["impact"]["A"], Decimal(400))
        self.assertFalse(result["impact_incomplete"]["A"])

    def test_unregistered_cost_marks_incomplete(self) -> None:
        self.store.save_targets(self.number, ["P1"])
        self.store.save_grid(self.number, "A", ["P1"], ["X1"], [{"product": "P1", "part": "X1", "value": "1"}])
        result = calculate(self.store.workspace(self.number), CostHead.MATERIAL)
        self.assertTrue(result["rows"][0]["incomplete"]["A"])

    def test_total_head_uses_the_formula(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        result = calculate(self.store.workspace(self.number), CostHead.MANUFACTURING)
        p1 = result["rows"][0]
        # 製造原価 ＝ 材料費 × 1.03 × 1.11（直接費・償却費は未登録で 0）
        self.assertEqual(p1["now"], Decimal("308.691"))
        self.assertEqual(p1["plans"]["A"], Decimal("354.423"))

    def test_processing_and_amortization_heads_are_the_product_own_values(self) -> None:
        # 部品の置き換えで変わるのは材料費だけ。加工（社内加工費）は未登録なら 0、型費（償却費）は未登録なら空で不完全
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        ws = self.store.workspace(self.number)
        processing = calculate(ws, HEADS["processing"])["rows"][0]
        self.assertEqual((processing["now"], processing["plans"]["A"]), (Decimal(0), Decimal(0)))
        self.assertFalse(processing["now_incomplete"] or processing["incomplete"]["A"])
        amortization = calculate(ws, HEADS["amortization"])
        self.assertIsNone(amortization["rows"][0]["plans"]["A"])
        self.assertTrue(amortization["rows"][0]["incomplete"]["A"])
        self.assertTrue(amortization["impact_incomplete"]["A"])

    def test_calculation_does_not_write(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        before = self.conn.total_changes
        calculate(self.store.workspace(self.number), CostHead.MANUFACTURING)
        check_changes(self.store.workspace(self.number))
        self.assertEqual(self.conn.total_changes, before)


class Release(StudyCase):
    def test_release_freezes_the_study_and_the_bom(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.assertEqual(self.store.take_release(self.number), 1)
        frozen = calculate(self.store.workspace(self.number, 1), CostHead.MATERIAL)
        # 仮を直し、部品表の単価も変わる
        self.store.save_replaces(self.number, "A", [])
        self.store.replace_bom(bom(k121_cost="200"), ["cost"])
        again = calculate(self.store.workspace(self.number, 1), CostHead.MATERIAL)
        self.assertEqual(again, frozen)
        self.assertEqual(again["rows"][0]["plans"]["A"], Decimal(310))
        draft = calculate(self.store.workspace(self.number), CostHead.MATERIAL)
        self.assertEqual(draft["rows"][0]["plans"]["A"], Decimal(270))
        self.assertEqual(self.store.versions(self.number), [1])
        self.assertEqual(self.store.take_release(self.number), 2)

    def test_unknown_version_is_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.store.workspace(self.number, 3)


class Attributes(unittest.TestCase):
    def test_old_item_table_gets_the_attribute_columns(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE v2_item (code TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '', "
                     "specification TEXT NOT NULL DEFAULT '')")
        store = StudyStore(conn)
        store.replace_bom(bom(), ALL, {"P1": {"model": "M1", "product_spec": "標準", "drawing": "D1"}})
        self.assertEqual(store.load_attributes()["P1"], {"model": "M1", "product_spec": "標準", "drawing": "D1"})
        self.assertEqual(store.load_attributes()["P2"], {"model": "", "product_spec": "", "drawing": ""})
        conn.close()


class WhereUsed(unittest.TestCase):
    def test_inside_and_outside_the_targets(self) -> None:
        inside, outside = where_used(bom(), "K120", ["P1", "P3"])
        self.assertEqual(inside, ["P1", "P3"])
        self.assertEqual(outside, ["P2", "Q9"])


class StudyApiFlow(unittest.TestCase):
    """見本の CSV を取り込み、③→④→⑤→⑥→⑦→版数取得 を API で通す。"""

    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.api = Api(self.store)

    def tearDown(self) -> None:
        self.store.close()

    def ok(self, method: str, path: str, query: str = "", body: object = None) -> dict:
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        response = self.api.handle(method, path, query, raw)
        self.assertLess(response.status, 300, response.body.decode("utf-8"))
        return response.json()

    def test_create_with_targets_sets_them_in_one_call(self) -> None:
        # 新規作成は対象品番を選んでから作る（作った時点で対象品番が入っている）
        made = self.ok("POST", "/api/v2/studies", body={"title": "差し替え", "products": [P1, P2], "main_product": P2})
        self.assertEqual(made["targets"], [P1, P2])
        number = made["header"]["number"]
        self.assertEqual(self.ok("GET", "/api/v2/targets", f"number={number}")["main_product"], P2)

    def test_main_product_is_chosen_on_the_grid_and_kept_by_the_target_list(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        number = self.ok("POST", "/api/v2/studies", body={"products": [P1, P2]})["header"]["number"]
        q = f"number={number}"
        self.assertEqual(self.ok("PUT", "/api/v2/main-product", q, {"main_product": P2})["main_product"], P2)
        self.assertEqual(self.ok("GET", "/api/v2/grid", q + "&plan=A")["main_product"], P2)
        # ③で対象品番を直しても、主対象は残る（主対象を外した時だけ消える）
        self.ok("PUT", "/api/v2/targets", q, {"products": [P2, P1]})
        self.assertEqual(self.ok("GET", "/api/v2/targets", q)["main_product"], P2)
        self.ok("PUT", "/api/v2/targets", q, {"products": [P1]})
        self.assertIsNone(self.ok("GET", "/api/v2/targets", q)["main_product"])
        bad = self.api.handle("PUT", "/api/v2/main-product", q, json.dumps({"main_product": P2}).encode())
        self.assertEqual(bad.status, 400, bad.body)
        self.assertIsNone(self.ok("PUT", "/api/v2/main-product", q, {"main_product": ""})["main_product"])

    def test_temp_items_price_an_unregistered_new_part(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        number = self.ok("POST", "/api/v2/studies", body={"products": [P1, P2]})["header"]["number"]
        q = f"number={number}"
        tmp = "ZZ0001-001"
        price = self.ok("POST", "/api/v2/lookup", q, {"codes": [NEW]})["items"][NEW]["material"]
        # 新品番を未登録の品番にすると、結果は不完全で、仮品番の画面に未登録として出る
        self.ok("PUT", "/api/v2/replace", q + "&plan=A", {"rows": [{"old": OLD, "new": tmp}]})
        self.assertTrue(all(r["incomplete"]["A"] for r in self.ok("GET", "/api/v2/calc", q + "&head=material")["rows"]))
        self.assertEqual(self.ok("GET", "/api/v2/temp-items", q)["unregistered"], [tmp])
        # 仮品番に新品番と同じ材料費を入れると、新品番に置き換えた時と同じ値で、不完全の印が外れる
        got = self.ok("PUT", "/api/v2/temp-items", q, {"rows": [{"code": tmp, "name": "仮の部品", "material": price},
                                                               {"code": NEW, "name": "台帳にある品番"}, {"code": ""}]})
        self.assertEqual(got["rows"], [{"code": tmp, "name": "仮の部品", "material": price, "master": False},
                                       {"code": NEW, "name": "台帳にある品番", "material": None, "master": True}])
        self.assertEqual(got["unregistered"], [])
        looked = self.ok("POST", "/api/v2/lookup", q, {"codes": [tmp, NEW]})["items"]
        self.assertEqual((looked[tmp]["name"], looked[tmp]["temp"], looked[NEW]["temp"]), ("仮の部品", True, False))
        with_temp = self.ok("GET", "/api/v2/calc", q + "&head=material")
        self.assertFalse(any(r["incomplete"]["A"] for r in with_temp["rows"]))
        self.ok("PUT", "/api/v2/replace", q + "&plan=A", {"rows": [{"old": OLD, "new": NEW}]})
        self.assertEqual(self.ok("GET", "/api/v2/calc", q + "&head=material")["impact"], with_temp["impact"])
        # 版数取得で仮品番も固まる
        self.ok("PUT", "/api/v2/replace", q + "&plan=A", {"rows": [{"old": OLD, "new": tmp}]})
        version = self.ok("POST", "/api/v2/release", q, {})["version"]
        self.ok("PUT", "/api/v2/temp-items", q, {"rows": []})
        fixed = f"{q}&version={version}"
        self.assertEqual(self.ok("GET", "/api/v2/temp-items", fixed)["rows"][0]["code"], tmp)
        self.assertEqual(self.ok("GET", "/api/v2/calc", fixed + "&head=material")["impact"], with_temp["impact"])
        # 同じ品番の2行は受けない
        raw = json.dumps({"rows": [{"code": tmp}, {"code": tmp}]}).encode("utf-8")
        self.assertGreaterEqual(self.api.handle("PUT", "/api/v2/temp-items", q, raw).status, 400)

    def test_own_heads_rewrite_the_product_values_in_the_plan(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        number = self.ok("POST", "/api/v2/studies", body={"products": [P1, P2]})["header"]["number"]
        q = f"number={number}"
        before = self.ok("GET", "/api/v2/calc", q + "&head=all")
        self.assertEqual([h["key"] for h in before["heads"]],
                         ["material", "processing", "amortization", "adjustment", "material_total", "indirect",
                          "direct", "total"])
        own = self.ok("GET", "/api/v2/own", q + "&plan=A")
        now = own["rows"][0]["heads"]["processing"]["now"]
        self.assertEqual(own["rows"][0]["product"], P1)
        got = self.ok("PUT", "/api/v2/own", q + "&plan=A", {"rows": [
            {"product": P1, "head": "processing", "value": "1000"}, {"product": P1, "head": "direct", "value": ""}]})
        self.assertEqual(got["rows"][0]["heads"]["processing"], {"now": now, "after": "1000"})
        self.assertIsNone(got["rows"][0]["heads"]["direct"]["after"])  # 空は書き換えなし
        after = self.ok("GET", "/api/v2/calc", q + "&head=all")
        heads = {h["key"]: h for h in after["heads"]}
        p1 = heads["processing"]["rows"][0]
        self.assertEqual((p1["now"], p1["plans"]["A"]), (now, "1000"))
        self.assertEqual(heads["processing"]["rows"][1]["plans"]["A"], heads["processing"]["rows"][1]["now"])
        # 加工費の差は調整費・間接費を通って製造原価に効く。費目全部の値は1つの費目で出した値と同じ
        total = Decimal(heads["total"]["rows"][0]["plans"]["A"]) - Decimal(heads["total"]["rows"][0]["now"])
        self.assertEqual(total, (Decimal(1000) - Decimal(now)) * Decimal("1.03") * Decimal("1.11"))
        single = self.ok("GET", "/api/v2/calc", q + "&head=total")
        self.assertEqual((single["rows"], single["impact"]), (heads["total"]["rows"], heads["total"]["impact"]))
        book = load_openpyxl().load_workbook(io.BytesIO(self.api.handle("GET", "/api/v2/export/result",
                                                                        q + "&head=all", b"").body)).active
        self.assertEqual([c.value for c in book[1]], ["対象品番", "費目", "年間台数", "今", "案A"])
        self.assertEqual(book.max_row, 1 + 2 * 8 + 8 * 2)  # 2製品×8費目＋年間の影響と加重平均×8費目
        # 変更一覧にも出る
        changed = [r for r in self.ok("GET", "/api/v2/changes", q + "&plan=A")["rows"] if r["source"] == "own"]
        self.assertEqual([(r["product"], r["before"], r["after"]) for r in changed], [(P1, now, "1000")])
        # 版数取得で固まる
        version = self.ok("POST", "/api/v2/release", q, {})["version"]
        self.ok("PUT", "/api/v2/own", q + "&plan=A", {"rows": []})
        fixed = self.ok("GET", "/api/v2/own", f"{q}&plan=A&version={version}")
        self.assertEqual(fixed["rows"][0]["heads"]["processing"]["after"], "1000")
        # 書き換えられない費目・数でない値は受けない
        for row in ({"product": P1, "head": "material", "value": "1"}, {"product": P1, "head": "direct", "value": "x"}):
            raw = json.dumps({"rows": [row]}).encode("utf-8")
            self.assertGreaterEqual(self.api.handle("PUT", "/api/v2/own", q + "&plan=A", raw).status, 400)

    def test_parts_of_chosen_targets_are_in_part_number_order(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        number = self.ok("POST", "/api/v2/studies", body={"products": [P1, P2]})["header"]["number"]
        q = f"number={number}"
        both = self.ok("GET", "/api/v2/parts", q + f"&products={P1},{P2}")
        self.assertEqual(both["of"], 2)
        codes = [p["code"] for p in both["parts"]]
        self.assertEqual(codes, sorted(codes))  # 品番の順
        self.assertEqual(next(p["used"] for p in both["parts"] if p["code"] == OLD), 2)
        # 対象品番に無い製品は数えない
        self.assertEqual(self.ok("GET", "/api/v2/parts", q + f"&products={P1},ZZ9999-001")["of"], 1)
        self.assertEqual(self.ok("GET", "/api/v2/parts", q + "&products=")["parts"], [])

    def test_create_with_a_bad_target_makes_no_study(self) -> None:
        raw = json.dumps({"products": [P1, 1.5]}).encode("utf-8")  # 小数の品番は元の表記が分からないので受けない
        self.assertGreaterEqual(self.api.handle("POST", "/api/v2/studies", "", raw).status, 400)
        self.assertEqual(self.ok("GET", "/api/v2/studies")["studies"], [])

    def test_flow_with_samples(self) -> None:
        imported = self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        self.assertEqual(sorted(imported["tables"]), ["bom_line", "cost", "item", "volume"])
        self.assertEqual(imported["warnings"], [])
        number = self.ok("POST", "/api/v2/studies", body={"title": "差し替え"})["header"]["number"]
        q = f"number={number}"
        self.ok("PUT", "/api/v2/targets", q, {"products": [P1, P2, "ZZ9999-001"], "main_product": P1})
        looked = self.ok("POST", "/api/v2/lookup", body={"codes": [P1, "ZZ9999-001"]})["items"]
        self.assertTrue(looked[P1]["registered"])
        self.assertFalse(looked["ZZ9999-001"]["registered"])
        rows = self.ok("PUT", "/api/v2/replace", q + "&plan=A", {"rows": [{"old": OLD, "new": NEW}]})["rows"]
        self.assertEqual(rows, [{"old": OLD, "new": NEW, "balloon": None, "count": 2, "products": [P1, P2],
                                 "overridden": False}])
        groups = self.ok("GET", "/api/v2/replace/groups", q + f"&code={OLD}")["groups"]
        self.assertEqual(sorted({p for g in groups for p in g["products"]}), [P1, P2])
        self.assertTrue(all(g["count"] == len(g["products"]) for g in groups))
        grid = self.ok("PUT", "/api/v2/grid", q + "&plan=A",
                     {"columns": [P1], "rows": ["ST0050-001"], "cells": [{"product": P1, "part": "ST0050-001",
                                                                          "value": "0"}]})
        # セルは変えた後の数量（列の製品で使っていなければ出さない）
        self.assertEqual(grid["rows"][0]["cells"], {P1: "0", P2: "2"})
        self.assertIn({"part": OLD, "origin": 1, "cells": {P1: "0", P2: "0"}, "problems": {}, "locked": {}},
                      [{k: v for k, v in r.items() if k not in ("balloons", "now")} for r in grid["rows"]])
        self.assertEqual(next(r for r in grid["rows"] if r["part"] == OLD)["now"], {P1: "2", P2: "2"})
        # 風船の行: 列の製品ごとに、その製品でのその品番の風船番号
        self.assertEqual(sorted(next(r for r in grid["rows"] if r["part"] == OLD)["balloons"]), [P1, P2])
        changes = self.ok("GET", "/api/v2/changes", q + "&plan=A")["rows"]
        self.assertTrue(any(r["part"] == NEW and r["added"] for r in changes))
        before = self.store.connection.total_changes
        result = self.ok("GET", "/api/v2/calc", q + "&head=material")
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertEqual([r["product"] for r in result["rows"]], [P1, P2, "ZZ9999-001"])
        rewrite = self.ok("GET", "/api/v2/rewrite", q + f"&plan=A&product={P1}")["rows"]
        self.assertIn(NEW, [r["part"] for r in rewrite])
        # 親ごとにまとまり（製品→フレーム→ヒンジの順）、同じ親の中は風船の順
        parents = [r["parent"] for r in rewrite]
        self.assertEqual(list(dict.fromkeys(parents)), [P1, "FR0010-001", "HG0040-001"])
        self.assertEqual(parents, sorted(parents, key=[P1, "FR0010-001", "HG0040-001"].index))
        self.assertEqual([r["balloon"] for r in rewrite if r["parent"] == P1], ["10", "20", "30", "31", "40"])
        used = self.ok("GET", "/api/v2/where-used", q + f"&code={OLD}")
        self.assertEqual((used["inside"], used["outside"]), (2, 8))
        released = self.ok("POST", "/api/v2/release", q, {})
        self.assertEqual((released["version"], released["versions"]), (1, [1]))
        self.assertEqual(self.ok("GET", "/api/v2/calc", q + "&version=1&head=material"), {
            **result, "computed_at": self.ok("GET", "/api/v2/calc", q + "&version=1&head=material")["computed_at"]})
        excel = self.api.handle("GET", "/api/v2/export/result", q)
        self.assertEqual(excel.status, 200)
        self.assertTrue(excel.body.startswith(b"PK"))

    def test_replacing_the_common_part_keeps_every_product_positive(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        products = [p["code"] for p in self.ok("GET", "/api/v2/products")["products"]]
        self.assertEqual(len(products), 10)
        number = self.ok("POST", "/api/v2/studies", body={})["header"]["number"]
        q = f"number={number}"
        self.ok("PUT", "/api/v2/targets", q, {"products": products})
        self.ok("PUT", "/api/v2/replace", q + "&plan=A", {"rows": [{"old": OLD, "new": NEW}]})
        changes = self.ok("GET", "/api/v2/changes", q + "&plan=A")["rows"]
        self.assertEqual({r["product"] for r in changes}, set(products))
        self.assertTrue(all(not r["problems"] for r in changes))
        for head in ("total", "material"):
            result = self.ok("GET", "/api/v2/calc", q + f"&head={head}")
            for row in result["rows"]:
                with self.subTest(head=head, product=row["product"]):
                    self.assertGreater(Decimal(row["now"]), 0)
                    self.assertGreater(Decimal(row["plans"]["A"]), 0)
                    self.assertLess(Decimal(row["plans"]["A"]), Decimal(row["now"]))  # 置き換え先が安い
                    self.assertFalse(row["now_incomplete"] or row["incomplete"]["A"])
                    self.assertGreater(Decimal(row["annual"]), 0)
            self.assertFalse(result["impact_incomplete"]["A"])
            self.assertLess(Decimal(result["impact"]["A"]), 0)

    def test_samples_carry_the_mismatches(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        v = StudyStore(self.store.connection).load_bom()

        def balloons(product: str, part: str) -> set[tuple[str, str | None]]:
            return {(l.parent, l.balloon) for l in product_lines(v, product) if l.child == part}

        # 1) 同じ品番で製品ごとに風船番号が違う
        self.assertEqual(balloons("PR1001-001", "ST0050-001"), {("PR1001-001", "20")})
        self.assertEqual(balloons("PR1003-001", "ST0050-001"), {("PR1003-001", "6")})
        # 2) 同じ風船番号で製品ごとに品番が違う（同じ組立図）
        self.assertEqual(balloons("PR1004-001", "ST0051-001"), {("PR1004-001", "20")})
        # 3) 1つの製品の中で同じ品番・風船番号が別の親の下に出る
        self.assertEqual({p for p, b in balloons(P1, "BT0008-001") if b == "13"}, {"FR0010-001", "HG0040-001"})
        # 4) ある製品にしか無い部品
        users = {p: where_used(v, p, [])[1] for p in ("HT0090-001", "CV0230-001")}
        self.assertEqual(users, {"HT0090-001": ["PR1002-001"], "CV0230-001": ["PR1503-001"]})
        # 5) 多くの箇所・製品で使う部品
        self.assertEqual(len(where_used(v, "BT0008-001", [])[1]), 10)
        self.assertGreaterEqual(len(balloons(P1, "BT0008-001")), 3)
        # 6) ASSY の下にさらに ASSY・品番
        self.assertIn(("HG0040-001", "1"), balloons(P1, "PN0041-001"))
        # 製品の材料費は登録せず部品から積み上げる（登録値と積み上げが食い違わない）
        self.assertTrue(all(v.cost_value(p, CostHead.MATERIAL) is None for p in v.volumes))

    def test_api_made_outside_the_db_thread_works(self) -> None:
        # 立てる時と同じ形: DB は作業のスレッドで開き、Api は別のスレッドで作る
        with ThreadPoolExecutor(max_workers=1) as worker:
            store = worker.submit(Store, ":memory:").result()
            api = Api(store)
            response = worker.submit(api.handle, "GET", "/api/v2/bom", "", b"").result()
            worker.submit(store.close).result()
        self.assertEqual(response.status, 200)

    def test_item_attributes_from_samples_reach_lookup_and_search(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        looked = self.ok("POST", "/api/v2/lookup", body={"codes": [P1, P2, "PR2003-001", OLD]})["items"]
        attrs = {c: (i["model"], i["product_spec"], i["drawing"]) for c, i in looked.items()}
        self.assertEqual(attrs, {P1: ("AX-100", "標準", "AS-01"), P2: ("AX-100", "寒冷地", "AS-01"),
                                 "PR2003-001": ("BX-200", "輸出", "AS-21"), OLD: ("", "", "")})
        found = self.ok("GET", "/api/v2/products", "model=ax-1")
        self.assertEqual([p["code"] for p in found["products"]],
                         ["PR1001-001", "PR1002-001", "PR1003-001", "PR1004-001",
                          "PR1501-001", "PR1502-001", "PR1503-001"])
        self.assertEqual(found["product_specs"], ["標準", "寒冷地", "輸出"])
        self.assertEqual(found["products"][0]["drawing"], "AS-01")
        by_spec = self.ok("GET", "/api/v2/products", "model=ax-100&product_spec=寒冷地")["products"]
        self.assertEqual([p["code"] for p in by_spec], [P2])

    def test_item_table_without_attribute_columns_leaves_them_blank(self) -> None:
        csv = "品番,品名\nA-100,製品A\n".encode("utf-8")
        self.ok("POST", "/api/v2/bom/import",
                body={"files": [{"filename": "品目.csv", "content": base64.b64encode(csv).decode()}]})
        looked = self.ok("POST", "/api/v2/lookup", body={"codes": ["A-100"]})["items"]["A-100"]
        self.assertEqual((looked["name"], looked["model"], looked["product_spec"], looked["drawing"]),
                         ("製品A", "", "", ""))

    def test_import_with_chosen_columns_reads_unknown_headers_and_keeps_the_map(self) -> None:
        csv = "コードX,なまえY\nA-100,製品A\n".encode("utf-8")
        f = {"filename": "品目.csv", "content": base64.b64encode(csv).decode(),
             "table": "item", "columns": {"code": "コードX", "name": "なまえY"}}
        checked = self.ok("POST", "/api/v2/bom/import", body={"files": [f], "check": True})
        self.assertEqual(checked["counts"], {"item": 1})
        self.assertEqual(self.ok("GET", "/api/v2/bom")["counts"]["item"], 0)  # 確認では何も入らない
        done = self.ok("POST", "/api/v2/bom/import", body={"files": [f]})
        self.assertEqual(done["counts"]["item"], 1)
        looked = self.ok("POST", "/api/v2/lookup", body={"codes": ["A-100"]})["items"]["A-100"]
        self.assertEqual(looked["name"], "製品A")
        preview = self.ok("POST", "/api/import/preview", body={"filename": f["filename"], "content": f["content"]})
        self.assertEqual(preview["suggested"], "item")  # 決定で列の対応を保存し、次の同じ見出しで出る

    def test_import_with_chosen_columns_refuses_a_missing_required_column(self) -> None:
        csv = "コードX,なまえY\nA-100,製品A\n".encode("utf-8")
        f = {"filename": "品目.csv", "content": base64.b64encode(csv).decode(),
             "table": "item", "columns": {"name": "なまえY"}}
        response = self.api.handle("POST", "/api/v2/bom/import", "",
                                   json.dumps({"files": [f]}, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(response.status, 400)

    def test_attributes_are_frozen_with_the_version(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        number = self.ok("POST", "/api/v2/studies", body={})["header"]["number"]
        self.ok("PUT", "/api/v2/targets", f"number={number}", {"products": [P1]})
        self.ok("POST", "/api/v2/release", f"number={number}", {})
        csv = f"品番,品名,代表\n{P1},製品A,ZZ\n".encode("utf-8")
        self.ok("POST", "/api/v2/bom/import",
                body={"files": [{"filename": "品目.csv", "content": base64.b64encode(csv).decode()}]})
        frozen = self.ok("POST", "/api/v2/lookup", f"number={number}&version=1", {"codes": [P1]})["items"]
        self.assertEqual((frozen[P1]["model"], frozen[P1]["drawing"]), ("AX-100", "AS-01"))
        live = self.ok("POST", "/api/v2/lookup", body={"codes": [P1]})["items"]
        self.assertEqual((live[P1]["model"], live[P1]["drawing"]), ("ZZ", ""))
    def test_errors_are_json(self) -> None:
        response = self.api.handle("GET", "/api/v2/study", "number=D-0404")
        self.assertEqual(response.status, 404)
        self.assertIn("error", response.json())


class StaticV2(unittest.TestCase):
    def test_v2_page_and_files(self) -> None:
        page = static_response("/v2/")
        self.assertEqual(page.status, 200)
        self.assertIn(b"v2.js", page.body)
        for name in ("v2.js", "grid.js", "v2.css"):
            self.assertEqual(static_response(f"/v2/{name}").status, 200, name)
        self.assertEqual(static_response("/v2").status, 301)
        self.assertEqual(static_response("/v2/../app.js").status, 404)
        self.assertEqual(static_response("/").status, 200)


if __name__ == "__main__":
    unittest.main()
