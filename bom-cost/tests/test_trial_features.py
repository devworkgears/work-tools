"""②構成変更の簡易照会・品番集計・品番一括置換。"""

from bomcost.api import Response
from bomcost.scenario import Scenario
from test_group import NAME, SampleDbCase


class TrialFeatures(SampleDbCase):
    def grouped(self, query: str = "") -> dict:
        return self.ok("GET", "/api/group/parts/by-code", "version=2026-10&part=部品" + query)

    def replace_code(self, part: str, **extra: str) -> Response:
        return self.call("POST", "/api/group/changes/by-code", body={
            "version_id": "2026-10", "name": NAME, "part": part, "replacement": "X-610", **extra,
        })

    def test_brief_uses_lookup_values_and_sources(self) -> None:
        self.ok("PUT", "/api/manual/items", body={"code": "B-200", "name": "部品B",
                                                  "specification": "M6", "costs": {"INDIRECT": "7"}})
        query = "version=2026-10&q=B-200"
        full = self.ok("GET", "/api/lookup", query)["item"]
        brief = self.ok("GET", "/api/lookup", query + "&brief=1")["item"]
        self.assertEqual(brief, {key: full[key] for key in ("code", "name", "specification", "costs")})
        self.assertEqual(len(brief["costs"]), 6)
        costs = {c["head"]: (c["value"], c["source"]) for c in brief["costs"]}
        self.assertEqual(costs["MATERIAL"], ("1600", "bom"))
        self.assertEqual(costs["INDIRECT"], ("7", "common"))
        self.assertEqual(costs["AMORTIZATION"], (None, None))

    def test_group_counts_all_places_before_limit(self) -> None:
        rows = {r["part"]: r for r in self.grouped()["rows"]}
        self.assertEqual((rows["B-200"]["product_count"], rows["B-200"]["place_count"]), (2, 2))
        self.assertEqual((rows["C-300"]["product_count"], rows["C-300"]["place_count"]), (2, 3))
        limited = self.grouped("&limit=1")
        self.assertTrue(limited["truncated"])
        self.assertEqual(limited["rows"], [rows["B-200"]])
        self.assertEqual(len(self.parts("&part=部品&limit=1")["rows"]), 1)

    def test_grouped_trial_scope_and_replacements(self) -> None:
        self.change("A-100", kind="REPLACE", part="B-200", replacement="X-610")
        rows = {r["part"]: r for r in self.grouped(f"&name={NAME}&trial=1")["rows"]}
        self.assertEqual(rows["B-200"]["product_count"], 1)
        self.assertEqual(rows["B-200"]["replaced_by"], ["X-610"])
        filtered = self.grouped("&q=新しい")
        self.assertTrue(all(r["product_count"] == 1 for r in filtered["rows"]))

    def test_replace_all_products_and_places(self) -> None:
        result = self.replace_code("C-300", quantity="1")
        self.assertEqual(result.status, 200, result.body.decode())
        self.assertEqual(result.json()["count"], 3)
        self.assertEqual(result.json()["products"], ["A-100", "V-110"])
        self.assertEqual(sum(len(self.store.load_scenario(s.scenario_id).history)
                             for s in self.store.list_scenarios()), 3)

    def test_replace_trial_scope(self) -> None:
        self.change("A-100", kind="ADD", part="F-600")
        result = self.replace_code("B-200", trial="1")
        self.assertEqual(result.status, 200, result.body.decode())
        self.assertEqual(result.json()["products"], ["A-100"])
        self.assertEqual(result.json()["count"], 1)

    def test_failure_preserves_existing_and_creates_nothing(self) -> None:
        sid = self.change("A-100", kind="ADD", part="F-600")["scenario_id"]
        before = self.store.load_scenario(sid).history
        result = self.replace_code("C-300")
        self.assert_error(result, 400, "正の数")
        self.assertEqual([s.scenario_id for s in self.store.list_scenarios()], [sid])
        self.assertEqual(self.store.load_scenario(sid).history, before)

    def test_write_failure_rolls_back_every_product(self) -> None:
        write = self.store._write_scenario
        calls = []

        def failing(scenario: Scenario) -> int:
            calls.append(scenario.base)
            if len(calls) == 2:
                raise RuntimeError("書けない")
            return write(scenario)

        self.store._write_scenario = failing
        try:
            result = self.replace_code("B-200")
        finally:
            del self.store._write_scenario
        self.assertEqual(result.status, 500)
        self.assertEqual(calls, ["A-100", "V-110"])
        self.assertEqual(self.store.list_scenarios(), [])
