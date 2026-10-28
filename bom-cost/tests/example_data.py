"""テストで使う架空の例（すべて材料費・円）。品番・数値・区分の記号は架空。

品番は8桁でないが、変換の規則は MBOM の列を取り込むときだけ掛かるので、原価の行は品番で直接作る。
試作品 X-610 と新製品 V-110 の年間台数（新製品の計画）は手入力の版 manual に置き、取り込みの版に重ねる。
"""

from bomcost import (
    MANUAL_VERSION_ID,
    BomLine,
    CostHead,
    CostRow,
    Item,
    MoldCost,
    ReuseTarget,
    Scope,
    ScopePolicy,
    Version,
    Volume,
    overlay_manual,
)

M = CostHead.MATERIAL

PRODUCT = "P"  # 区分の記号（架空）: 製品
PART = "B"  # 部品


def items() -> list[Item]:
    return [
        Item("A-100", "土台の製品", PRODUCT, has_children=1),
        Item("V-110", "新しい製品", PRODUCT, has_children=1),
        Item("K-200", "既存品", PRODUCT, has_children=0),
        Item("B-200", "部品B", PART),
        Item("C-300", "部品C", PART),
        Item("F-600", "部品F", PART),
    ]


def bom_lines() -> list[BomLine]:
    return [
        BomLine("A-100", "1", "B-200", 2),
        BomLine("A-100", "2", "C-300", 3),
        BomLine("V-110", "1", "A-100", 1),
        BomLine("V-110", "2", "F-600", 1),
        BomLine("V-110", "3", "C-300", -1),
    ]


def manual() -> Version:
    """手入力の分: 試作品の品目と原価、新製品の年間台数（計画）。"""
    v = Version(MANUAL_VERSION_ID)
    v.add_item(Item("X-610", "試作品", PART))
    v.add_cost(CostRow("X-610", {M: 200}))
    v.add_volume(Volume("V-110", 10000))
    return v


def imported_2026_10() -> Version:
    v = Version("2026-10")
    v.add_items(items())
    v.add_bom_lines(bom_lines())
    v.add_costs(
        [
            CostRow("A-100", {M: 200}),
            CostRow("B-200", {M: 1600}),
            CostRow("C-300", {M: 500}),
            CostRow("F-600", {M: 250}),
            CostRow("K-200", {M: 3000}),
        ]
    )
    v.add_volume(Volume("K-200", 5000))
    return v


def imported_2026_12() -> Version:
    v = Version("2026-12")
    v.add_items(items())
    v.add_bom_lines(bom_lines())
    v.add_costs(
        [
            CostRow("A-100", {M: 200}),
            CostRow("B-200", {M: 1600}),
            CostRow("C-300", {M: 520}),
            CostRow("F-600", {M: 250}),
        ]
    )
    return v


def version_2026_10() -> Version:
    return overlay_manual(imported_2026_10(), manual())


def version_2026_12() -> Version:
    return overlay_manual(imported_2026_12(), manual())


def policy() -> ScopePolicy:
    """A-100 の材料費は「自分だけ」（登録値 200 に子を足す）。"""
    p = ScopePolicy()
    p.set_item("A-100", M, Scope.OWN_ONLY)
    return p


def mold_x610() -> MoldCost:
    return MoldCost("X-610", 2400000, 2)


def reuse_k200() -> ReuseTarget:
    return ReuseTarget("X-610", "K-200", 2, replaced_part="F-600")
