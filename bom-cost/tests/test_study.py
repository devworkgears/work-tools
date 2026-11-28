"""設計検討: どの画面からの変更も1つの箇所の集まりに書くこと・後勝ち・読み直しの確認・計算・版。データは架空。"""

import base64
import io
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import example_settings
from test_study_level import lose_temps
from bomcost import settings
from bomcost.api import Api
from bomcost.importer import load_openpyxl
from bomcost.model import BomLine, CostHead, CostRow, Item, Version, Volume
from bomcost.store import Store
from bomcost.structure import structure
from bomcost.study import (
    after_places,
    calculate,
    change_effect,
    check_changes,
    HEADS,
    MOVED,
    NO_PLACE,
    NOT_TARGET,
    OTHER_PART,
    Prices,
    product_lines,
    StudyStore,
    UNREGISTERED,
    where_used,
    Workspace,
)
from bomcost.studyapi import _change_row, changes_by_product, summarize_changes

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

    def changes(self, plan: str = "A") -> dict[tuple, tuple[str, str]]:
        """（製品, 風船番号, 現行の品番）→（変えた後の品番, 数量）。足した箇所の現行の品番は None。"""
        ws = self.store.workspace(self.number)
        found = [((c.product, c.slot.balloon, c.slot.now), (c.slot.part, str(c.slot.quantity))) for c in ws.plan_changes(plan)]
        self.assertEqual(len(dict(found)), len(found))
        return dict(found)

    def quantity(self, product: str, balloon: str | None, part: str, quantity: str, **more: object) -> None:
        self.store.save_quantities(self.number, "A", [{"product": product, "balloon": balloon, "part": part, "quantity": quantity, **more}])

    def swap(self, product: str, balloon: str | None, old: str, new: str, **more: object) -> None:
        self.store.apply_places(self.number, "A", [{"product": product, "balloon": balloon, "old": old, "new": new, **more}])

    def back(self, product: str, balloon: str | None, now: str | None, part: str) -> None:
        self.store.back_places(self.number, "A", [{"product": product, "balloon": balloon, "now": now, "part": part}])

    def take(self, lines: list[BomLine]) -> None:
        """部品表を取り直す。"""
        self.store.replace_bom(bom(lines=lines), ["bom_line"])

    def placed(self, product: str) -> list[tuple]:
        """（風船番号, 現行の品番, 変えた後の品番, 数量）。"""
        return [(p.balloon, p.now, p.part, str(p.quantity)) for p in self.store.workspace(self.number).placed("A", product)]


class Numbering(StudyCase):
    def test_number_is_given_on_create_and_plan_a_exists(self) -> None:
        self.assertEqual(self.number, "D-0001")
        self.assertEqual(self.store.create_study(), "D-0002")
        self.assertEqual(self.store.workspace(self.number).plan_ids(), ["A"])

    def test_unknown_number_is_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.store.workspace("D-9999")


