"""差分モードの試算: 土台の製品に変更（足す・抜く・替える・数量）を順に重ね、履歴を残す。

試算の結果の構成は「仮の製品 → 土台 ×1 ＋ 変更の行」で、仮の製品の子を上書きして計算する。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Iterable

from .model import BomLine, Code, CostHead, Version
from .mold import Allocation, usage_per_unit
from .normalize import NumberLike, to_code, to_decimal, to_optional_code
from .rollup import CostResult, ScopePolicy, calculate_cost


class ChangeKind(Enum):
    ADD = "足す"
    REMOVE = "抜く"
    REPLACE = "替える"
    SET_QUANTITY = "数量"


@dataclass(frozen=True)
class Change:
    """parent を付けた置換は、その親の風船番号 balloon（空欄可）の場所だけを替える。quantity は代わりの部品の員数。"""

    kind: ChangeKind
    part: str
    quantity: Decimal = Decimal(1)
    replacement: str | None = None
    parent: str | None = None
    balloon: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "part", to_code(self.part))
        object.__setattr__(self, "quantity", to_decimal(self.quantity, name="数量"))
        if self.replacement is not None:
            object.__setattr__(self, "replacement", to_code(self.replacement, name="代わりの品番"))
        object.__setattr__(self, "parent", to_optional_code(self.parent, name="親品番"))
        object.__setattr__(self, "balloon", to_optional_code(self.balloon, name="風船番号"))
        if self.kind is ChangeKind.REPLACE and self.replacement is None:
            raise ValueError("替えるには代わりの品番が要る")
        if self.kind in (ChangeKind.ADD, ChangeKind.REMOVE, ChangeKind.REPLACE) and self.quantity <= 0:
            raise ValueError("足す・抜く・替える数量は正の数で渡す")
        if self.parent is not None and self.kind is not ChangeKind.REPLACE:
            raise ValueError("場所（親・風船番号）を付けられるのは置換だけ")
        if self.balloon is not None and self.parent is None:
            raise ValueError("風船番号には親品番が要る")

    @property
    def location(self) -> tuple[str, str | None, str] | None:
        """場所つきの置換なら（親品番, 風船番号, 品番）。"""
        return None if self.parent is None else (self.parent, self.balloon, self.part)


@dataclass(frozen=True)
class ChangeRecord:
    """履歴の1行。そのときの原価と前提（版・費目・台数・型費の載せ方）を残し、版を入れ替えても判断した時点の数字が残る。

    台数は試算の製品の年間台数、載せ方は型費の載せ方。決めていなければ None。
    """

    sequence: int
    change: Change
    version_id: str
    head: CostHead
    cost_before: Decimal | None
    cost_after: Decimal | None
    recorded_at: datetime
    volume: Decimal | None = None
    allocation: Allocation | None = None
    batch: int | None = None  # 1回の操作のまとまり（その操作の最初の行の sequence）。1行だけの操作は None

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
        history: Iterable[ChangeRecord] = (),
        scenario_id: int | None = None,
    ) -> None:
        """history は保存から読み戻すときに渡す。scenario_id は保存したときに振られる。"""
        self.name = name
        self.base = to_code(base, name="土台の品番")
        self.product_code = to_code(product_code, name="試算の製品の品番")
        self.version = version
        self.head = head
        self.policy = policy or ScopePolicy()
        self.note = note
        self.scenario_id = scenario_id
        self._history: list[ChangeRecord] = list(history)

    @property
    def history(self) -> tuple[ChangeRecord, ...]:
        return tuple(self._history)

    @property
    def changes(self) -> tuple[Change, ...]:
        return tuple(r.change for r in self._history)

    def root_lines(self, changes: tuple[Change, ...] | None = None) -> tuple[BomLine, ...]:
        """土台 ×1 に変更（省けば履歴の全部）を順に当てた、仮の製品の直下の行（同じ品番は合算）。"""
        entries: list[list] = [[self.base, Decimal(1), True]]  # 品番・数量・土台か
        removed: set[tuple[str, str | None, str]] = set()  # 1回の置換の代わりの部品が何行でも、場所の部品は1回だけ抜く
        usage: dict[str, Decimal] | None = None
        for change in self.changes if changes is None else changes:
            location = change.location
            if location is None:
                _apply(entries, change)
                continue
            if usage is None:
                usage = usage_per_unit(self.version, self.base)
            per_unit = Decimal(1) if change.parent == self.base else usage.get(change.parent, Decimal(0))
            if location not in removed:
                removed.add(location)
                entries.append([change.part, -self.location_quantity(location) * per_unit, False])
            entries.append([change.replacement, change.quantity * per_unit, False])
        merged: dict[str, Decimal] = {}
        for code, qty, _ in entries:
            merged[code] = merged.get(code, Decimal(0)) + qty
        return tuple(
            BomLine(self.product_code, None, code, qty) for code, qty in merged.items() if qty != 0
        )

    def location_quantity(self, location: tuple[str, str | None, str]) -> Decimal:
        """版の構成の、その場所（親・風船番号・品番）の員数。場所が版に無ければ 0。"""
        parent, balloon, part = location
        return sum(
            (line.quantity for line in self.version.children(parent, by_balloon=True)
             if line.balloon == balloon and line.child == part),
            Decimal(0),
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

    def apply(
        self,
        change: Change,
        recorded_at: datetime | None = None,
        volume: NumberLike | None = None,
        allocation: Allocation | None = None,
    ) -> ChangeRecord:
        """volume（試算の製品の年間台数）と allocation（型費の載せ方）は、履歴に前提として残すだけ。"""
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
            volume=None if volume is None else to_decimal(volume, name="年間台数"),
            allocation=allocation,
        )
        self._history.append(record)
        return record

    def apply_batch(
        self,
        changes: Iterable[Change],
        recorded_at: datetime | None = None,
        volume: NumberLike | None = None,
        allocation: Allocation | None = None,
    ) -> tuple[ChangeRecord, ...]:
        """1回の操作の変更を続けて足し、同じまとまり（batch）にする。取り消しはまとまりごと。"""
        changes = tuple(changes)
        if not changes:
            raise ValueError("変更が空")
        if len(changes) == 1:
            return (self.apply(changes[0], recorded_at, volume, allocation),)
        recorded_at = recorded_at or datetime.now()
        batch = len(self._history) + 1
        records = []
        for change in changes:
            record = self.apply(change, recorded_at, volume, allocation)
            record = ChangeRecord(**{**record.__dict__, "batch": batch})
            self._history[-1] = record
            records.append(record)
        return tuple(records)

    def replaced_locations(self) -> set[tuple[str, str | None, str]]:
        return {c.location for c in self.changes if c.location is not None}

    def undo(self) -> ChangeRecord | None:
        """最後の操作を取り消す（まとまりなら丸ごと）。取り消した行の最後の1行を返す。"""
        if not self._history:
            return None
        last = self._history.pop()
        if last.batch is not None:
            while self._history and self._history[-1].batch == last.batch:
                self._history.pop()
        return last

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
