"""差分モードの試算: 土台の製品に変更（足す・抜く・替える・数量）を順に重ね、履歴を残す。

試算の結果の構成は「仮の製品 → 土台 ×1 ＋ 変更の行」で、仮の製品の子を上書きして計算する。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .model import BomLine, Code, CostHead, Version
from .normalize import NumberLike, to_code, to_decimal
from .rollup import CostResult, ScopePolicy, calculate_cost


class ChangeKind(Enum):
    ADD = "足す"
    REMOVE = "抜く"
    REPLACE = "替える"
    SET_QUANTITY = "数量"


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    part: str
    quantity: Decimal = Decimal(1)
    replacement: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "part", to_code(self.part))
        object.__setattr__(self, "quantity", to_decimal(self.quantity, name="数量"))
        if self.replacement is not None:
            object.__setattr__(self, "replacement", to_code(self.replacement, name="代わりの品番"))
        if self.kind is ChangeKind.REPLACE and self.replacement is None:
            raise ValueError("替えるには代わりの品番が要る")
        if self.kind in (ChangeKind.ADD, ChangeKind.REMOVE, ChangeKind.REPLACE) and self.quantity <= 0:
            raise ValueError("足す・抜く・替える数量は正の数で渡す")


@dataclass(frozen=True)
class ChangeRecord:
    """履歴の1行。そのときの原価と前提（版・費目）を残し、版を入れ替えても判断した時点の数字が残る。"""

    sequence: int
    change: Change
    version_id: str
    head: CostHead
    cost_before: Decimal | None
    cost_after: Decimal | None
    recorded_at: datetime

    @property
    def difference(self) -> Decimal | None:
        if self.cost_before is None or self.cost_after is None:
            return None
        return self.cost_after - self.cost_before


@dataclass(frozen=True)
class LineComparison:
    part: str
    quantity: Decimal
    amount_a: Decimal | None  # 数量 × 版 a の原価
    amount_b: Decimal | None

    @property
    def difference(self) -> Decimal | None:
        if self.amount_a is None or self.amount_b is None:
            return None
        return self.amount_b - self.amount_a


@dataclass(frozen=True)
class VersionComparison:
    version_a: str
    version_b: str
    head: CostHead
    lines: tuple[LineComparison, ...]
    total_a: Decimal | None
    total_b: Decimal | None

    @property
    def difference(self) -> Decimal | None:
        if self.total_a is None or self.total_b is None:
            return None
        return self.total_b - self.total_a


class Scenario:
    """試算ケース。土台の製品と、計算に使う版・費目・計上範囲を持つ。"""

    def __init__(
        self,
        name: str,
        base: Code,
        product_code: Code,
        version: Version,
        head: CostHead = CostHead.MATERIAL,
        policy: ScopePolicy | None = None,
        note: str = "",
    ) -> None:
        self.name = name
        self.base = to_code(base, name="土台の品番")
        self.product_code = to_code(product_code, name="試算の製品の品番")
        self.version = version
        self.head = head
        self.policy = policy or ScopePolicy()
        self.note = note
        self._history: list[ChangeRecord] = []

    @property
    def history(self) -> tuple[ChangeRecord, ...]:
        return tuple(self._history)

    @property
    def changes(self) -> tuple[Change, ...]:
        return tuple(r.change for r in self._history)

    def root_lines(self, changes: tuple[Change, ...] | None = None) -> tuple[BomLine, ...]:
        """土台 ×1 に変更（省けば履歴の全部）を順に当てた、仮の製品の直下の行（同じ品番は合算）。"""
        entries: list[list] = [[self.base, Decimal(1), True]]  # 品番・数量・土台か
        for change in self.changes if changes is None else changes:
            _apply(entries, change)
        merged: dict[str, Decimal] = {}
        for code, qty, _ in entries:
            merged[code] = merged.get(code, Decimal(0)) + qty
        return tuple(
            BomLine(self.product_code, None, code, qty) for code, qty in merged.items() if qty != 0
        )

    def overrides(self, changes: tuple[Change, ...] | None = None) -> dict[str, tuple[BomLine, ...]]:
        return {self.product_code: self.root_lines(changes)}

    def calculate(
        self,
        version: Version | None = None,
        head: CostHead | None = None,
        changes: tuple[Change, ...] | None = None,
    ) -> CostResult:
        """同じ土台＋変更の履歴を、指定の版（省けば試算の版）の原価で計算する。"""
        return calculate_cost(
            version or self.version,
            self.product_code,
            head or self.head,
            self.policy,
            self.overrides(changes),
        )

    def apply(self, change: Change, recorded_at: datetime | None = None) -> ChangeRecord:
        before = self.calculate().total
        after = self.calculate(changes=self.changes + (change,)).total
        record = ChangeRecord(
            sequence=len(self._history) + 1,
            change=change,
            version_id=self.version.version_id,
            head=self.head,
            cost_before=before,
            cost_after=after,
            recorded_at=recorded_at or datetime.now(),
        )
        self._history.append(record)
        return record

    def undo(self) -> ChangeRecord | None:
        return self._history.pop() if self._history else None

    def clear(self) -> None:
        self._history.clear()

    def compare_versions(
        self, version_a: Version, version_b: Version, head: CostHead | None = None
    ) -> VersionComparison:
        """同じ試算を2つの版で計算し直し、直下の品目ごとの差を出す。"""
        head = head or self.head
        result_a = self.calculate(version_a, head)
        result_b = self.calculate(version_b, head)
        lines = tuple(
            LineComparison(
                part=line.child,
                quantity=line.quantity,
                amount_a=_amount(line.quantity, result_a.item_costs.get(line.child)),
                amount_b=_amount(line.quantity, result_b.item_costs.get(line.child)),
            )
            for line in self.root_lines()
        )
        return VersionComparison(
            version_a.version_id, version_b.version_id, head, lines, result_a.total, result_b.total
        )


def _amount(quantity: Decimal, unit: Decimal | None) -> Decimal | None:
    return None if unit is None else quantity * unit


def _apply(entries: list[list], change: Change) -> None:
    if change.kind is ChangeKind.ADD:
        entries.append([change.part, change.quantity, False])
    elif change.kind is ChangeKind.REMOVE:
        entries.append([change.part, -change.quantity, False])
    elif change.kind is ChangeKind.SET_QUANTITY:
        entries[:] = [e for e in entries if e[0] != change.part]
        entries.append([change.part, change.quantity, False])
    elif change.kind is ChangeKind.REPLACE:
        # 足した部品を替えるときは、その行の品番を差し替える。土台の中の部品は「抜く＋足す」で表す
        for entry in entries:
            if entry[0] == change.part and entry[1] > 0 and not entry[2]:
                entry[0] = change.replacement
                return
        entries.append([change.part, -change.quantity, False])
        entries.append([change.replacement, change.quantity, False])
