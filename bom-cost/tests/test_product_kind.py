"""製品と部品の区分の記号（製品 2・部品 1）: 製品とみなす記号の決め方と、マスタ登録で付く記号。"""

import io
import tempfile
import unittest
from pathlib import Path

from bomcost import BomLine, Item, Store, Version
from bomcost.api import Api
from bomcost.demo import build_demo
from bomcost.group import Group, _Structure
from bomcost.importer import load_openpyxl
from bomcost.masterbook import PART, PRODUCT, SHEETS, plan_book
from test_group import SampleDbCase
from test_screens import EmptyDbCase

try:
    load_openpyxl()
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


def _version(*items: Item) -> Version:
    v = Version("t")
    v.add_items(list(items))
    v.add_bom_lines([BomLine("T1", None, "C1", 1)])
    return v


class ProductKindOrder(unittest.TestCase):
    def kind(self, v: Version, requested: str = "") -> str | None:
        return Group.product_kind(v, _Structure(v), requested)

    def test_requested_wins(self) -> None:
        v = _version(Item("T1", "上", "2"), Item("C1", "部品", "1"))
        self.assertEqual(self.kind(v, "X"), "X")

    def test_code_2_before_text_and_top_parents(self) -> None:
        v = _version(Item("T1", "上", "P"), Item("C1", "部品", "1"), Item("Q1", "製品", "製品"), Item("Q2", "q", "2"))
        self.assertEqual(self.kind(v), "2")

    def test_without_code_2_keeps_earlier_rule(self) -> None:
        self.assertEqual(self.kind(_version(Item("T1", "上", "P"), Item("C1", "部品", "製品"))), "製品")
        self.assertEqual(self.kind(_version(Item("T1", "上", "P"), Item("C1", "部品", "B"))), "P")
        self.assertIsNone(self.kind(_version(Item("T1", "上"), Item("C1", "部品"))))


class ManualKindWithoutBom(EmptyDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.group = self.new_group()

    def put(self, code: str, **extra: str) -> dict:
        return self.ok("PUT", "/api/manual/items", body={"code": code, "name": code, "group_id": self.group, **extra})

    def test_product_2_and_part_1(self) -> None:
        self.put("N-1", role="product")
        data = self.put("Q-1", role="part")
        self.assertEqual(data["product_kind"], "2")
        self.assertEqual({i["code"]: (i["kind"], i["role"]) for i in data["items"]},
                         {"N-1": ("2", "product"), "Q-1": ("1", "part")})

    def test_part_keeps_sent_kind_but_not_2(self) -> None:
        data = self.put("Q-2", role="part", kind="M")
        self.assertEqual(data["items"][0]["kind"], "M")
        self.assert_error(self.call("PUT", "/api/manual/items", body={"code": "Q-3", "name": "q", "role": "part",
                                                                      "kind": "2", "group_id": self.group}),
                          400, "製品の記号")


class ManualKindOnOtherSymbols(SampleDbCase):
    def test_part_stays_empty_when_products_use_other_symbol(self) -> None:
        data = self.ok("PUT", "/api/manual/items", body={"code": "Q-9", "name": "q", "role": "part",
                                                         "group_id": "2026-10"})
        self.assertEqual(data["product_kind"], "P")
        self.assertEqual({i["code"]: i["kind"] for i in data["items"]}["Q-9"], "")


@unittest.skipUnless(HAS_OPENPYXL, "openpyxl が無い")
class BookPartKind(unittest.TestCase):
    def book(self, part_kind: str) -> bytes:
        wb = load_openpyxl().Workbook()
        wb.remove(wb.active)
        for sheet, headers in SHEETS.items():
            wb.create_sheet(sheet).append(list(headers))
        wb[PRODUCT].append(["N-1", "製品"])
        wb[PART].append(["Q-1", "部品", part_kind])
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    def test_empty_part_kind_becomes_1_and_2_is_refused(self) -> None:
        with Store(":memory:") as store:
            store.create_group("D-1", group_id="g")
            plan = plan_book(store, "2", self.book(""), "g")
            self.assertEqual(plan.errors, [])
            self.assertEqual({c: i.kind for c, i in plan.items.items()}, {"N-1": "2", "Q-1": "1"})
            self.assertEqual({c: i.kind for c, i in plan_book(store, "P", self.book(""), "g").items.items()},
                             {"N-1": "P", "Q-1": ""})
            self.assertEqual([e.message for e in plan_book(store, "P", self.book("2"), "g").errors],
                             ["部品の区分に製品の記号 2 は使えない"])


class DemoKinds(unittest.TestCase):
    def test_demo_products_2_and_parts_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.sqlite"
            build_demo(path)
            with Store(path) as store:
                v = store.load_group("2026-10")
                products = Api(store).handle("GET", "/api/group/products", "group=2026-10", b"").json()
        self.assertEqual({c: i.kind for c, i in v.items.items()},
                         {"A-100": "2", "V-110": "2", "K-200": "2", "B-200": "1", "C-300": "1", "F-600": "1", "X-610": "1"})
        self.assertEqual([p["code"] for p in products["products"]], ["A-100", "K-200", "V-110"])


if __name__ == "__main__":
    unittest.main()
