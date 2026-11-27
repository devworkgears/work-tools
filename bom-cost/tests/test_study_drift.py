"""確定した版の時と今の部品表の違い（原価・構成・年間台数）。データは架空。"""

import io
import json
import unittest
from decimal import Decimal

import example_settings  # noqa: F401
from bomcost.api import Api
from bomcost.importer import load_openpyxl
from bomcost.model import BomLine, CostHead, CostRow, Item, Version, Volume
from bomcost.store import Store
from bomcost.study import StudyStore, release_drift

ALL = ("item", "bom_line", "cost", "volume")
D = Decimal


def bom(b_cost: str = "20", c_quantity: int = 4, extra_line: bool = False, p1_annual: str = "1000", z_cost: str = "1") -> Version:
    """製品 P1（A×1・B×1・C×4）。Z は検討に出てこない品番。"""
    v = Version("t")
    for code in ("P1", "A", "B", "C", "N", "Z"):
        v.add_item(Item(code, f"{code} の名称"))
    lines = [BomLine("P1", "1", "A", 1), BomLine("P1", "2", "B", 1), BomLine("P1", "3", "C", c_quantity)]
    if extra_line:
        lines.append(BomLine("P1", "4", "N", 1))
    v.add_bom_lines(lines)
    for code, value in {"A": "10", "B": b_cost, "C": "5", "Z": z_cost}.items():
        v.add_cost(CostRow(code, {CostHead.MATERIAL: value}))
    v.add_volume(Volume("P1", D(p1_annual)))
    return v


class DriftCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.api = Api(self.store)
        self.study = StudyStore(self.store.connection)
        self.study.replace_bom(bom(), ALL)
        self.number = self.ok("POST", "/api/v2/studies", {"products": ["P1"]})["header"]["number"]
        self.ok("POST", "/api/v2/release", {}, f"number={self.number}")

    def call(self, method: str, path: str, body: object = None, query: str = ""):
        return self.api.handle(method, path, query, b"" if body is None else json.dumps(body).encode())

    def ok(self, method: str, path: str, body: object = None, query: str = "") -> dict:
        r = self.call(method, path, body, query)
        self.assertLess(r.status, 300, r.body.decode("utf-8"))
        return r.json()

    def drift(self):
        return self.call("GET", "/api/v2/export/release-drift", query=f"number={self.number}&version=1")

    def test_nothing_changed(self) -> None:
        self.assertEqual(self.drift().json(), {"same": True})

    def test_a_part_not_in_the_study_is_not_compared(self) -> None:
        self.study.replace_bom(bom(z_cost="9"), ALL)
        self.assertEqual(self.drift().json(), {"same": True})

    def test_costs_lines_and_volumes_that_changed(self) -> None:
        self.study.replace_bom(bom(b_cost="22", c_quantity=5, extra_line=True, p1_annual="1200"), ALL)
        got = release_drift(self.study.workspace(self.number, 1), self.study.load_bom())
        self.assertEqual(got.costs, [["B", "B の名称", CostHead.MATERIAL.value, D("20"), D("22"), D("2")]])
        self.assertEqual(got.lines, [["P1", "3", "C", "C の名称", D("4"), D("5")], ["P1", "4", "N", "N の名称", None, D("1")]])
        self.assertEqual(got.volumes, [["P1", "P1 の名称", D("1000"), D("1200")]])
        r = self.drift()
        self.assertEqual(r.status, 200)
        book = load_openpyxl().load_workbook(io.BytesIO(r.body))
        self.assertEqual(book.sheetnames, ["原価の違い", "構成の違い", "年間台数の違い"])
        self.assertEqual([c.value for c in book["構成の違い"][1]], ["親品番", "風船", "サブ番号", "子", "名称", "数量（版1の時）", "数量（今）"])

    def test_only_sheets_with_differences(self) -> None:
        self.study.replace_bom(bom(b_cost="22"), ALL)
        book = load_openpyxl().load_workbook(io.BytesIO(self.drift().body))
        self.assertEqual(book.sheetnames, ["原価の違い"])

    def test_the_working_version_is_refused(self) -> None:
        r = self.call("GET", "/api/v2/export/release-drift", query=f"number={self.number}")
        self.assertEqual(r.status, 400)


if __name__ == "__main__":
    unittest.main()
