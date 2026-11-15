"""設計検討（/v2/）: 3つの入力が1つの変更の集まりに書くこと・後勝ち・上書きの印・読み直しの確認・計算・版。データは架空。"""

import base64
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from bomcost.api import Api
from bomcost.app import static_response
from bomcost.model import BomLine, CostHead, CostRow, Item, Version, Volume
from bomcost.store import Store
from bomcost.study import (
    GRID,
    NEGATIVE,
    NO_PLACE,
    NOT_TARGET,
    OTHER_PART,
    REPLACE,
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


class LaterWriteWins(StudyCase):
    def test_grid_over_replace_does_not_add_and_marks_the_replace_row(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_grid(self.number, "A", ["P2"], ["K121"], [{"product": "P2", "part": "K121", "value": "1"}])
        got = self.changes()
        self.assertEqual(got[("P2", "U10", "12", "K121")], (Decimal(1), GRID))
        self.assertEqual(got[("P1", "U10", "12", "K121")], (Decimal(2), REPLACE))
        ws = self.store.workspace(self.number)
        self.assertEqual(replace_overridden(ws, "A"), {("K120", "K121"): True})

    def test_replace_alone_is_not_marked(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.assertEqual(replace_overridden(self.store.workspace(self.number), "A"), {("K120", "K121"): False})

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
            {"product": "P1", "part": "K120", "value": "-2", "row": 0},
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
            {"product": "P1", "part": "S50", "value": "-2"},  # 1 個しか無い所から 2 個引く
            {"product": "P1", "part": "X1", "value": "1"},  # 品目に無い
            {"product": "Q9", "part": "K120", "value": "-1"},  # 対象品番でない列
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


class Calculation(StudyCase):
    def test_now_and_plans_and_annual_impact(self) -> None:
        self.store.save_replaces(self.number, "A", [("K120", "K121")])
        self.store.save_grid(self.number, "B", ["P2"], ["S50"], [{"product": "P2", "part": "S50", "value": "-1"}])
        result = calculate(self.store.workspace(self.number), CostHead.MATERIAL)
        rows = {r["product"]: r for r in result["rows"]}
        self.assertEqual(result["plans"], ["A", "B"])
        # P1 = U10(2×100 + 4×5) + 30 + 4×5
        self.assertEqual(rows["P1"]["now"], Decimal(270))
        self.assertEqual(rows["P1"]["plans"], {"A": Decimal(310), "B": Decimal(270)})
        self.assertEqual(rows["P2"]["plans"], {"A": Decimal(320), "B": Decimal(250)})
        self.assertEqual(result["impact"]["A"], Decimal(40) * 1000 + Decimal(40) * 500)
        self.assertTrue(result["impact_incomplete"]["A"])  # P3 は年間台数が無い

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
        self.assertEqual(rows, [{"old": OLD, "new": NEW, "overridden": False}])
        grid = self.ok("PUT", "/api/v2/grid", q + "&plan=A",
                     {"columns": [P1], "rows": ["ST0050-001"], "cells": [{"product": P1, "part": "ST0050-001",
                                                                          "value": "-1"}]})
        self.assertEqual(grid["rows"][0]["cells"], {P1: "-1"})
        self.assertIn({"part": OLD, "origin": 1, "cells": {P1: "-2", P2: "-2"}, "problems": {}, "locked": {}},
                      grid["rows"])
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
