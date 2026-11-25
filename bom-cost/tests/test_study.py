"""設計検討（/v2/）: 3つの入力が1つの変更の集まりに書くこと・後勝ち・上書きの印・読み直しの確認・計算・版。データは架空。"""

import base64
import io
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import example_settings
from bomcost import settings
from bomcost.api import Api
from bomcost.app import static_response
from bomcost.importer import load_openpyxl
from bomcost.model import BomLine, CostHead, CostRow, Item, Version, Volume
from bomcost.store import Store
from bomcost.structure import structure
from bomcost.study import (
    ADD,
    after_parts,
    after_places,
    calculate,
    check_changes,
    HEADS,
    NEGATIVE,
    NO_PLACE,
    NOT_TARGET,
    OTHER_PART,
    product_lines,
    QUANTITY,
    REPLACE,
    replace_groups,
    replace_overridden,
    REWRITE,
    StudyStore,
    UNREGISTERED,
    where_used,
    Workspace,
)
from bomcost.studyapi import _slot_rows, changes_by_product, summarize_changes

ALL = ("item", "bom_line", "cost", "volume")
# 設計検討の見本（samples_v2）の品番
P1, P2 = "PR1001-001", "PR1002-001"
OLD, NEW = "CV0210-001", "CV0230-001"  # 6製品が直下で使う部品と、置き換え先


