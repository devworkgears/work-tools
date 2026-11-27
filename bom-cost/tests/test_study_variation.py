"""バリエーション: 元の製品を直下に持ち、ほかの直下の行で足し引きする製品。データは架空。"""

import json
import unittest
from dataclasses import replace
from decimal import Decimal

import example_settings  # noqa: F401
from bomcost import settings
from bomcost.api import Api
from bomcost.model import BomLine, CostHead, CostRow, Item, Version, Volume
from bomcost.store import Store
from bomcost.study import NEGATIVE, SHORT, StudyStore, calculate, check_changes, variation_bases, variation_shortfalls

ALL = ("item", "bom_line", "cost", "volume")
D = Decimal


def bom() -> Version:
    """製品 P1（A×1・B×1・C×4）。バリエーション（3桁）P1V は P1×1 に H を足して C を2つ引く。P1W は P1×1 から B を1つ引く。
    Q1X も3桁で P1 を直下に持つが、品番の頭が違うのでバリエーションでない。材料費 A 10・B 20・N 25・C 5・H 7。"""
    v = Version("t")
    for code in ("P1", "P1V", "P1W", "Q1X", "A", "B", "N", "C", "H"):
        v.add_item(Item(code, f"{code} の名称"))
    v.add_bom_lines([
        BomLine("P1", "1", "A", 1), BomLine("P1", "2", "B", 1), BomLine("P1", "3", "C", 4),
        BomLine("P1V", "1", "P1", 1), BomLine("P1V", "11", "H", 1), BomLine("P1V", "12", "C", -2),
        BomLine("P1W", "1", "P1", 1), BomLine("P1W", "13", "B", -1),
        BomLine("Q1X", "1", "P1", 1),
    ])
    for code, value in {"A": "10", "B": "20", "N": "25", "C": "5", "H": "7"}.items():
        v.add_cost(CostRow(code, {CostHead.MATERIAL: value}))
    for code, annual in {"P1": "1000", "P1V": "300", "P1W": "200"}.items():
        v.add_volume(Volume(code, D(annual)))
    return v


class VariationCase(unittest.TestCase):
    def setUp(self) -> None:
        before = settings.current()
        self.addCleanup(settings.use, before)
        settings.use(replace(before, variation_digits=3))
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.api = Api(self.store)
        StudyStore(self.store.connection).replace_bom(bom(), ALL)
        self.number = self.ok("POST", "/api/v2/studies", {"products": ["P1", "P1V", "P1W"]}, "")["header"]["number"]
        self.q = f"number={self.number}&plan=A"

    def ok(self, method: str, path: str, body: object = None, query: str | None = None) -> dict:
        r = self.api.handle(method, path, self.q if query is None else query, b"" if body is None else json.dumps(body).encode())
        self.assertLess(r.status, 300, r.body.decode("utf-8"))
        return r.json()

    def ws(self):
        return StudyStore(self.store.connection).workspace(self.number)

    def material(self) -> dict[str, tuple]:
        return {r["product"]: (r["now"], r["plans"]["A"]) for r in calculate(self.ws(), CostHead.MATERIAL)["rows"]}

    def replace_b(self) -> None:
        self.ok("POST", "/api/v2/replace/apply", {"rows": [{"product": "P1", "balloon": "2", "old": "B", "new": "N"}]})


class Finding(VariationCase):
    def test_a_product_with_a_product_below_is_a_variation(self) -> None:
        self.assertEqual(variation_bases(self.ws()), {"P1V": "P1", "P1W": "P1"})
        self.assertEqual(self.ok("GET", "/api/v2/targets")["variations"], {"P1V": "P1", "P1W": "P1"})

    def test_the_digits_and_the_head_of_the_code_pick_the_variations(self) -> None:
        self.ok("PUT", "/api/v2/targets", {"products": ["P1", "P1V", "P1W", "Q1X"]}, f"number={self.number}")
        self.assertEqual(variation_bases(self.ws()), {"P1V": "P1", "P1W": "P1"})
        before = settings.current()
        for digits in (4, None):
            settings.use(replace(before, variation_digits=digits))
            self.assertEqual(variation_bases(self.ws()), {}, digits)
        settings.use(before)
        self.assertEqual(settings.parse({"variation_digits": "10"}).variation_digits, 10)
        with self.assertRaisesRegex(ValueError, "整数"):
            settings.parse({"variation_digits": "1.5"})

    def test_the_base_need_not_be_chosen(self) -> None:
        self.ok("PUT", "/api/v2/targets", {"products": ["P1V"]}, f"number={self.number}")
        self.assertEqual(variation_bases(self.ws()), {"P1V": "P1"})


