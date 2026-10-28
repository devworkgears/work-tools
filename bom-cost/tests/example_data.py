"""テストで使う架空の例（すべて材料費・円）。品番・数値は架空。"""

from bomcost import (
    BomLine,
    CostHead,
    CostRow,
    Item,
    ItemKind,
    MoldCost,
    ReuseTarget,
    Scope,
    ScopePolicy,
    Source,
    Version,
    Volume,
    VolumeKind,
)

M = CostHead.MATERIAL


def items() -> list[Item]:
    return [
        Item("A-100", "土台の製品", ItemKind.PRODUCT, has_children=1),
        Item("V-110", "新しい製品", ItemKind.PRODUCT, has_children=1),
        Item("K-200", "既存品", ItemKind.PRODUCT, has_children=0),
        Item("B-200", "部品B", ItemKind.PART),
        Item("C-300", "部品C", ItemKind.PART),
        Item("F-600", "部品F", ItemKind.PART),
        Item("X-610", "試作品", ItemKind.PART, source=Source.MANUAL),
    ]


def bom_lines() -> list[BomLine]:
    return [
        BomLine("A-100", "1", "B-200", 2),
        BomLine("A-100", "2", "C-300", 3),
        BomLine("V-110", "1", "A-100", 1),
        BomLine("V-110", "2", "F-600", 1),
        BomLine("V-110", "3", "C-300", -1),
    ]


def version_2026_10() -> Version:
    v = Version("2026-10")
    v.add_items(items())
    v.add_bom_lines(bom_lines())
    v.add_costs(
        [
            CostRow.from_mbom("A-100000", {M: 200}),
            CostRow.from_mbom("B-200000", {M: 1600}),
            CostRow.from_mbom("C-300000", {M: 500}),
            CostRow.from_mbom("F-600000", {M: 250}),
            CostRow.from_mbom("K-200000", {M: 3000}),
            CostRow("X-610", {M: 200}, source=Source.MANUAL),
        ]
    )
    v.add_volume(Volume("V-110", 10000, VolumeKind.PLAN))
    v.add_volume(Volume("K-200", 5000, VolumeKind.ACTUAL))
    return v


def version_2026_12() -> Version:
    v = Version("2026-12")
    v.add_items(items())
    v.add_bom_lines(bom_lines())
    v.add_costs(
        [
            CostRow.from_mbom("A-100000", {M: 200}),
            CostRow.from_mbom("B-200000", {M: 1600}),
            CostRow.from_mbom("C-300000", {M: 520}),
            CostRow.from_mbom("F-600000", {M: 250}),
        ]
    )
    return v


def policy() -> ScopePolicy:
    """A-100 の材料費は「自分だけ」（登録値 200 に子を足す）。"""
    p = ScopePolicy()
    p.set_item("A-100", M, Scope.OWN_ONLY)
    return p


def mold_x610() -> MoldCost:
    return MoldCost("X-610", 2400000, 2)


def reuse_k200() -> ReuseTarget:
    return ReuseTarget("X-610", "K-200", 2, replaced_part="F-600")