def bom(k121_cost: str = "120", lines: list[BomLine] | None = None) -> Version:
    """P1・P2・P3 は K120 を直に持つ。P1・P2 は組立品 U10 も持つ（中の変更は test_study_level）。"""
    v = Version("t")
    for code, name in (("P1", "製品1"), ("P2", "製品2"), ("P3", "製品3"), ("U10", "組立"), ("K120", "ブラケット"),
                       ("K121", "ブラケット改"), ("B1", "ボルト"), ("S50", "ステー"), ("Q9", "外の製品")):
        v.add_item(Item(code, name))
    v.add_bom_lines(lines if lines is not None else [
        BomLine("P1", "10", "U10", 1), BomLine("P1", "20", "S50", 1), BomLine("P1", "31", "B1", 4),
        BomLine("P1", "12", "K120", 2), BomLine("P2", "12", "K120", 2), BomLine("U10", "13", "B1", 4),
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
            ("P1", "P1", "12", "K120"): (Decimal(-2), REPLACE),
            ("P1", "P1", "12", "K121"): (Decimal(2), REPLACE),
            ("P2", "P2", "12", "K120"): (Decimal(-2), REPLACE),
            ("P2", "P2", "12", "K121"): (Decimal(2), REPLACE),
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
        # 置換は「元品番 × 風船番号」のまとまりで選ぶ（製品ごとに選ぶのは 部品変更の製品ごと・構成変更）
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
    def test_quantity_over_replace_does_not_add_and_stays_a_replace(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_rewrite(self.number, "A", "P2", [
            {"balloon": "10", "part": "U10", "quantity": "1"}, {"balloon": "20", "part": "S50", "quantity": "2"},
            {"balloon": "12", "part": "K121", "quantity": "3"}])
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "12", "part": "K121", "quantity": "5"}])
        got = self.changes()
        self.assertEqual(got[("P2", "P2", "12", "K121")], (Decimal(3), REWRITE))
        place = next(p for p in after_places(self.store.workspace(self.number), "A") if p.product == "P1" and p.part == "K121")
        self.assertEqual((place.quantity, place.was, place.before), (Decimal(5), ["K120"], Decimal(2)))  # before は置き換える前の数量
        self.assertEqual(got[("P1", "P1", "12", "K121")], (Decimal(5), REPLACE))
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "12", "part": "K121", "quantity": "2"}])
        got = self.changes()
        self.assertEqual(got[("P1", "P1", "12", "K121")], (Decimal(2), REPLACE))
        ws = self.store.workspace(self.number)
        self.assertEqual(replace_overridden(ws, "A"), {("K120", "K121", None): True})

    def test_replace_alone_is_not_marked(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.assertEqual(replace_overridden(self.store.workspace(self.number), "A"), {("K120", "K121", None): False})

    def test_running_replace_again_keeps_hand_edits(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_rewrite(self.number, "A", "P2", [
            {"balloon": "10", "part": "U10", "quantity": "1"}, {"balloon": "20", "part": "S50", "quantity": "2"},
            {"balloon": "12", "part": "K121", "quantity": "1"}])
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.assertEqual(self.changes()[("P2", "P2", "12", "K121")], (Decimal(1), REWRITE))

    def test_rewrite_over_quantity_replaces_the_amount(self) -> None:
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "20", "part": "S50", "quantity": "0"}])
        self.assertEqual(self.changes(), {("P1", "P1", "20", "S50"): (Decimal(-1), QUANTITY)})
        rows = [{"parent": "P1", "balloon": "10", "part": "U10", "quantity": "1"},
                {"parent": "P1", "balloon": "20", "part": "S50", "quantity": "3"},
                {"parent": "P1", "balloon": "31", "part": "B1", "quantity": "4"},
                {"balloon": "12", "part": "K120", "quantity": "2"}]  # 親を省けば製品の直下
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

    def test_empty_quantity_goes_back_to_the_bom(self) -> None:
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "20", "part": "S50", "quantity": "4"}])
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "20", "part": "S50", "quantity": ""}])
        self.assertEqual(self.changes(), {})

    def test_quantity_is_all_or_nothing(self) -> None:
        good = {"product": "P1", "balloon": "20", "part": "S50", "quantity": "4"}
        for bad in ({**good, "balloon": "99"}, {**good, "product": "Q9"}, {**good, "quantity": "-1"}, {**good, "quantity": "x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.save_quantities(self.number, "A", [good, bad])
            self.assertEqual(self.changes(), {})

    def test_add_part_to_many_products_at_once(self) -> None:
        self.store.add_part(self.number, "A", "K121", "2", ["P1", "P3"])
        self.assertEqual(self.changes(), {("P1", "P1", None, "K121"): (Decimal(2), ADD),
                                          ("P3", "P3", None, "K121"): (Decimal(2), ADD)})
        # 数量を直しても追加のまま・0 で無くなる
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": None, "part": "K121", "quantity": "3"},
                                                      {"product": "P3", "balloon": None, "part": "K121", "quantity": "0"}])
        self.assertEqual(self.changes(), {("P1", "P1", None, "K121"): (Decimal(3), ADD)})

    def test_add_part_refuses_and_changes_nothing(self) -> None:
        for part, quantity, products in (("X1", "1", ["P1"]), ("B1", "1", ["P3", "P1"]), ("K121", "0", ["P1"]), ("K121", "1", []),
                                         ("K121", "1", ["P1", "Q9"]), ("P1", "1", ["P1"])):
            with self.subTest(part=part, products=products), self.assertRaises(ValueError):
                self.store.add_part(self.number, "A", part, quantity, products)
            self.assertEqual(self.changes(), {})

    def test_added_part_is_not_taken_for_a_replacement_at_the_same_slot(self) -> None:
        # 風船番号の無い部品表: 1つ消して、別の1つを足す
        self.store.replace_bom(bom(lines=[BomLine("P1", None, "K120", 2), BomLine("P1", None, "S50", 1)]), ["bom_line"])
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": None, "part": "K120", "quantity": "0"}])
        self.store.add_part(self.number, "A", "B1", "4", ["P1"])
        ws = self.store.workspace(self.number)
        self.assertEqual([(p.part, p.quantity, p.was, p.before) for p in after_places(ws, "A") if p.product == "P1"], [
            ("K120", Decimal(0), [], Decimal(2)), ("S50", Decimal(1), [], Decimal(1)), ("B1", Decimal(4), [], None)])
        self.assertNotIn("K120", [p.code for p in after_parts(ws, "A")])
        rows = change_rows(ws)
        self.assertEqual([(s["kind"], s["old"], s["new"]) for s in summarize_changes(rows)], [("削除", "K120", None), ("追加", None, "B1")])
        # 同じ所で置き換えた組は、足した部品があっても組のまま・戻しても足した部品は残る
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": None, "part": "K120", "quantity": ""}])
        self.store.apply_replace(self.number, "A", "S50", "K121")
        kinds = [(s["kind"], s["old"], s["new"]) for s in summarize_changes(change_rows(self.store.workspace(self.number)))]
        self.assertEqual(sorted(kinds, key=str), sorted([("置き換え", "S50", "K121"), ("追加", None, "B1")], key=str))
        self.store.revert_replace(self.number, "A", "K121")
        self.assertEqual(self.changes(), {("P1", "P1", None, "B1"): (Decimal(4), ADD)})


def change_rows(ws: Workspace) -> list[dict]:
    """⑥ の行（summarize_changes に渡す形）。"""
    return [{"product": c.change.product, "parent": c.change.parent, "balloon": c.change.balloon, "part": c.change.part, "name": None,
             "before": str(c.before), "after": str(c.after), "delta": str(c.change.delta), "added": c.added,
             "problems": list(c.problems), "source": c.change.source} for c in check_changes(ws)]


