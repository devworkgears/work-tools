"""画面を試すための架空の例の DB。中身はテストの tests/example_data.py と同じ（すべて材料費・円）。区分の記号だけ製品 2・部品 1。

品番・名称・数値・区分の記号はすべて架空。
"""

from datetime import datetime
from pathlib import Path

from .group import PART_KIND_CODE, PRODUCT_KIND_CODE
from .model import MANUAL_VERSION_ID, BomLine, CostHead, CostRow, Item, Version, Volume
from .mold import MoldCost, ReuseTarget
from .rollup import Scope, ScopePolicy
from .scenario import Change, ChangeKind, Scenario
from .store import Store

M = CostHead.MATERIAL
PRODUCT = PRODUCT_KIND_CODE  # 区分の記号: 製品
PART = PART_KIND_CODE  # 部品
DEMO_SCENARIO_NAME = "V-110 検討"
DEMO_DEV_NUMBER = "D-100"  # 2つのグループはこの開発番号
DEMO_DEV_NAME = "例のユニット"


def _items() -> list[Item]:
    return [
        Item("A-100", "土台の製品", PRODUCT, has_children=1),
        Item("V-110", "新しい製品", PRODUCT, has_children=1),
        Item("K-200", "既存品", PRODUCT, has_children=0),
        Item("B-200", "部品B", PART),
        Item("C-300", "部品C", PART),
        Item("F-600", "部品F", PART),
    ]


def _bom_lines() -> list[BomLine]:
    return [
        BomLine("A-100", "1", "B-200", 2),
        BomLine("A-100", "2", "C-300", 3),
        BomLine("V-110", "1", "A-100", 1),
        BomLine("V-110", "2", "F-600", 1),
        BomLine("V-110", "3", "C-300", -1),
    ]


def demo_manual() -> Version:
    """手入力の分: 試作品の品目と原価、新製品の年間台数（計画）。"""
    v = Version(MANUAL_VERSION_ID)
    v.add_item(Item("X-610", "試作品", PART))
    v.add_cost(CostRow("X-610", {M: 200}))
    v.add_volume(Volume("V-110", 10000))
    return v


def demo_versions() -> list[Version]:
    """グループ 2026-10 と、その原価を取り込み直した 2026-12 の BOM（マスタ登録は重ねない）。"""
    v10 = Version("2026-10", datetime(2026, 10, 1), "架空の例", "例の取り込み", dev_number=DEMO_DEV_NUMBER,
                  dev_name=DEMO_DEV_NAME)
    v10.add_items(_items())
    v10.add_bom_lines(_bom_lines())
    v10.add_costs(
        [
            CostRow("A-100", {M: 200}),
            CostRow("B-200", {M: 1600}),
            CostRow("C-300", {M: 500}),
            CostRow("F-600", {M: 250}),
            CostRow("K-200", {M: 3000}),
        ]
    )
    v10.add_volume(Volume("K-200", 5000))

    v12 = Version("2026-12", datetime(2026, 12, 1), "原価の更新", "例の取り込み", dev_number=DEMO_DEV_NUMBER,
                  dev_name=DEMO_DEV_NAME)
    v12.add_items(_items())
    v12.add_bom_lines(_bom_lines())
    v12.add_costs(
        [
            CostRow("A-100", {M: 200}),
            CostRow("B-200", {M: 1600}),
            CostRow("C-300", {M: 520}),
            CostRow("F-600", {M: 250}),
        ]
    )
    return [v10, v12]


def demo_policy() -> ScopePolicy:
    """A-100 の材料費は「自分だけ」（登録値 200 に子を足す）。"""
    p = ScopePolicy()
    p.set_item("A-100", M, Scope.OWN_ONLY)
    return p


def build_demo(path: str | Path) -> int:
    """架空の例を入れた DB を path に作り、グループ 2026-10 の A-100 の試算（V-110）の ID を返す。path にファイルがあれば止める。

    2つのグループは同じ開発番号で、どちらにも同じマスタ登録・型費・流用先を入れる（グループどうしは共有しない）。
    """
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} はもうある（作り直すなら消すか、別の名前にする）")
    with Store(path) as store:
        for version in demo_versions():
            store.save_group(version)
            store.save_manual(demo_manual(), version.version_id)
            store.save_mold_cost(MoldCost("X-610", 2400000, 2), version.version_id)
            store.add_reuse_target(ReuseTarget("X-610", "K-200", 2, replaced_part="F-600"), version.version_id)
        store.save_scope_policy(demo_policy())
        version = store.load_group("2026-10")
        scenario = Scenario(DEMO_SCENARIO_NAME, "A-100", "V-110", version, M, store.load_scope_policy())
        volume = version.volume("V-110")
        annual = None if volume is None else volume.annual
        scenario.apply(Change(ChangeKind.ADD, "F-600", 1), volume=annual)
        scenario.apply(Change(ChangeKind.REMOVE, "C-300", 1), volume=annual)
        return store.save_scenario(scenario)
