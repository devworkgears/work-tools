"""取り込んだ部品表を全部消す。検討は残る。データは架空。"""

import json
import unittest
from decimal import Decimal

import example_settings  # noqa: F401
from bomcost.api import Api
from bomcost.model import BomLine, CostHead, CostRow, Item, Version, Volume
from bomcost.store import Store
from bomcost.study import StudyStore


class ClearBomCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.api = Api(self.store)
        v = Version("t")
        for code in ("P1", "A"):
            v.add_item(Item(code, f"{code} の名称"))
        v.add_bom_lines([BomLine("P1", "1", "A", 2)])
        v.add_cost(CostRow("A", {CostHead.MATERIAL: "10"}))
        v.add_volume(Volume("P1", Decimal(100)))
        self.study = StudyStore(self.store.connection)
        self.study.replace_bom(v, ("item", "bom_line", "cost", "volume"))

    def call(self, method: str, path: str, body: object = None, query: str = ""):
        r = self.api.handle(method, path, query, b"" if body is None else json.dumps(body).encode())
        self.assertLess(r.status, 300, r.body.decode("utf-8"))
        return r.json()

    def test_clear_empties_all_tables_and_keeps_studies(self) -> None:
        number = self.call("POST", "/api/v2/studies", {"products": ["P1"]})["header"]["number"]
        got = self.call("POST", "/api/v2/bom/clear", {})
        self.assertEqual(got["counts"], {"item": 0, "bom_line": 0, "cost": 0, "volume": 0})
        self.assertEqual(self.call("GET", "/api/v2/bom")["counts"], got["counts"])
        self.assertEqual(len(self.study.load_bom().items), 0)
        self.assertIn(number, [s["number"] for s in self.call("GET", "/api/v2/studies")["studies"]])


if __name__ == "__main__":
    unittest.main()