class StructureRows(StudyCase):
    """⑤ 構成一覧・⑧ 構成変更の箇所。"""

    def slots(self, product: str) -> list[tuple]:
        found = structure(self.store.workspace(self.number), "A", [product])
        return [(s.balloon, s.now, s.part, s.before, s.quantity) for s in found.direct[product]]

    def test_every_place_has_the_current_and_the_after_side(self) -> None:
        one, two, five = Decimal(1), Decimal(2), Decimal(5)
        before = self.slots("P1")
        self.assertEqual({(b, n) for b, n, *_ in before}, {("10", "U10"), ("20", "S50"), ("31", "B1"), ("12", "K120")})
        self.assertTrue(all(now == part and x == y for _, now, part, x, y in before))
        self.store.apply_places(self.number, "A", [{"product": "P1", "balloon": "12", "old": "K120", "new": "K121"}])
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "20", "part": "S50", "quantity": "5"},
                                                      {"product": "P1", "balloon": "31", "part": "B1", "quantity": "0"}])
        self.store.add_places(self.number, "A", [{"product": "P1", "balloon": "31", "part": "K121", "quantity": "1"}])
        got = {(b, n): (p, x, y) for b, n, p, x, y in self.slots("P1")}
        self.assertEqual(got[("12", "K120")], ("K121", two, two))
        self.assertEqual(got[("20", "S50")], ("S50", one, five))
        self.assertEqual((got[("31", "B1")][0], got[("31", "B1")][2]), ("B1", Decimal(0)))
        self.assertEqual(got[("31", None)], ("K121", None, one))  # 足した部品は、消した部品の置き換えとは見ない

    def test_added_places_are_refused_when_they_are_there_or_unknown(self) -> None:
        row = {"product": "P1", "balloon": "77", "part": "K121", "quantity": "1"}
        for change, text in [({"part": "無い"}, "部品表にも仮品番にもありません"), ({"quantity": "0"}, "0より大きい"), ({"product": "Z"}, "対象品番にありません"),
                             ({"balloon": "12", "part": "K120"}, "もうあります"), ({"part": "P1"}, "自分の中")]:
            with self.subTest(text), self.assertRaisesRegex(ValueError, text):
                self.store.add_places(self.number, "A", [row, {**row, "balloon": "78", **change}])
        self.assertEqual(self.store.workspace(self.number).plan_changes("A"), [])
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "12", "part": "K120", "quantity": "0"}])
        self.store.add_places(self.number, "A", [{"product": "P1", "balloon": "12", "part": "K120", "quantity": "2"}])
        self.assertEqual(self.store.workspace(self.number).plan_changes("A"), [])  # 現行の数量に戻れば変更は無い

    def test_problems_are_marked(self) -> None:
        self.store.save_temps(self.number, [{"code": "ZZ1"}])
        self.store.add_part(self.number, "A", "ZZ1", "1", ["P1"])
        self.store.save_temps(self.number, [])
        self.assertEqual(structure(self.store.workspace(self.number), "A", ["P1"]).bad, {("P1", None, "ZZ1")})  # 未登録


class Checks(StudyCase):
    def problems(self) -> dict[tuple, tuple[str, ...]]:
        return {c.change.place: c.problems for c in check_changes(self.store.workspace(self.number))}

    def by_part(self, product: str, part: str) -> tuple[str, ...]:
        found = [c.problems for c in check_changes(self.store.workspace(self.number))
                 if c.change.product == product and c.change.part == part]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_same_checks_whichever_input_wrote(self) -> None:
        self.store.save_temps(self.number, [{"code": "X1"}])
        self.store.add_part(self.number, "A", "X1", "1", ["P1"])
        self.store.save_temps(self.number, [])  # 使った後に、仮品番が消えた
        self.store.save_targets(self.number, ["Q9", "P2"], None)
        self.store.save_quantities(self.number, "A", [{"product": "Q9", "balloon": "1", "part": "K120", "quantity": "2"}])
        self.store.save_targets(self.number, ["P1", "P2", "P3"], "P1")  # Q9 は対象品番でない製品になる
        self.store.save_rewrite(self.number, "A", "P2", [
            {"parent": "P2", "balloon": "10", "part": "U10", "quantity": "1"},
            {"parent": "P2", "balloon": "20", "part": "S50", "quantity": "-1"},
            {"balloon": "12", "part": "K120", "quantity": "2"}])
        got = self.problems()
        self.assertEqual(self.by_part("P1", "X1"), (UNREGISTERED,))
        self.assertEqual(got[("Q9", "Q9", "1", "K120")], (NOT_TARGET,))
        self.assertEqual(got[("P2", "P2", "20", "S50")], (NEGATIVE,))

    def test_reading_the_bom_again_finds_lost_and_changed_places(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "20", "part": "S50", "quantity": "0"}])
        changed = [BomLine("P1", "10", "U10", 1), BomLine("P1", "31", "B1", 4),
                   BomLine("P1", "12", "K125", 2), BomLine("P2", "12", "K120", 2), BomLine("U10", "13", "B1", 4),
                   BomLine("P2", "10", "U10", 1), BomLine("P2", "20", "S50", 2), BomLine("P3", "5", "K120", 1)]
        self.store.replace_bom(bom(lines=changed), ["bom_line"])
        got = self.problems()
        self.assertEqual(got[("P1", "P1", "12", "K120")], (OTHER_PART,))
        self.assertEqual(got[("P1", "P1", "20", "S50")], (NO_PLACE,))
        self.assertEqual(got[("P3", "P3", "5", "K120")], ())
        ws = self.store.workspace(self.number)
        result = calculate(ws, CostHead.MATERIAL)
        p1 = next(r for r in result["rows"] if r["product"] == "P1")
        # K120 の引きと S50 の引きは計算に入らない。置換の組の相手の K121 の足しも一緒に外れる
        self.assertEqual(p1["plans"]["A"], p1["now"])
        self.assertTrue(p1["incomplete"]["A"])

    def test_rewrite_takes_only_the_rows_right_under_the_product(self) -> None:
        with self.assertRaisesRegex(ValueError, "組立品の中"):
            self.store.save_rewrite(self.number, "A", "P1", [{"parent": "U10", "balloon": "13", "part": "B1", "quantity": "9"}])
        self.assertEqual(self.changes(), {})


