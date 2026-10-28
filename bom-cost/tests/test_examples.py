"""架空の例の数値が、入力データから計算で出ることを確かめる。"""

import unittest
from decimal import Decimal

import example_data as ex
from bomcost import (
    Allocation,
    Change,
    ChangeKind,
    CostHead,
    Scenario,
    allocate_mold_cost,
    calculate_cost,
    reuse_impact,
)

M = CostHead.MATERIAL


def d(text: str) -> Decimal:
    return Decimal(text)


def v110_scenario(version=None) -> Scenario:
    s = Scenario("V-110 検討", "A-100", "V-110", version or ex.version_2026_10(), M, ex.policy())
    s.apply(Change(ChangeKind.ADD, "F-600", 1))
    s.apply(Change(ChangeKind.REMOVE, "C-300", 1))
    return s


class RollupExamples(unittest.TestCase):
    def test_a100(self) -> None:
        self.assertEqual(calculate_cost(ex.version_2026_10(), "A-100", M, ex.policy()).total, d("4900"))

    def test_v110_from_version_bom(self) -> None:
        self.assertEqual(calculate_cost(ex.version_2026_10(), "V-110", M, ex.policy()).total, d("4650"))

    def test_replace_c300_with_450(self) -> None:
        v = ex.version_2026_10()
        before = calculate_cost(v, "A-100", M, ex.policy()).total
        v.add_cost(ex.CostRow.from_mbom("C-300000", {M: 450}))
        after = calculate_cost(v, "A-100", M, ex.policy()).total
        self.assertEqual(after, d("4750"))
        self.assertEqual(after - before, d("-150"))

    def test_version_2026_12(self) -> None:
        v = ex.version_2026_12()
        self.assertEqual(calculate_cost(v, "A-100", M, ex.policy()).total, d("4960"))
        self.assertEqual(calculate_cost(v, "V-110", M, ex.policy()).total, d("4690"))


class ScenarioExamples(unittest.TestCase):
    def test_diff_mode(self) -> None:
        s = v110_scenario()
        self.assertEqual(s.calculate().total, d("4650"))
        record = s.apply(Change(ChangeKind.REPLACE, "F-600", replacement="X-610"))
        self.assertEqual(record.cost_before, d("4650"))
        self.assertEqual(record.cost_after, d("4600"))
        self.assertEqual(record.difference, d("-50"))


class VersionComparisonExamples(unittest.TestCase):
    def test_recalculate_with_newer_version(self) -> None:
        s = v110_scenario()
        cmp = s.compare_versions(ex.version_2026_10(), ex.version_2026_12())
        self.assertEqual(cmp.total_a, d("4650"))
        self.assertEqual(cmp.total_b, d("4690"))
        self.assertEqual(cmp.difference, d("40"))
        by_part = {row.part: row for row in cmp.lines}
        self.assertEqual(by_part["A-100"].difference, d("60"))
        self.assertEqual(by_part["F-600"].difference, d("0"))
        self.assertEqual(by_part["C-300"].amount_a, d("-500"))
        self.assertEqual(by_part["C-300"].amount_b, d("-520"))
        self.assertEqual(by_part["C-300"].difference, d("-20"))


class MoldExamples(unittest.TestCase):
    def setUp(self) -> None:
        self.version = ex.version_2026_10()
        self.scenario = v110_scenario(self.version)
        self.scenario.apply(Change(ChangeKind.REPLACE, "F-600", replacement="X-610"))

    def allocate(self, allocation: Allocation):
        return allocate_mold_cost(
            self.version, ex.mold_x610(), allocation, [ex.reuse_k200()], self.scenario.overrides()
        )

    def per_unit(self, allocation: Allocation) -> dict[str, Decimal]:
        return {s.product: s.per_unit for s in self.allocate(allocation).shares}

    def test_annual_count_and_per_part(self) -> None:
        a = self.allocate(Allocation.BY_USAGE)
        self.assertEqual(a.annual_count, d("20000"))
        self.assertEqual(a.per_part, d("60"))
        users = {s.product: s.quantity_per_unit for s in a.shares}
        self.assertEqual(users, {"V-110": d("1"), "K-200": d("2")})

    def test_new_products_only(self) -> None:
        self.assertEqual(self.per_unit(Allocation.NEW_PRODUCTS_ONLY), {"V-110": d("120"), "K-200": d("0")})

    def test_by_usage(self) -> None:
        self.assertEqual(self.per_unit(Allocation.BY_USAGE), {"V-110": d("60"), "K-200": d("120")})

    def test_none(self) -> None:
        self.assertEqual(self.per_unit(Allocation.NONE), {"V-110": d("0"), "K-200": d("0")})

    def test_k200_reuse_impact(self) -> None:
        impact = reuse_impact(self.version, ex.reuse_k200(), self.allocate(Allocation.BY_USAGE), M, ex.policy())
        self.assertEqual(impact.current_cost, d("3000"))
        self.assertEqual(impact.part_difference, d("-100"))
        self.assertEqual(impact.difference, d("20"))
        self.assertEqual(impact.after, d("3020"))

    def test_v110_after(self) -> None:
        share = self.allocate(Allocation.BY_USAGE).share_for("V-110")
        self.assertEqual(self.scenario.calculate().total + share.per_unit, d("4660"))


if __name__ == "__main__":
    unittest.main()