class Edits(StudyCase):
    def test_replace_in_every_target_and_back(self) -> None:
        self.store.apply_replace(self.number, "A", "K120", "K121")
        self.assertEqual(self.changes(), {("P1", "12", "K120"): ("K121", "2"), ("P2", "12", "K120"): ("K121", "2"),
                                          ("P3", "5", "K120"): ("K121", "1")})
        # 後から直した数量も、そのまま置き換えに乗る
        self.quantity("P1", "12", "K121", "3")
        self.assertEqual(self.changes()[("P1", "12", "K120")], ("K121", "3"))
        place = next(p for p in after_places(self.store.workspace(self.number), "A") if p.product == "P1" and p.part == "K121")
        self.assertEqual((place.quantity, place.was, place.before), (Decimal(3), ["K120"], Decimal(2)))  # before は置き換える前の数量
        self.store.apply_replace(self.number, "A", "K121", "K120")
        self.assertEqual(self.changes(), {("P1", "12", "K120"): ("K120", "3")})
        self.quantity("P1", "12", "K120", "")  # 空は現行の数量
        self.assertEqual(self.changes(), {})
        with self.assertRaises(ValueError):
            self.store.apply_replace(self.number, "A", "ZZ1", "K121")
        self.store.apply_replace(self.number, "A", "K120", "K120")
        self.assertEqual(self.changes(), {})

    def test_plans_are_separate(self) -> None:
        self.store.apply_replace(self.number, "B", "S50", "K121")
        self.assertEqual(self.changes("A"), {})
        self.assertEqual(set(self.changes("B")), {("P1", "20", "S50"), ("P2", "20", "S50")})
        self.assertEqual(self.store.workspace(self.number).plan_ids(), ["A", "B"])

    def test_later_write_wins_at_the_same_place(self) -> None:
        self.quantity("P1", "12", "K120", "5")
        self.swap("P1", "12", "K120", "K121")
        self.assertEqual(self.changes(), {("P1", "12", "K120"): ("K121", "5")})
        self.swap("P1", "12", "K121", "S50")
        self.quantity("P1", "12", "S50", "1")
        self.assertEqual(self.changes(), {("P1", "12", "K120"): ("S50", "1")})
        # 置き換えた箇所を 0 にすれば、現行の品番を消したことになる
        self.quantity("P1", "12", "S50", "0")
        self.assertEqual(self.changes(), {("P1", "12", "K120"): ("K120", "0")})

    def test_empty_quantity_goes_back_to_the_bom(self) -> None:
        self.quantity("P1", "20", "S50", "4")
        self.quantity("P1", "20", "S50", "")
        self.assertEqual(self.changes(), {})

    def test_quantity_is_all_or_nothing(self) -> None:
        good = {"product": "P1", "balloon": "20", "part": "S50", "quantity": "4"}
        for bad in ({**good, "balloon": "99"}, {**good, "product": "Q9"}, {**good, "quantity": "-1"}, {**good, "quantity": "x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.save_quantities(self.number, "A", [good, bad])
            self.assertEqual(self.changes(), {})

    def test_add_part_to_many_products_at_once(self) -> None:
        self.store.add_part(self.number, "A", "K121", "2", ["P1", "P3"])
        self.assertEqual(self.changes(), {("P1", None, None): ("K121", "2"), ("P3", None, None): ("K121", "2")})
        # 数量を直しても追加のまま・0 で無くなる
        self.quantity("P1", None, "K121", "3")
        self.quantity("P3", None, "K121", "0")
        self.assertEqual(self.changes(), {("P1", None, None): ("K121", "3")})
        self.back("P1", None, None, "K121")
        self.assertEqual(self.changes(), {})

    def test_add_part_refuses_and_changes_nothing(self) -> None:
        for part, quantity, products in (("X1", "1", ["P1"]), ("B1", "1", ["P3", "P1"]), ("K121", "0", ["P1"]), ("K121", "1", []),
                                         ("K121", "1", ["P1", "Q9"]), ("P1", "1", ["P1"])):
            with self.subTest(part=part, products=products), self.assertRaises(ValueError):
                self.store.add_part(self.number, "A", part, quantity, products)
            self.assertEqual(self.changes(), {})

    def test_a_product_cannot_hold_itself(self) -> None:
        # 製品を、自分の中や、自分を含む組立品に置き換えると、計算が止まっていた
        self.store.replace_bom(bom(lines=[BomLine("P1", "1", "K120", 1), BomLine("U10", "1", "P1", 1)]), ["bom_line"])
        for new in ("P1", "U10"):
            with self.subTest(new=new), self.assertRaisesRegex(ValueError, "中には入れられません"):
                self.swap("P1", "1", "K120", new)
            with self.subTest(new=new), self.assertRaisesRegex(ValueError, "中には入れられません"):
                self.store.add_places(self.number, "A", [{"product": "P1", "balloon": "2", "part": new, "quantity": "1"}])
        self.assertEqual(self.changes(), {})

    def test_full_width_codes_in_the_bom_can_be_pointed_at(self) -> None:
        # 部品表に全角・丸数字のまま入っている品番と風船番号
        v = bom(lines=[BomLine("Ｐ９", "①", "ＡＢ", 2), BomLine("Ｐ９", "②", "S50", 1)])
        for code in ("Ｐ９", "ＡＢ"):
            v.add_item(Item(code, code))
        self.store.replace_bom(v, ALL)
        self.store.save_targets(self.number, ["Ｐ９"], "Ｐ９")
        self.assertEqual(self.store.workspace(self.number).main_product, "Ｐ９")
        self.quantity("Ｐ９", "①", "ＡＢ", "3")
        self.swap("Ｐ９", "②", "S50", "ＡＢ")
        self.assertEqual(self.changes(), {("Ｐ９", "①", "ＡＢ"): ("ＡＢ", "3"), ("Ｐ９", "②", "S50"): ("ＡＢ", "1")})
        self.store.save_conditions(self.number, {"Ｐ９": "10"})
        self.store.save_owns(self.number, "A", [{"product": "Ｐ９", "head": "direct", "value": "5"}])
        ws = self.store.workspace(self.number)
        self.assertEqual((ws.conditions, set(ws.owns["A"])), ({"Ｐ９": Decimal(10)}, {("Ｐ９", "direct")}))


class SamePlace(StudyCase):
    """同じ風船番号の2つの箇所。"""

    def setUp(self) -> None:
        super().setUp()
        self.take([BomLine("P1", "1", "K120", 2), BomLine("P1", "1", "S50", 3)])

    def test_two_places_may_become_the_same_part(self) -> None:
        self.swap("P1", "1", "K120", "B1")
        self.swap("P1", "1", "S50", "B1")
        self.assertEqual(self.changes(), {("P1", "1", "K120"): ("B1", "2"), ("P1", "1", "S50"): ("B1", "3")})
        row = calculate(self.store.workspace(self.number), CostHead.MATERIAL)["rows"][0]
        self.assertEqual((row["now"], row["plans"]["A"]), (Decimal(290), Decimal(25)))
        # 品番だけで見ると1つに見え、どちらの箇所かは構成変更で指す
        places = [p for p in after_places(self.store.workspace(self.number), "A") if p.product == "P1"]
        self.assertEqual([(p.part, p.quantity, p.was, p.before) for p in places], [("B1", Decimal(5), ["K120", "S50"], Decimal(5))])
        with self.assertRaisesRegex(ValueError, "構成変更"):
            self.quantity("P1", "1", "B1", "9")
        self.quantity("P1", "1", "B1", "9", now="S50")
        self.assertEqual(self.changes()[("P1", "1", "S50")], ("B1", "9"))
        self.back("P1", "1", "K120", "B1")
        self.assertEqual(self.changes(), {("P1", "1", "S50"): ("B1", "9")})

    def test_replacing_into_the_part_that_is_already_there(self) -> None:
        self.swap("P1", "1", "K120", "S50")
        self.assertEqual(self.placed("P1"), [("1", "K120", "S50", "2"), ("1", "S50", "S50", "3")])
        self.quantity("P1", "1", "S50", "7", now="K120")
        self.assertEqual(self.placed("P1"), [("1", "K120", "S50", "7"), ("1", "S50", "S50", "3")])
        self.back("P1", "1", "K120", "S50")
        self.assertEqual(self.changes(), {})

    def test_the_two_parts_can_be_exchanged(self) -> None:
        self.store.apply_places(self.number, "A", [{"product": "P1", "balloon": "1", "old": "K120", "new": "S50"},
                                                   {"product": "P1", "balloon": "1", "old": "S50", "new": "K120"}])
        self.assertEqual(self.placed("P1"), [("1", "K120", "S50", "2"), ("1", "S50", "K120", "3")])
        row = calculate(self.store.workspace(self.number), CostHead.MATERIAL)["rows"][0]
        self.assertEqual((row["now"], row["plans"]["A"]), (Decimal(290), Decimal(360)))
        kinds = [(s["kind"], s["old"], s["new"], s["quantity"]) for s in summarize_changes(change_rows(self.store.workspace(self.number)))]
        self.assertEqual(kinds, [("置き換え", "K120", "S50", "2"), ("置き換え", "S50", "K120", "3")])

    def test_delete_one_and_add_another_is_not_a_replacement(self) -> None:
        self.quantity("P1", "1", "K120", "0")
        self.store.add_places(self.number, "A", [{"product": "P1", "balloon": "1", "part": "B1", "quantity": "4"}])
        self.assertEqual([(s["kind"], s["old"], s["new"]) for s in summarize_changes(change_rows(self.store.workspace(self.number)))],
                         [("削除", "K120", None), ("追加", None, "B1")])
        self.assertEqual([(p.part, p.quantity, p.was, p.before) for p in after_places(self.store.workspace(self.number), "A") if p.product == "P1"], [
            ("K120", Decimal(0), [], Decimal(2)), ("S50", Decimal(3), [], Decimal(3)), ("B1", Decimal(4), [], None)])
        # 足した部品を置き換えても、追加のまま
        self.swap("P1", "1", "B1", "K121")
        self.assertEqual(self.changes(), {("P1", "1", "K120"): ("K120", "0"), ("P1", "1", None): ("K121", "4")})
        # 消した品番に戻せば、その箇所の数量になる
        self.swap("P1", "1", "K121", "K120")
        self.assertEqual(self.changes(), {("P1", "1", "K120"): ("K120", "4")})

    def test_no_quantity_is_lost_when_an_added_part_meets_the_original(self) -> None:
        self.swap("P1", "1", "K120", "K121")
        self.store.add_places(self.number, "A", [{"product": "P1", "balloon": "1", "part": "K120", "quantity": "1"}])
        self.assertEqual(self.placed("P1"), [("1", "K120", "K121", "2"), ("1", "S50", "S50", "3"), ("1", None, "K120", "1")])
        self.back("P1", "1", "K120", "K121")
        self.assertEqual(self.changes(), {("P1", "1", "K120"): ("K120", "3")})
        # 足した部品を、もうある部品に置き換えた時も足す
        self.store.add_places(self.number, "A", [{"product": "P1", "balloon": "1", "part": "B1", "quantity": "1"}])
        self.swap("P1", "1", "B1", "S50")
        self.assertEqual(self.changes(), {("P1", "1", "K120"): ("K120", "3"), ("P1", "1", "S50"): ("S50", "4")})


def change_rows(ws: Workspace) -> list[dict]:
    """⑥ の行（summarize_changes に渡す形）。"""
    return [_change_row(ws, c) for c in check_changes(ws)]


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
        lose_temps(self.store, self.number)
        self.assertEqual(structure(self.store.workspace(self.number), "A", ["P1"]).bad, {("P1", None, None, "ZZ1")})  # 未登録


class Checks(StudyCase):
    def problems(self) -> dict[tuple, tuple[str, ...]]:
        return {(c.change.product, c.change.slot.balloon, c.change.slot.now or c.change.slot.part): c.problems
                for c in check_changes(self.store.workspace(self.number))}

    def material(self, product: str) -> tuple[Decimal, Decimal, bool]:
        rows = calculate(self.store.workspace(self.number), CostHead.MATERIAL)["rows"]
        r = next(r for r in rows if r["product"] == product)
        return r["now"], r["plans"]["A"], r["incomplete"]["A"]

    def test_same_checks_whichever_input_wrote(self) -> None:
        self.store.save_temps(self.number, [{"code": "X1"}])
        self.store.add_part(self.number, "A", "X1", "1", ["P1"])
        lose_temps(self.store, self.number)  # 使った後に、仮品番が消えた
        self.store.save_targets(self.number, ["Q9", "P2"], None)
        self.quantity("Q9", "1", "K120", "2")
        self.store.save_targets(self.number, ["P1", "P2", "P3"], "P1")  # Q9 は対象品番でない製品になる
        self.assertEqual(self.problems(), {("P1", None, "X1"): (UNREGISTERED,), ("Q9", "1", "K120"): (NOT_TARGET,)})

    def test_reading_the_bom_again_finds_lost_and_changed_places(self) -> None:
        self.store.apply_replace(self.number, "A", "K120", "K121")
        self.quantity("P1", "20", "S50", "0")
        self.take([BomLine("P1", "10", "U10", 1), BomLine("P1", "31", "B1", 4),
                   BomLine("P1", "12", "B1", 2), BomLine("P2", "12", "K120", 2), BomLine("U10", "13", "B1", 4),
                   BomLine("P2", "10", "U10", 1), BomLine("P2", "20", "S50", 2), BomLine("P3", "5", "K120", 1)])
        self.assertEqual(self.problems(), {("P1", "12", "K120"): (OTHER_PART,), ("P1", "20", "S50"): (NO_PLACE,),
                                           ("P2", "12", "K120"): (), ("P3", "5", "K120"): ()})
        # 現行の品番が無くなった変更は計算に入らない
        now, after, incomplete = self.material("P1")
        self.assertEqual((after, incomplete), (now, True))
        # 構成変更に残って見え、［戻す］で消せる
        lost = [p for p in structure(self.store.workspace(self.number), "A", ["P1"]).direct["P1"] if p.lost]
        self.assertEqual([(p.now, p.part) for p in lost], [("K120", "K121"), ("S50", "S50")])
        self.back("P1", "12", "K120", "K121")
        self.back("P1", "20", "S50", "S50")
        self.assertEqual(self.material("P1"), (now, now, False))

    def test_a_changed_quantity_in_the_bom_keeps_the_typed_quantity(self) -> None:
        # 2 → 1 に減らした後、部品表の数量が 5 になった。前は増減で持っていたので 4 になっていた
        self.quantity("P1", "12", "K120", "1")
        self.swap("P2", "12", "K120", "K121")
        self.take([BomLine("P1", "12", "K120", 5), BomLine("P2", "12", "K120", 3)])
        self.assertEqual(self.placed("P1"), [("12", "K120", "K120", "1")])
        self.assertEqual(self.placed("P2"), [("12", "K120", "K121", "2")])
        self.assertEqual(set(self.problems().values()), {(MOVED,)})
        # 計算には入れる
        self.assertEqual(self.material("P1"), (Decimal(500), Decimal(100), False))
        self.assertEqual(self.material("P2"), (Decimal(300), Decimal(240), False))
        self.quantity("P1", "12", "K120", "1")  # 打ち直せば、今の部品表を見て打ったことになる
        self.assertEqual(self.problems()[("P1", "12", "K120")], ())

    def test_an_added_part_that_came_into_the_bom_is_not_doubled(self) -> None:
        self.store.add_places(self.number, "A", [{"product": "P3", "balloon": "7", "part": "S50", "quantity": "2"}])
        self.take([BomLine("P3", "5", "K120", 1), BomLine("P3", "7", "S50", 1)])
        self.assertEqual(self.placed("P3"), [("5", "K120", "K120", "1"), ("7", "S50", "S50", "2")])
        self.assertEqual(self.problems(), {("P3", "7", "S50"): (MOVED,)})
        self.assertEqual(self.material("P3"), (Decimal(130), Decimal(160), False))
        rows = summarize_changes(change_rows(self.store.workspace(self.number)))
        self.assertEqual([(s["kind"], s["old"], s["quantity"]) for s in rows], [("数量", "S50", "1 → 2")])
        self.back("P3", "7", "S50", "S50")
        self.assertEqual(self.changes(), {})

    def test_an_unpriced_replacement_is_held_next_to_other_changes(self) -> None:
        # 同じ風船番号に、単価の無い品番への置き換えと、ただの削除。前は組を取り違えていた
        self.store.save_temps(self.number, [{"code": "ZZ"}])
        self.take([BomLine("P1", "1", "K120", 2), BomLine("P1", "1", "S50", 2)])
        self.quantity("P1", "1", "K120", "0")
        self.swap("P1", "1", "S50", "ZZ")
        now, after, incomplete = self.material("P1")
        self.assertEqual((now, after, incomplete), (Decimal(260), Decimal(60), True))  # S50 は現行のまま・K120 は引く

    def test_an_assembly_with_an_unpriced_part_inside_is_unpriced(self) -> None:
        # 中の一部の単価が分からない組立品は、分かる分だけの値で置き換えない
        v = bom(lines=[BomLine("P1", "1", "S50", 1), BomLine("U10", "1", "B1", 1), BomLine("U10", "2", "Q9", 1)])
        self.store.replace_bom(v, ["bom_line"])
        self.swap("P1", "1", "S50", "U10")
        self.assertEqual(self.material("P1"), (Decimal(30), Decimal(30), True))

    def test_a_loop_in_the_bom_marks_the_product_and_does_not_stop(self) -> None:
        self.take([BomLine("P1", "1", "U10", 1), BomLine("U10", "1", "Q9", 1), BomLine("Q9", "1", "U10", 1), BomLine("P3", "5", "K120", 1)])
        self.quantity("P1", "1", "U10", "2")
        self.quantity("P3", "5", "K120", "2")
        rows = {r["product"]: r for r in calculate(self.store.workspace(self.number), CostHead.MATERIAL)["rows"]}
        self.assertEqual((rows["P1"]["plans"]["A"], rows["P1"]["incomplete"]["A"], rows["P3"]["plans"]["A"]), (None, True, Decimal(200)))
        ws = self.store.workspace(self.number)
        self.assertEqual(change_effect(ws, check_changes(ws)[0], Prices(ws.bom)), (None, True))

    def test_products_out_of_the_targets_keep_their_values(self) -> None:
        self.store.save_owns(self.number, "A", [{"product": "P1", "head": "direct", "value": "5"}, {"product": "P2", "head": "direct", "value": "6"}])
        self.store.save_conditions(self.number, {"P1": "10", "P2": "20"})
        self.store.save_targets(self.number, ["P2", "P3"])
        self.store.save_owns(self.number, "A", [{"product": "P3", "head": "direct", "value": "7"}])
        self.store.save_conditions(self.number, {"P3": "30"})
        self.store.save_targets(self.number, ["P1", "P2", "P3"])
        ws = self.store.workspace(self.number)
        self.assertEqual(ws.owns["A"], {("P1", "direct"): Decimal(5), ("P3", "direct"): Decimal(7)})
        self.assertEqual(ws.conditions, {"P1": Decimal(10), "P3": Decimal(30)})
        for call in (lambda: self.store.save_conditions(self.number, {"P1": "-1"}),
                     lambda: self.store.save_temps(self.number, [{"code": "ZZ", "material": "-1"}])):
            with self.assertRaisesRegex(ValueError, "0以上"):
                call()

    def test_temps_made_in_another_window_are_kept(self) -> None:
        self.store.save_temps(self.number, [{"code": "T1"}])
        self.store.save_temps(self.number, [{"code": "T1"}, {"code": "T2"}])  # ほかの窓で T2 ができた
        self.store.save_temps(self.number, [{"code": "T1", "name": "名"}], known=["T1"])
        self.assertEqual([(t.code, t.name) for t in self.store.workspace(self.number).temps], [("T1", "名"), ("T2", "")])
        with self.assertRaisesRegex(ValueError, "ほかの窓"):
            self.store.save_temps(self.number, [{"code": "T1"}, {"code": "T2"}], known=["T1"])
        self.store.save_temps(self.number, [{"code": "T1"}], known=["T1", "T2"])  # 画面で消した
        self.assertEqual([t.code for t in self.store.workspace(self.number).temps], ["T1"])


class LostSettings(StudyCase):
    def test_a_missing_settings_file_does_not_drop_the_bom(self) -> None:
        # settings.json を置き忘れて起動しただけで、取り込んだ部品表が消えていた
        self.addCleanup(settings.use, example_settings.EXAMPLE)
        settings.use(example_settings.EXAMPLE)
        self.store.drop_old_bom()
        settings.use(settings.Settings())
        self.assertFalse(self.store.drop_old_bom())
        self.assertTrue(self.store.load_bom().lines)
        settings.use(example_settings.EXAMPLE)
        self.assertFalse(self.store.drop_old_bom())
        settings.use(replace(example_settings.EXAMPLE, code_total=10))
        self.assertTrue(self.store.drop_old_bom())
        self.assertFalse(self.store.load_bom().lines)


class Summary(unittest.TestCase):
    @staticmethod
    def row(kind: str, product: str, old: str | None, new: str | None, before: str | None, after: str, balloon: str | None = "1", **more) -> dict:
        return {"kind": kind, "product": product, "parent": product, "balloon": balloon, "part": new or old, "old": old, "old_name": None,
                "new": new, "new_name": None, "before": before, "after": after, "problems": [], "source": "slot", "key": None, **more}

    def test_same_replacement_in_many_products_is_one_row(self) -> None:
        rows = [self.row("置き換え", p, "OLD", "NEW", n, n) for p, n in (("P1", "2"), ("P2", "2"), ("P3", "4"))]
        rows += [self.row("数量", "P1", "S", None, "2", "3", "9"), self.row("追加", "P1", None, "X", "0", "1", "8"),
                 self.row("削除", "P2", "Y", None, "1", "0", "7"), self.row("置き換え", "P2", "A", "B", "2", "3", "6"),
                 {"product": "P1", "source": "own"}]
        got = [(s["kind"], s["old"], s["new"], s["quantity"], s["products"]) for s in summarize_changes(rows)]
        self.assertEqual(got, [("置き換え", "OLD", "NEW", "2（2製品）・4（1製品）", ["P1", "P2", "P3"]),
                               ("数量", "S", None, "2 → 3", ["P1"]), ("追加", None, "X", "1", ["P1"]),
                               ("削除", "Y", None, "1 → 0", ["P2"]), ("置き換え", "A", "B", "2 → 3", ["P2"])])

    def test_by_product_lists_every_target_in_order_even_without_changes(self) -> None:
        rows = [self.row("置き換え", "P3", "OLD", "NEW", "2", "2"), self.row("数量", "P1", "S", None, "2", "3", "9"),
                self.row("数量", "P3", "S", None, "2", "4", "9"), self.row("削除", "Q9", "Y", None, "1", "0", "7")]
        got = changes_by_product(summarize_changes(rows), ["P1", "P2", "P3"])
        self.assertEqual([(r["product"], r["kind"], r["old"], r["new"], r["quantity"]) for r in got], [
            ("P1", "数量", "S", None, "2 → 3"), ("P2", "変更なし", None, None, ""),
            ("P3", "置き換え", "OLD", "NEW", "2"), ("P3", "数量", "S", None, "2 → 4"),
            ("Q9", "削除", "Y", None, "1 → 0")])  # 対象品番の外の製品は後ろ

    def test_places_without_a_balloon_come_last_in_part_order(self) -> None:
        rows = [self.row("数量", "P1", p, None, "1", "2", b) for p, b in (("Z", None), ("A", None), ("X", "10"), ("Y", "9"))]
        got = changes_by_product(summarize_changes(rows), ["P1"])
        self.assertEqual([(r["balloon"], r["old"]) for r in got], [("9", "Y"), ("10", "X"), (None, "A"), (None, "Z")])

    def test_lost_places_show_no_current_count(self) -> None:
        got = summarize_changes([self.row("置き換え", "P1", "A", "C", None, "2", problems=[NO_PLACE])])
        self.assertEqual([(s["kind"], s["quantity"], s["problems"]) for s in got], [("置き換え", "— → 2", [NO_PLACE])])


class Calculation(StudyCase):
    def test_now_and_plans_and_annual_impact(self) -> None:
        self.store.apply_replace(self.number, "A", "K120", "K121")
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
        # 1台あたりの平均の増減は、年間の影響に入れた P1・P2 の台数（1000＋500）で割る。台数の無い P3 は分母に入れない
        self.assertEqual(result["weighted"]["A"], Decimal(40))
        self.assertEqual(result["weighted"]["B"], Decimal(-30) * 500 / 1500)  # P2 は 280 → 250、P1 は差 0 でも分母に入る

    def test_weighted_is_none_without_any_annual(self) -> None:
        self.store.save_targets(self.number, ["P3"])
        result = calculate(self.store.workspace(self.number), CostHead.MATERIAL)
        self.assertEqual(result["weighted"], {p: None for p in result["plans"]})

    def test_annual_from_conditions_overrides_the_bom(self) -> None:
        self.store.save_targets(self.number, ["P1"])
        self.store.save_conditions(self.number, {"P1": "10"})
        self.store.apply_replace(self.number, "A", "K120", "K121")
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
        self.store.apply_replace(self.number, "A", "K120", "K121")
        result = calculate(self.store.workspace(self.number), CostHead.MANUFACTURING)
        p1 = result["rows"][0]
        # 製造原価 ＝ 材料費 × 1.1 × 1.2（直接費・償却費は未登録で 0）
        self.assertEqual(p1["now"], Decimal("356.4"))
        self.assertEqual(p1["plans"]["A"], Decimal("409.2"))

    def test_processing_and_amortization_heads_are_the_product_own_values(self) -> None:
        # 部品の置き換えで変わるのは材料費だけ。加工（加工費）は未登録なら 0、型費（償却費）は未登録なら空で不完全
        self.store.apply_replace(self.number, "A", "K120", "K121")
        ws = self.store.workspace(self.number)
        processing = calculate(ws, HEADS["processing"])["rows"][0]
        self.assertEqual((processing["now"], processing["plans"]["A"]), (Decimal(0), Decimal(0)))
        self.assertFalse(processing["now_incomplete"] or processing["incomplete"]["A"])
        amortization = calculate(ws, HEADS["amortization"])
        self.assertIsNone(amortization["rows"][0]["plans"]["A"])
        self.assertTrue(amortization["rows"][0]["incomplete"]["A"])
        self.assertTrue(amortization["impact_incomplete"]["A"])

    def test_calculation_does_not_write(self) -> None:
        self.store.apply_replace(self.number, "A", "K120", "K121")
        before = self.conn.total_changes
        calculate(self.store.workspace(self.number), CostHead.MANUFACTURING)
        check_changes(self.store.workspace(self.number))
        self.assertEqual(self.conn.total_changes, before)


class Release(StudyCase):
    def test_release_freezes_the_study_and_the_bom(self) -> None:
        self.store.apply_replace(self.number, "A", "K120", "K121")
        self.assertEqual(self.store.take_release(self.number), 1)
        frozen = calculate(self.store.workspace(self.number, 1), CostHead.MATERIAL)
        # 仮を直し、部品表の単価も変わる
        self.store.apply_replace(self.number, "A", "K121", "K120")
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
        # 新品番を未登録の品番にすると、結果は不完全で、⑥に未登録として出る
        self.ok("PUT", "/api/v2/temp-items", q, {"rows": [{"code": tmp}]})
        self.ok("POST", "/api/v2/replace/apply", q + "&plan=A", {"old": OLD, "new": tmp})
        lose_temps(StudyStore(self.store.connection), number)
        self.assertTrue(all(r["incomplete"]["A"] for r in self.ok("GET", "/api/v2/calc", q + "&head=material")["rows"]))
        self.assertIn("未登録", [p for s in self.ok("GET", "/api/v2/changes", q + "&plan=A")["summary"] if s["new"] == tmp for p in s["problems"]])
        # 仮品番に新品番と同じ材料費を入れると、新品番に置き換えた時と同じ値で、不完全の印が外れる
        got = self.ok("PUT", "/api/v2/temp-items", q, {"rows": [{"code": tmp, "name": "仮の部品", "specification": "t2×40", "material": price},
                                                               {"code": NEW, "name": "台帳にある品番"}, {"code": ""}]})
        self.assertEqual(got["rows"], [{"code": tmp, "name": "仮の部品", "specification": "t2×40", "material": price, "master": False,
                                        "base": None, "changed": 0, "derived": None},
                                       {"code": NEW, "name": "台帳にある品番", "specification": "", "material": None, "master": True,
                                        "base": None, "changed": 0, "derived": None}])
        self.assertNotIn("未登録", [p for s in self.ok("GET", "/api/v2/changes", q + "&plan=A")["summary"] if s["new"] == tmp for p in s["problems"]])
        looked = self.ok("POST", "/api/v2/lookup", q, {"codes": [tmp, NEW]})["items"]
        self.assertEqual((looked[tmp]["name"], looked[tmp]["temp"], looked[NEW]["temp"]), ("仮の部品", True, False))
        # 仮品番の仕様は、⑤⑧の仕様の列に出る（仕様を送らない保存では消えない）
        self.ok("PUT", "/api/v2/temp-items", q, {"rows": [{"code": tmp, "name": "仮の部品", "material": price}]})
        self.assertEqual(self.ok("POST", "/api/v2/lookup", q, {"codes": [tmp]})["items"][tmp]["specification"], "t2×40")
        with_temp = self.ok("GET", "/api/v2/calc", q + "&head=material")
        self.assertFalse(any(r["incomplete"]["A"] for r in with_temp["rows"]))
        self.ok("POST", "/api/v2/replace/apply", q + "&plan=A", {"old": tmp, "new": NEW})
        self.assertEqual(self.ok("GET", "/api/v2/calc", q + "&head=material")["impact"], with_temp["impact"])
        # 版確定で仮品番も固まる
        self.ok("POST", "/api/v2/replace/apply", q + "&plan=A", {"old": NEW, "new": tmp})
        version = self.ok("POST", "/api/v2/release", q, {})["version"]
        lose_temps(StudyStore(self.store.connection), number)
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
        self.assertEqual(book["金額（縦持ち）"].max_row, 1 + 2 * 8)  # 2製品×8費目
        money = [r for r in book["金額（縦持ち）"].iter_rows(min_row=2, values_only=True) if r[0] == P1 and r[2] == "製造原価"][0]
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
        self.ok("POST", "/api/v2/replace/apply", q + "&plan=A", {"old": OLD, "new": NEW})
        stay = next(p for p in self.ok("GET", "/api/v2/after-parts", q)["places"] if p["part"] == "ST0050-001" and p["product"] == P1)
        self.ok("POST", "/api/v2/structure", q + "&plan=A", {"quantity": [{**{k: stay[k] for k in ("product", "balloon", "part")}, "quantity": "0"}]})
        self.ok("POST", "/api/v2/structure", q + "&plan=A", {"add": [{"product": P2, "part": "PL0030-001", "quantity": "1"}]})
        data = self.ok("GET", "/api/v2/structure", q + "&plan=A")
        # 図面が同じ対象品番だけ（無い品番は図面なし）。箇所は現行と変えた後の組
        self.assertEqual((data["drawing"], data["products"]), ("AS-01", [P1, P2]))
        self.assertEqual(data["drawings"], [{"drawing": "AS-01", "count": 2}, {"drawing": "", "count": 1}])
        # 最初に出す図面は、対象品番の先頭の製品の図面
        self.ok("PUT", "/api/v2/targets", q, {"products": ["ZZ9999-001", P1, P2]})
        self.assertEqual(self.ok("GET", "/api/v2/structure", q + "&plan=A")["products"], ["ZZ9999-001"])
        self.ok("PUT", "/api/v2/targets", q, {"products": [P1, P2, "ZZ9999-001"]})
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
        self.assertEqual({(r["product"], r["kind"], r["old"], r["new"]) for r in changes}, {
            (P1, "置き換え", OLD, NEW), (P2, "置き換え", OLD, NEW), (P1, "削除", "ST0050-001", None), (P2, "追加", None, "PL0030-001")})
        before = self.store.connection.total_changes
        result = self.ok("GET", "/api/v2/calc", q + "&head=material")
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertEqual([r["product"] for r in result["rows"]], [P1, P2, "ZZ9999-001"])
        used = self.ok("GET", "/api/v2/where-used", q + f"&code={OLD}")
        self.assertEqual((used["inside"], used["outside"], used["deep"]), (2, 4, 0))
        released = self.ok("POST", "/api/v2/release", q, {})
        self.assertEqual((released["version"], released["versions"]), (1, [1]))
        self.assertEqual([r["version"] for r in released["releases"]], [1])
        self.assertRegex(released["releases"][0]["taken_at"], r"^\d{4}-\d{2}-\d{2}")
        self.assertEqual(self.ok("GET", "/api/v2/calc", q + "&version=1&head=material"), {
            **result, "computed_at": self.ok("GET", "/api/v2/calc", q + "&version=1&head=material")["computed_at"]})
        excel = self.api.handle("GET", "/api/v2/export/study", q)
        self.assertEqual(excel.status, 200)
        book = load_openpyxl().load_workbook(io.BytesIO(excel.body))
        self.assertEqual(book.sheetnames, ["金額（横持ち）", "金額（縦持ち）", "構成の変更", "計算条件"])
        # 横持ちは画面の製品別と同じ形: 製品ごとに4行、列は8費目。値は縦持ちと同じ
        across = list(book["金額（横持ち）"].iter_rows(values_only=True))
        heads = list(across[0][4:12])
        self.assertEqual(len(heads), 8)
        result = self.ok("GET", "/api/v2/calc", q + "&head=all")
        products = [r["product"] for r in result["heads"][0]["rows"]]
        self.assertEqual([r[3] for r in across[1:1 + 4 * len(products)]], ["現行", "変更後", "増減", "増減（％）"] * len(products))
        down = {(r[0], r[2]): r for r in book["金額（縦持ち）"].iter_rows(min_row=2, values_only=True)}
        for row in across[1:1 + 4 * len(products)]:
            at = {"現行": 3, "変更後": 4, "増減": 5, "増減（％）": 6}[row[3]]
            self.assertEqual(list(row[4:12]), [down[(row[0], h)][at] for h in heads])
        # 一番下に年間の影響（材料費小計と製造原価だけ）と、1台あたりの平均の増減
        bottom = {r[0]: r for r in across[-3:]}
        impact = {h["label"]: h["impact"]["A"] for h in result["heads"]}
        self.assertEqual([None if v is None else Decimal(str(v)) for v in bottom["年間の影響"][4:12]],
                         [Decimal(impact[h]) if h in ("材料費小計", "製造原価") else None for h in heads])
        self.assertEqual(len([v for v in bottom["1台あたりの平均の増減"][4:12] if v is not None]), 8)
        # 計算条件の先頭に検討の見出し
        self.assertEqual(list(book["計算条件"].iter_rows(min_row=2, max_row=2, values_only=True))[0][2:4], ("検討番号", number))
        # 構成の変更は、した事ごとに1行（2製品の置き換えが1行）
        self.assertIn(("置き換え", OLD, NEW, 2), [(r[0], r[1], r[3], r[6]) for r in
                                              book["構成の変更"].iter_rows(min_row=2, values_only=True)])

    def test_replacing_the_common_part_keeps_every_product_positive(self) -> None:
        self.ok("POST", "/api/v2/bom/import", body={"sample": True})
        products = [p["code"] for p in self.ok("GET", "/api/v2/products")["products"]]
        self.assertEqual(len(products), 10)
        number = self.ok("POST", "/api/v2/studies", body={})["header"]["number"]
        q = f"number={number}"
        self.ok("PUT", "/api/v2/targets", q, {"products": products})
        self.ok("POST", "/api/v2/replace/apply", q + "&plan=A", {"old": OLD, "new": NEW})
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
            "bom_line": ["parent", "child", "quantity", "balloon", "branch"],
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

    def test_excel_is_made_even_with_control_characters_in_the_names(self) -> None:
        v = bom()
        v.add_item(Item("X7", "名\x0b称\x01"))
        StudyStore(self.store.connection).replace_bom(v, ALL)
        number = self.ok("POST", "/api/v2/studies", body={"title": "題\x02目", "products": ["P1"]})["header"]["number"]
        self.ok("POST", "/api/v2/structure", f"number={number}", {"add": [{"product": "P1", "part": "X7", "quantity": "1"}]})
        book = load_openpyxl().load_workbook(io.BytesIO(self.api.handle("GET", "/api/v2/export/study", f"number={number}").body))
        self.assertIn("名称", [r[4] for r in book["構成の変更"].iter_rows(values_only=True)])

    def test_errors_are_json(self) -> None:
        response = self.api.handle("GET", "/api/v2/study", "number=D-0404")
        self.assertEqual(response.status, 404)
        self.assertIn("error", response.json())


if __name__ == "__main__":
    unittest.main()