class ApplyReplace(StudyCase):
    def test_replace_moves_the_after_quantity_in_every_target_and_undoes_by_replacing_back(self) -> None:
        self.store.apply_replace(self.number, "A", "K120", "K121")
        self.assertEqual(self.changes(), {
            ("P1", "P1", "12", "K120"): (Decimal(-2), REPLACE), ("P1", "P1", "12", "K121"): (Decimal(2), REPLACE),
            ("P2", "P2", "12", "K120"): (Decimal(-2), REPLACE), ("P2", "P2", "12", "K121"): (Decimal(2), REPLACE),
            ("P3", "P3", "5", "K120"): (Decimal(-1), REPLACE), ("P3", "P3", "5", "K121"): (Decimal(1), REPLACE)})
        self.assertEqual(self.store.workspace(self.number).replaces, {})  # 決まりとしては残さない
        parts = {p.code: (p.users, p.was) for p in after_parts(self.store.workspace(self.number), "A")}
        self.assertEqual(parts["K121"], (["P1", "P2", "P3"], ["K120"]))
        self.assertNotIn("K120", parts)
        # 後から直した数量も、そのまま置き換えに乗る（変えた後の構成で動く）
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "12", "part": "K121", "quantity": "3"}])
        self.store.apply_replace(self.number, "A", "K121", "K120")
        self.assertEqual(self.changes(), {("P1", "P1", "12", "K120"): (Decimal(1), REPLACE)})
        with self.assertRaises(ValueError):
            self.store.apply_replace(self.number, "A", "ZZ1", "K121")


    def test_revert_leaves_the_products_that_already_used_the_new_part(self) -> None:
        # 今の部品表で S50 を使う P1・P2 と、P3 の K120 を S50 に替えた所が1行にまとまる
        self.store.apply_replace(self.number, "A", "K120", "S50")
        self.assertEqual({p.code: p.users for p in after_parts(self.store.workspace(self.number), "A")}["S50"],
                         ["P1", "P2", "P3"])
        self.store.revert_replace(self.number, "A", "S50")
        self.assertEqual(self.changes(), {})
        with self.assertRaises(ValueError):
            self.store.revert_replace(self.number, "A", "S50")