class BaseChanges(VariationCase):
    def test_a_change_of_the_base_also_counts_in_its_variations(self) -> None:
        # P 50・V 50＋7−10＝47・W 50−20＝30。P の B（20）を N（25）に替えると、どれも 5 上がる
        self.assertEqual({k: v[0] for k, v in self.material().items()}, {"P1": D(50), "P1V": D(47), "P1W": D(30)})
        self.replace_b()
        self.assertEqual({k: v[1] for k, v in self.material().items()}, {"P1": D(55), "P1V": D(52), "P1W": D(35)})

    def test_the_base_is_not_changed_when_only_the_variation_is_chosen(self) -> None:
        self.ok("PUT", "/api/v2/targets", {"products": ["P1V"]}, f"number={self.number}")
        r = self.api.handle("POST", "/api/v2/replace/apply", self.q,
                            json.dumps({"rows": [{"product": "P1", "balloon": "2", "old": "B", "new": "N"}]}).encode())
        self.assertEqual(r.status, 400)
        self.assertEqual(self.material(), {"P1V": (D(47), D(47))})

    def test_the_effect_of_a_base_change_lists_its_variations(self) -> None:
        self.replace_b()
        effects = self.ok("GET", "/api/v2/calc", query=self.q + "&head=all")["effects"]
        (effect,) = [e for e in effects if e["old"] == "B"]
        self.assertEqual(sorted(p["product"] for p in effect["products"]), ["P1", "P1V", "P1W"])
        # 年間の影響は製品ごとに台数を掛けて足す（台数は元の製品とバリエーションで別）
        self.assertEqual(D(effect["impact"]), sum(D(p["unit"]) * D(p["annual"]) for p in effect["products"]))


class Subtracting(VariationCase):
    def test_a_variation_takes_minus_rows(self) -> None:
        self.ok("POST", "/api/v2/structure", {"quantity": [{"product": "P1V", "balloon": "12", "part": "C", "quantity": "-3"}],
                                              "add": [{"product": "P1V", "balloon": "14", "part": "A", "quantity": "-1"}]})
        self.assertFalse([c for c in check_changes(self.ws()) if NEGATIVE in c.problems])
        self.assertEqual(self.material()["P1V"], (D(47), D(32)))  # 47 − 5 − 10

    def test_the_base_takes_no_minus_rows(self) -> None:
        r = self.api.handle("POST", "/api/v2/structure", self.q,
                            json.dumps({"quantity": [{"product": "P1", "balloon": "3", "part": "C", "quantity": "-1"}]}).encode())
        self.assertEqual(r.status, 400)

    def test_going_below_zero_is_flagged_but_still_counted(self) -> None:
        # W は B を1つ引く。P で B を N に替えると、W の B は 0 − 1 ＝ −1
        self.assertEqual(variation_shortfalls(self.ws(), "A"), {})
        self.replace_b()
        self.assertEqual(variation_shortfalls(self.ws(), "A"), {"P1W": {"B": D(-1)}})
        (change,) = check_changes(self.ws())
        self.assertIn(SHORT, change.problems)
        self.assertTrue(change.counted)
        rows = {r["product"]: r for r in self.ok("GET", "/api/v2/calc", query=self.q + "&head=material")["rows"]}
        self.assertEqual((rows["P1W"]["short"], rows["P1V"]["short"], rows["P1W"]["base"]), ({"A": ["B"]}, {}, "P1"))


class Words(VariationCase):
    def test_the_mark_follows_the_workplace_word(self) -> None:
        before = settings.current()
        self.addCleanup(settings.use, before)
        settings.use(settings.parse({**before.to_json(), "labels": {"バリエーション": "VC"}}))
        self.replace_b()
        rows = self.ok("GET", "/api/v2/changes")["rows"]
        self.assertEqual([r["problems"] for r in rows if r["product"] == "P1"], [["VCでマイナス"]])


class Screens(VariationCase):
    def test_the_change_screen_leaves_out_the_variations(self) -> None:
        got = self.ok("GET", "/api/v2/structure", query=self.q + "&kind=product&all=1")
        self.assertEqual(got["products"], ["P1"])

    def test_the_variation_screen_shows_every_variation_with_only_the_rows_added_or_taken(self) -> None:
        self.replace_b()
        got = self.ok("GET", "/api/v2/structure", query=self.q + "&variation=1")
        self.assertEqual(got["products"], ["P1V", "P1W"])
        self.assertEqual({p: [(r["part"], r["quantity"], r.get("problem", False)) for r in rows] for p, rows in got["direct"].items()},
                         {"P1V": [("H", "1", False), ("C", "-2", False)], "P1W": [("B", "-1", True)]})
        # 直下に持つ製品の中は変えた後
        self.assertIn("N", [r["part"] for r in got["inside"]["P1"]])


if __name__ == "__main__":
    unittest.main()
