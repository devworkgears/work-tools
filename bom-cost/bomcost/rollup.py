"""原価の積み上げ: 品目の原価 ＝ 自分の原価 ＋ Σ（子の数量 × 子の原価）。"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence

from .model import BomConflict, BomLine, Code, CostHead, Version
from .normalize import to_code

# 構成の上書き（親品番 → その子の行）。差分モードの試算は、仮の製品の子をこれで差し替える。
StructureOverrides = Mapping[str, Sequence[BomLine]]


class Scope(Enum):
    INCLUDES_CHILDREN = "下位込み"  # 登録値がすでに子を含む総額。子を足さない
    OWN_ONLY = "自分だけ"  # 子を足す


class ScopePolicy:
    """費目ごとの計上範囲。費目の既定と、品目×費目の上書きを持つ。

    既定は全費目「下位込み」（登録値をそのまま使う側が二重計上を起こさないため）。
    登録値が未登録の親は、範囲によらず子から足し上げる。
    """

    def __init__(
        self,
        defaults: Mapping[CostHead, Scope] | None = None,
        overrides: Mapping[tuple[Code, CostHead], Scope] | None = None,
    ) -> None:
        self._defaults: dict[CostHead, Scope] = {head: Scope.INCLUDES_CHILDREN for head in CostHead}
        self._defaults.update(defaults or {})
        self._overrides: dict[tuple[str, CostHead], Scope] = {}
        for (code, head), scope in (overrides or {}).items():
            self.set_item(code, head, scope)

    def set_head(self, head: CostHead, scope: Scope) -> None:
        self._defaults[head] = scope

    def set_item(self, code: Code, head: CostHead, scope: Scope) -> None:
        self._overrides[(to_code(code), head)] = scope

    def scope_for(self, code: Code, head: CostHead) -> Scope:
        return self._overrides.get((to_code(code), head), self._defaults[head])

    @property
    def defaults(self) -> dict[CostHead, Scope]:
        return dict(self._defaults)

    @property
    def overrides(self) -> dict[tuple[str, CostHead], Scope]:
        return dict(self._overrides)


@dataclass(frozen=True)
class CostResult:
    root: str
    head: CostHead
    version_id: str
    total: Decimal | None
    item_costs: Mapping[str, Decimal | None]
    missing: tuple[str, ...]  # 原価が未登録の品目（0 円扱いせず足し上げから外した）
    conflicts: tuple[BomConflict, ...]

    @property
    def complete(self) -> bool:
        return not self.missing and not self.conflicts


class CostCalculator:
    """1つの版・1つの費目で、品番ごとに1回だけ計算して使い回す。"""

    def __init__(
        self,
        version: Version,
        head: CostHead = CostHead.MATERIAL,
        policy: ScopePolicy | None = None,
        overrides: StructureOverrides | None = None,
    ) -> None:
        if head is CostHead.AMORTIZATION:
            raise ValueError("償却費の登録値は計算から外す（型費は別枠で扱う）")
        self.version = version
        self.head = head
        self.policy = policy or ScopePolicy()
        self._overrides = {to_code(k): tuple(v) for k, v in (overrides or {}).items()}
        self._memo: dict[str, Decimal | None] = {}
        self._missing: dict[str, None] = {}
        self._visited_parents: dict[str, None] = {}
        self._in_progress: set[str] = set()

    def children(self, code: str) -> tuple[BomLine, ...]:
        if code in self._overrides:
            return self._overrides[code]
        return self.version.children(code)

    def item_cost(self, code: Code) -> Decimal | None:
        code = to_code(code)
        if code in self._memo:
            return self._memo[code]
        if code in self._in_progress:
            raise ValueError(f"構成が循環している: {code}")
        self._in_progress.add(code)
        try:
            value = self._compute(code)
        finally:
            self._in_progress.discard(code)
        self._memo[code] = value
        return value

    def _compute(self, code: str) -> Decimal | None:
        # 構成を上書きした品目は試算の仮の製品なので、同じ品番の登録値は使わない（使うと変更が効かなくなる）
        own = None if code in self._overrides else self.version.cost_value(code, self.head)
        lines = self.children(code)
        if not lines:
            if own is None:
                self._missing[code] = None
            return own
        if own is not None and self.policy.scope_for(code, self.head) is Scope.INCLUDES_CHILDREN:
            return own
        if own is None:
            self._missing[code] = None
        self._visited_parents[code] = None
        total = own if own is not None else Decimal(0)
        for line in lines:
            child_cost = self.item_cost(line.child)
            if child_cost is not None:
                total += line.quantity * child_cost
        return total

    def calculate(self, root: Code) -> CostResult:
        root = to_code(root)
        total = self.item_cost(root)
        return CostResult(
            root=root,
            head=self.head,
            version_id=self.version.version_id,
            total=total,
            item_costs=dict(self._memo),
            missing=tuple(self._missing),
            conflicts=self.version.conflicts_under(self._visited_parents),
        )


def calculate_cost(
    version: Version,
    root: Code,
    head: CostHead = CostHead.MATERIAL,
    policy: ScopePolicy | None = None,
    overrides: StructureOverrides | None = None,
) -> CostResult:
    """root の原価を1つの費目で計算する。"""
    return CostCalculator(version, head, policy, overrides).calculate(root)