class Summary(unittest.TestCase):
    @staticmethod
    def row(product: str, part: str, before: str | None, after: str, delta: str, balloon: str | None = "1", **more) -> dict:
        return {"product": product, "parent": "U", "balloon": balloon, "part": part, "name": part, "before": before,
                "after": after, "delta": delta, "added": before == "0", "problems": [], "source": QUANTITY, **more}

    def test_same_replacement_in_many_products_is_one_row(self) -> None:
        rows = [r for p, n in (("P1", "2"), ("P2", "2"), ("P3", "4")) for r in (
            self.row(p, "OLD", n, "0", f"-{n}"), self.row(p, "NEW", "0", n, n))]
        rows += [self.row("P1", "S", "2", "3", "1", "9"), self.row("P1", "X", "0", "1", "1", "8"),
                 self.row("P2", "Y", "1", "0", "-1", "7"),
                 self.row("P1", "加工費", "600", "700", "100", None, source="own")]
        got = [(s["kind"], s["old"], s["new"], s["quantity"], s["products"]) for s in summarize_changes(rows)]
        self.assertEqual(got, [("置き換え", "OLD", "NEW", "2（2製品）・4（1製品）", ["P1", "P2", "P3"]),
                               ("数量", "S", None, "2 → 3", ["P1"]), ("追加", None, "X", "1", ["P1"]),
                               ("削除", "Y", None, "1 → 0", ["P2"])])

    def test_by_product_lists_every_target_in_order_even_without_changes(self) -> None:
        rows = [self.row("P3", "OLD", "2", "0", "-2"), self.row("P3", "NEW", "0", "2", "2"),
                self.row("P1", "S", "2", "3", "1", "9"), self.row("P3", "S", "2", "4", "2", "9"),
                self.row("Q9", "Y", "1", "0", "-1", "7")]
        got = changes_by_product(summarize_changes(rows), ["P1", "P2", "P3"])
        self.assertEqual([(r["product"], r["kind"], r["old"], r["new"], r["quantity"]) for r in got], [
            ("P1", "数量", "S", None, "2 → 3"), ("P2", "変更なし", None, None, ""),
            ("P3", "置き換え", "OLD", "NEW", "2"), ("P3", "数量", "S", None, "2 → 4"),
            ("Q9", "削除", "Y", None, "1 → 0")])  # 対象品番の外の製品は後ろ

    def test_places_without_a_balloon_come_last_in_part_order(self) -> None:
        rows = [self.row("P1", "Z", "1", "2", "1", None), self.row("P1", "A", "1", "2", "1", None),
                self.row("P1", "X", "1", "2", "1", "10"), self.row("P1", "Y", "1", "2", "1", "9")]
        got = changes_by_product(summarize_changes(rows), ["P1"])
        self.assertEqual([(r["balloon"], r["old"]) for r in got], [("9", "Y"), ("10", "X"), (None, "A"), (None, "Z")])
        lines = {(None, "Z"): Decimal(1), (None, "A"): Decimal(1), ("10", "X"): Decimal(1), ("9", "Y"): Decimal(1)}
        got = _slot_rows(lines, {(None, "Z"): Decimal(1), (None, "B"): Decimal(1)})
        self.assertEqual([(r["balloon"], r["part"]) for r in got], [("9", "Y"), ("10", "X"), (None, "A"), (None, "B"), (None, "Z")])

    def test_ambiguous_or_lost_places_are_not_paired(self) -> None:
        two = [self.row("P1", "A", "1", "0", "-1"), self.row("P1", "B", "1", "0", "-1"), self.row("P1", "C", "0", "2", "2")]
        self.assertEqual([s["kind"] for s in summarize_changes(two)], ["削除", "削除", "追加"])
        partly = [self.row("P1", "A", "4", "2", "-2"), self.row("P1", "C", "0", "2", "2")]
        self.assertEqual([s["kind"] for s in summarize_changes(partly)], ["数量", "追加"])
        lost = [self.row("P1", "A", None, "-2", "-2", problems=["箇所が無い"]), self.row("P1", "C", "0", "2", "2")]
        self.assertEqual([(s["kind"], s["quantity"]) for s in summarize_changes(lost)], [("数量", "— → -2"), ("追加", "2")])


class Calculation(StudyCase):
    def test_now_and_plans_and_annual_impact(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_quantities(self.number, "B", [{"product": "P2", "balloon": "20", "part": "S50", "quantity": "1"}])
        result = calculate(self.store.workspace(self.number), CostHead.MATERIAL)
        rows = {r["product"]: r for r in result["rows"]}
        self.assertEqual(result["plans"], ["A", "B"])
        # P1 = U10(4×5) + 2×100 + 30 + 4×5
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
        self.store.save_temps(self.number, [{"code": "X1"}])
        self.store.add_part(self.number, "A", "X1", "1", ["P1"])
        result = calculate(self.store.workspace(self.number), CostHead.MATERIAL)
        self.assertTrue(result["rows"][0]["incomplete"]["A"])

    def test_total_head_uses_the_formula(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        result = calculate(self.store.workspace(self.number), CostHead.MANUFACTURING)
        p1 = result["rows"][0]
        # 製造原価 ＝ 材料費 × 1.1 × 1.2（直接費・償却費は未登録で 0）
        self.assertEqual(p1["now"], Decimal("356.4"))
        self.assertEqual(p1["plans"]["A"], Decimal("409.2"))

    def test_processing_and_amortization_heads_are_the_product_own_values(self) -> None:
        # 部品の置き換えで変わるのは材料費だけ。加工（加工費）は未登録なら 0、型費（償却費）は未登録なら空で不完全
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
        self.assertEqual(store.load_bom().items["P1"].lifecycle, "")
        conn.close()

    def test_lifecycle_is_kept_in_the_table_and_in_a_released_version(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = StudyStore(conn)
        v = bom()
        v.add_item(replace(v.items["K120"], lifecycle="D"))
        store.replace_bom(v, ALL)
        number = store.create_study("x")
        store.save_targets(number, ["P1"])
        store.take_release(number)
        self.assertEqual(store.workspace(number).bom.items["K120"].lifecycle, "D")
        self.assertEqual(store.workspace(number, 1).bom.items["K120"].lifecycle, "D")
        conn.close()


class WhereUsed(unittest.TestCase):
    def test_inside_and_outside_the_targets(self) -> None:
        self.assertEqual(where_used(bom(), "K120", ["P1", "P3"]), (["P1", "P3"], ["P2", "Q9"], 0))
        # 組立品の中でだけ使う製品は、件数だけ（P2 は U10 の中）
        self.assertEqual(where_used(bom(), "B1", ["P1", "P2", "P3"]), (["P1"], [], 1))


class StudyApiFlow(unittest.TestCase):
    """見本の CSV を取り込み、③→④→⑤→⑥→⑦→版確定 を API で通す。"""

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

    def test_main_product_is_chosen_on_the_list_and_kept_by_the_target_list(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        number = self.ok("POST", "/api/v2/studies", body={"products": [P1, P2]})["header"]["number"]
        q = f"number={number}"
        self.assertEqual(self.ok("PUT", "/api/v2/main-product", q, {"main_product": P2})["main_product"], P2)
        self.assertEqual(self.ok("GET", "/api/v2/structure", q + "&plan=A")["main_product"], P2)
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
        self.assertEqual(got["rows"], [{"code": tmp, "name": "仮の部品", "material": price, "master": False, "base": None,
                                        "changed": 0, "derived": None},
                                       {"code": NEW, "name": "台帳にある品番", "material": None, "master": True, "base": None,
                                        "changed": 0, "derived": None}])
        self.assertEqual(got["unregistered"], [])
        looked = self.ok("POST", "/api/v2/lookup", q, {"codes": [tmp, NEW]})["items"]
        self.assertEqual((looked[tmp]["name"], looked[tmp]["temp"], looked[NEW]["temp"]), ("仮の部品", True, False))
        with_temp = self.ok("GET", "/api/v2/calc", q + "&head=material")
        self.assertFalse(any(r["incomplete"]["A"] for r in with_temp["rows"]))
        self.ok("PUT", "/api/v2/replace", q + "&plan=A", {"rows": [{"old": OLD, "new": NEW}]})
        self.assertEqual(self.ok("GET", "/api/v2/calc", q + "&head=material")["impact"], with_temp["impact"])
        # 版確定で仮品番も固まる
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
        # 加工費の差は諸経費・間接費を通って製造原価に効く。費目全部の値は1つの費目で出した値と同じ
        total = Decimal(heads["total"]["rows"][0]["plans"]["A"]) - Decimal(heads["total"]["rows"][0]["now"])
        self.assertEqual(total, (Decimal(1000) - Decimal(now)) * Decimal("1.1") * Decimal("1.2"))
        single = self.ok("GET", "/api/v2/calc", q + "&head=total")
        self.assertEqual((single["rows"], single["impact"]), (heads["total"]["rows"], heads["total"]["impact"]))
        book = load_openpyxl().load_workbook(io.BytesIO(self.api.handle("GET", "/api/v2/export/study", q, b"").body))
        self.assertEqual(book["金額"].max_row, 1 + 2 * 8)  # 2製品×8費目
        money = [r for r in book["金額"].iter_rows(min_row=2, values_only=True) if r[0] == P1 and r[2] == "製造原価"][0]
        self.assertAlmostEqual(money[5], float(total))
        self.assertAlmostEqual(money[6], round(float(total) / money[3] * 100, 2))
        self.assertAlmostEqual(money[8], float(total) * money[7])
        self.assertIn(("製品の費目", P1, "加工費", 1000, f"台帳の値 {now}"),
                      list(book["計算条件"].iter_rows(values_only=True)))
        # 変更一覧にも出る
        changed = [r for r in self.ok("GET", "/api/v2/changes", q + "&plan=A")["rows"] if r["source"] == "own"]
        self.assertEqual([(r["product"], r["before"], r["after"]) for r in changed], [(P1, now, "1000")])
        # 版確定で固まる
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
        # 見本の品番は桁の変換をしない
        self.addCleanup(settings.use, example_settings.EXAMPLE)
        settings.use(replace(example_settings.EXAMPLE, code_total=None, code_base=None, code_branch=None))
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
        stay = next(p for p in self.ok("GET", "/api/v2/after-parts", q)["places"] if p["part"] == "ST0050-001" and p["product"] == P1)
        self.ok("POST", "/api/v2/quantity", q + "&plan=A", {"rows": [{**{k: stay[k] for k in ("product", "balloon", "part")}, "quantity": "0"}]})
        self.ok("POST", "/api/v2/add-part", q + "&plan=A", {"part": "PL0030-001", "quantity": "1", "products": [P2]})
        data = self.ok("GET", "/api/v2/structure", q + "&plan=A")
        # 図面が同じ対象品番だけ（無い品番は図面なし）。箇所は現行と変えた後の組
        self.assertEqual((data["drawing"], data["products"]), ("AS-01", [P1, P2]))
        self.assertEqual(data["drawings"], [{"drawing": "AS-01", "count": 2}, {"drawing": "", "count": 1}])
        cells = {(p, r["now"], r["part"]): (r["before"], r["quantity"]) for p, rows in data["direct"].items() for r in rows}
        self.assertEqual((cells[(P1, "ST0050-001", "ST0050-001")], cells[(P2, "ST0050-001", "ST0050-001")]), (("2", "0"), ("2", "2")))
        self.assertEqual((cells[(P1, OLD, NEW)], cells[(P2, None, "PL0030-001")]), (("1", "1"), (None, "1")))
        self.assertEqual([(r["balloon"], r["part"]) for r in data["inside"]["FR0010-001"]],
                         [("11", "HG0040-001"), ("12", "KB0120-001"), ("13", "BT0008-001"), ("14", "PL0030-001")])
        self.assertIn("HG0040-001", data["inside"])
        self.assertEqual(data["items"]["FR0010-001"], {"name": "フレーム ASSY", "specification": "", "lifecycle": "1", "temp": False})
        self.assertEqual(self.ok("GET", "/api/v2/structure", q + "&plan=A&blank=1")["products"], ["ZZ9999-001"])
        self.assertEqual(self.ok("GET", "/api/v2/structure", q + "&plan=A&all=1")["products"], [P1, P2, "ZZ9999-001"])
        changes = self.ok("GET", "/api/v2/changes", q + "&plan=A")["rows"]
        self.assertTrue(any(r["part"] == NEW and r["added"] for r in changes))
        before = self.store.connection.total_changes
        result = self.ok("GET", "/api/v2/calc", q + "&head=material")
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertEqual([r["product"] for r in result["rows"]], [P1, P2, "ZZ9999-001"])
        used = self.ok("GET", "/api/v2/where-used", q + f"&code={OLD}")
        self.assertEqual((used["inside"], used["outside"], used["deep"]), (2, 4, 0))
        released = self.ok("POST", "/api/v2/release", q, {})
        self.assertEqual((released["version"], released["versions"]), (1, [1]))
        self.assertEqual(self.ok("GET", "/api/v2/calc", q + "&version=1&head=material"), {
            **result, "computed_at": self.ok("GET", "/api/v2/calc", q + "&version=1&head=material")["computed_at"]})
        excel = self.api.handle("GET", "/api/v2/export/study", q)
        self.assertEqual(excel.status, 200)
        book = load_openpyxl().load_workbook(io.BytesIO(excel.body))
        self.assertEqual(book.sheetnames, ["まとめ", "変更ごとの影響", "構成の変更", "構成の明細", "金額", "計算条件"])
        # 構成の変更は、した事ごとに1行（2製品の置き換えが1行）・明細は箇所ごと
        self.assertIn(("置き換え", OLD, NEW, 2), [(r[0], r[1], r[3], r[6]) for r in
                                              book["構成の変更"].iter_rows(min_row=2, values_only=True)])
        # 構成の明細は製品から（画面の「製品から」と同じ行・変更の無い対象品番も出る）
        by_product = self.ok("GET", "/api/v2/changes", q + "&plan=A")["by_product"]
        self.assertEqual([(r[0], r[2]) for r in book["構成の明細"].iter_rows(min_row=2, values_only=True)],
                         [(r["product"], r["kind"]) for r in by_product])
        self.assertEqual(sorted(r["kind"] for r in self.ok("GET", "/api/v2/changes", q + f"&product={P2}")["by_product"]),
                         sorted(r["kind"] for r in by_product if r["product"] == P2))

    def test_replacing_the_common_part_keeps_every_product_positive(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        products = [p["code"] for p in self.ok("GET", "/api/v2/products")["products"]]
        self.assertEqual(len(products), 10)
        number = self.ok("POST", "/api/v2/studies", body={})["header"]["number"]
        q = f"number={number}"
        self.ok("PUT", "/api/v2/targets", q, {"products": products})
        self.ok("PUT", "/api/v2/replace", q + "&plan=A", {"rows": [{"old": OLD, "new": NEW}]})
        changes = self.ok("GET", "/api/v2/changes", q + "&plan=A")["rows"]
        users = {"PR1001-001", "PR1002-001", "PR1003-001", "PR1004-001", "PR1501-001", "PR1502-001"}
        self.assertEqual({r["product"] for r in changes}, users)
        self.assertTrue(all(not r["problems"] for r in changes))
        for head in ("total", "material"):
            result = self.ok("GET", "/api/v2/calc", q + f"&head={head}")
            for row in result["rows"]:
                with self.subTest(head=head, product=row["product"]):
                    self.assertGreater(Decimal(row["now"]), 0)
                    self.assertGreater(Decimal(row["plans"]["A"]), 0)
                    if row["product"] in users:
                        self.assertLess(Decimal(row["plans"]["A"]), Decimal(row["now"]))  # 置き換え先が安い
                    else:
                        self.assertEqual(row["plans"]["A"], row["now"])
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
        # 2) 同じ風船番号で製品ごとに品番が違う（同じ図面）
        self.assertEqual(balloons("PR1004-001", "ST0051-001"), {("PR1004-001", "20")})
        # 3) 1つの製品の中で同じ品番・風船番号が別の親の下に出る
        self.assertEqual({p for p, b in balloons(P1, "BT0008-001") if b == "13"}, {"FR0010-001", "HG0040-001"})
        # 4) ある製品にしか無い部品
        users = {p: where_used(v, p, [])[1] for p in ("HT0090-001", "CV0230-001")}
        self.assertEqual(users, {"HT0090-001": ["PR1002-001"], "CV0230-001": ["PR1503-001"]})
        # 5) 多くの箇所・製品で使う部品
        self.assertEqual([len(where_used(v, "BT0008-001", [])[1]), where_used(v, "BT0008-001", [])[2]], [9, 1])
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
        preview = self.ok("POST", "/api/v2/bom/preview", body={"filename": f["filename"], "content": f["content"]})
        self.assertEqual(preview["suggested"], "item")  # 決定で列の対応を保存し、次の同じ見出しで出る
        self.assertEqual(preview["tables"][0]["saved"], f["columns"])

    @staticmethod
    def csv(name: str, text: str, **more: object) -> dict[str, object]:
        return {"filename": name, "content": base64.b64encode(text.encode("utf-8")).decode(), **more}

    def refused(self, *files: dict[str, object]) -> str:
        response = self.api.handle("POST", "/api/v2/bom/import", "", json.dumps({"files": files}, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(response.status, 400)
        return response.json()["error"]

    def test_import_offers_only_the_fields_in_use(self) -> None:
        preview = self.ok("POST", "/api/v2/bom/preview", body=self.csv("品目.csv", "品番,品名,系列\nA,a,X\n"))
        fields = {d["id"]: [f["name"] for f in d["fields"]] for d in preview["tables"]}
        self.assertEqual(fields, {
            "item": ["code", "name", "specification", "lifecycle", "model", "product_spec", "drawing"],
            "bom_line": ["parent", "child", "quantity", "balloon"],
            "cost": ["mbom_code", "material", "direct", "amortization", "processing"],
            "volume": ["product", "annual"]})
        self.assertEqual(preview["suggested"], "item")
        self.assertEqual(preview["tables"][0]["guess"], {"code": "品番", "name": "品名", "model": "系列"})
        self.assertIn("level", self.refused(self.csv("構成.csv", "a,b,c,d,e\n", table="bom_line", columns={
            "parent": "a", "child": "b", "quantity": "c", "balloon": "d", "level": "e"})))

    def test_attribute_columns_are_chosen_on_the_screen(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"files": [self.csv(
            "品目.csv", "品番,品名,かたち\nA-100,製品A,ZZ\n", table="item", columns={"code": "品番", "name": "品名", "model": "かたち"})]})
        self.assertEqual(self.ok("POST", "/api/v2/lookup", body={"codes": ["A-100"]})["items"]["A-100"]["model"], "ZZ")

    def test_structure_needs_a_balloon_column_with_blank_cells_allowed(self) -> None:
        plain = "親品番,子品番,数量\nP,B,4\n"
        self.refused(self.csv("構成.csv", plain))
        self.assertIn("風船番号", self.refused(self.csv("構成.csv", plain, table="bom_line", columns={
            "parent": "親品番", "child": "子品番", "quantity": "数量"})))
        for header in ("風船番号", "風船", "バルーンNo", "ﾊﾞﾙｰﾝ番号", "符号", "照合番号"):
            r = self.ok("POST", "/api/v2/bom/import", body={"files": [self.csv(
                "構成.csv", f"親品番,{header},子品番,数量\nP,1,B,4\nP,2,B,4\nP,,C,1\n")], "check": True})
            self.assertEqual((r["counts"], r["conflicts"], r["warnings"]), ({"bom_line": 3}, [], []), header)

    def test_different_quantities_for_the_same_place_are_shown_in_the_check(self) -> None:
        body = {"files": [self.csv("構成.csv", "親品番,風船番号,子品番,数量\nP,1,B,4\nP,1,B,2\nP,1,B,4\nP,,C,1\nP,,C,1\n")], "check": True}
        r = self.ok("POST", "/api/v2/bom/import", body=body)
        self.assertEqual(r["conflicts"], [{"parent": "P", "balloon": "1", "child": "B", "quantities": ["4", "2"]}])
        # 風船番号が空の同じ親子は、展開の繰り返しか2か所かを見分けられないので知らせる
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("1 組あり、1行分の数量で取り込みます（P の C）", r["warnings"][0])

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
        csv = f"品番,品名,系列\n{P1},製品A,ZZ\n".encode("utf-8")
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
