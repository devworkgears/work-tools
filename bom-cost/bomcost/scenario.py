"""差分モードの試算: 土台の製品に変更（追加・削除・置換・員数変更）を順に重ね、履歴を残す。

試算の結果の構成は「仮の製品 → 土台 ×1 ＋ 変更の行」で、仮の製品の子を上書きして計算する。
"""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Iterable

from .formula import EDITABLE_HEADS, Head, OwnValues, registered_own
from .model import BomLine, Code, CostHead, CostRow, Version
from .mold import Allocation, usage_per_unit
from .normalize import NumberLike, to_code, to_decimal, to_optional_code
from .rollup import CostResult, ScopePolicy, calculate_cost


class ChangeKind(Enum):
    ADD = "追加"
    REMOVE = "削除"
    REPLACE = "置換"
    SET_QUANTITY = "員数変更"
    SET_HEAD = "費目変更"  # 製品の社内加工費・償却費・直接費を書き換える（構成は変えない）


@dataclass(frozen=True)
class Change:
    """parent を付けた置換・員数変更は、その親の風船番号 balloon（空欄可）の場所だけを変える。

    quantity は置換なら代わりの部品の員数、場所の員数変更ならその場所の新しい員数、費目変更なら費目 target_head
    （formula.EDITABLE_HEADS の id）の新しい値。費目変更の part は製品の品番。
    """

    kind: ChangeKind
    part: str
    quantity: Decimal = Decimal(1)
    replacement: str | None = None
    parent: str | None = None
    balloon: str | None = None
    target_head: str | None = None

    def __post_init__(self) -> None:
        if (self.kind is ChangeKind.SET_HEAD) != (self.target_head is not None):
            raise ValueError("費目（target_head）を付けるのは費目変更だけで、費目変更には要る")
        if self.target_head is not None and self.target_head not in {h.name for h in EDITABLE_HEADS}:
            raise ValueError(f"書き換えられる費目でない: {self.target_head}（"
                             + "・".join(h.value for h in EDITABLE_HEADS) + "）")
        object.__setattr__(self, "part", to_code(self.part))
        object.__setattr__(self, "quantity", to_decimal(self.quantity, name="数量"))
        if self.replacement is not None:
            object.__setattr__(self, "replacement", to_code(self.replacement, name="代わりの品番"))
        object.__setattr__(self, "parent", to_optional_code(self.parent, name="親品番"))
        object.__setattr__(self, "balloon", to_optional_code(self.balloon, name="風船番号"))
        if self.kind is ChangeKind.REPLACE and self.replacement is None:
            raise ValueError("置換には代わりの品番が要る")
        if self.kind in (ChangeKind.ADD, ChangeKind.REMOVE, ChangeKind.REPLACE) and self.quantity <= 0:
            raise ValueError("追加・削除・置換の数量は正の数で渡す")
        if self.parent is not None and self.kind not in (ChangeKind.REPLACE, ChangeKind.SET_QUANTITY):
            raise ValueError("場所（親・風船番号）を付けられるのは置換と員数変更だけ")
        if self.parent is not None and self.kind is ChangeKind.SET_QUANTITY and self.quantity < 0:
            raise ValueError("員数は 0 以上で渡す")
        if self.balloon is not None and self.parent is None:
            raise ValueError("風船番号には親品番が要る")
        if self.kind is ChangeKind.SET_HEAD:
            if self.parent is not None or self.replacement is not None:
                raise ValueError("費目変更には場所・代わりの品番を付けない")
            if self.quantity < 0:
                raise ValueError("費目の値は 0 以上で渡す")

    @property
    def location(self) -> tuple[str, str | None, str] | None:
        """場所つきの置換・員数変更なら（親品番, 風船番号, 品番）。"""
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


class Scenario:
    """試算ケース（グループ × 製品の変更）。土台の製品と、計算に使う版（グループ）・費目・計上範囲を持つ。

    name は画面の表示だけに使う（保存しない）。
    """

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
        merged: dict[str, Decimal] = {}
        for code, qty, _, _ in self.root_entries(changes):
            merged[code] = merged.get(code, Decimal(0)) + qty
        return tuple(
            BomLine(self.product_code, None, code, qty) for code, qty in merged.items() if qty != 0
        )

    def root_entries(self, changes: tuple[Change, ...] | None = None) -> list[tuple[str, Decimal, bool, str | None]]:
        """root_lines の合算前の行（品番・製品1台あたりの数量・土台か・場所つきの変更ならその場所の親）。先頭は土台 ×1。"""
        entries: list[list] = [[self.base, Decimal(1), True, None]]
        removed: set[tuple[str, str | None, str]] = set()  # 1回の置換の代わりの部品が何行でも、場所の部品は1回だけ抜く
        usage: dict[str, Decimal] | None = None
        for change in self.changes if changes is None else changes:
            if change.kind is ChangeKind.SET_HEAD:
                continue
            location = change.location
            if location is None:
                _apply(entries, change)
                continue
            if usage is None:
                usage = usage_per_unit(self.version, self.base)
            per_unit = Decimal(1) if change.parent == self.base else usage.get(change.parent, Decimal(0))
            if location not in removed:
                removed.add(location)
                entries.append([change.part, -self.location_quantity(location) * per_unit, False, change.parent])
            # 場所の員数変更は、その場所を同じ部品の新しい員数に置き換えるのと同じ
            entries.append([change.replacement or change.part, change.quantity * per_unit, False, change.parent])
        return [(e[0], e[1], e[2], e[3]) for e in entries]

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

    def inherited_costs(self, version: Version | None = None, changes: tuple[Change, ...] | None = None
                        ) -> dict[str, dict[CostHead, Decimal]]:
        """置換（省けば履歴の全部）の代わりの部品が、版で空の費目に引き継ぐ置き換えた元の部品の値（品番 → 費目 → 値）。

        同じ代わりの部品を、その費目の値が食い違う（片方が空も含む）元の部品の置換に使ったら、その費目は引き継がない。
        元の部品の値は版の値で、版で空なら先の置換で引き継いだ値。
        """
        version = version or self.version
        found: dict[str, dict[CostHead, set[Decimal | None]]] = {}

        def value(code: str, head: CostHead) -> Decimal | None:
            own = version.cost_value(code, head)
            seen = found.get(code, {}).get(head, set())
            return own if own is not None or len(seen) != 1 else next(iter(seen))

        for change in self.changes if changes is None else changes:
            if change.kind is not ChangeKind.REPLACE or change.replacement == change.part:
                continue
            for head in _INHERITED_HEADS:
                if version.cost_value(change.replacement, head) is None:
                    found.setdefault(change.replacement, {}).setdefault(head, set()).add(value(change.part, head))
        result: dict[str, dict[CostHead, Decimal]] = {}
        for code, heads in found.items():
            values = {h: v for h, seen in heads.items() if len(seen) == 1 and (v := next(iter(seen))) is not None}
            if values:
                result[code] = values
        return result

    def priced_version(self, version: Version | None = None, changes: tuple[Change, ...] | None = None) -> Version:
        """版（省けば試算の版）に、置換の代わりの部品が引き継ぐ値（inherited_costs）を入れた版。引き継ぎが無ければ版そのもの。"""
        version = version or self.version
        inherited = self.inherited_costs(version, changes)
        if not inherited:
            return version
        rows = []
        for code, values in inherited.items():
            row = version.cost_row(code)
            merged = {h: values.get(h, None if row is None else row.value(h)) for h in CostHead}
            rows.append(CostRow(code, merged) if row is None else replace(row, values=merged))
        return version.with_costs(rows)

    def own_values(self, version: Version | None = None, changes: tuple[Change, ...] | None = None) -> OwnValues:
        """試算の製品自身の値: 土台の製品の登録値に、費目変更（省けば履歴の全部）を順に当てたもの。"""
        own = registered_own(version or self.version, self.base)
        for change in self.changes if changes is None else changes:
            if change.kind is ChangeKind.SET_HEAD and change.target_head is not None:
                own = own.with_value(Head[change.target_head], change.quantity)
        return own

    def calculate(
        self,
        version: Version | None = None,
        head: CostHead | None = None,
        changes: tuple[Change, ...] | None = None,
    ) -> CostResult:
        """同じ土台＋変更の履歴を、指定の版（省けば試算の版）の原価で計算する（置換の代わりの部品は priced_version の値）。"""
        version = self.priced_version(version, changes)
        return calculate_cost(
            version,
            self.product_code,
            head or self.head,
            self.policy,
            self.overrides(changes),
            {self.product_code: self.own_values(version, changes)},
        )

    def apply(
        self,
        change: Change,
        recorded_at: datetime | None = None,
        volume: NumberLike | None = None,
        allocation: Allocation | None = None,
    ) -> ChangeRecord:
        """volume（試算の製品の年間台数）と allocation（型費の載せ方）は、履歴に前提として残すだけ。

        部品とその中の場所を両方変える変更は ValueError（nesting_conflict）。
        """
        reason = self.nesting_conflict(change)
        if reason is not None:
            raise ValueError(reason)
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
        kept = list(self._history)
        try:
            for change in changes:
                record = self.apply(change, recorded_at, volume, allocation)
                record = ChangeRecord(**{**record.__dict__, "batch": batch})
                self._history[-1] = record
                records.append(record)
        except Exception:
            self._history = kept
            raise
        return tuple(records)

    def replaced_locations(self) -> set[tuple[str, str | None, str]]:
        return {c.location for c in self.changes if c.location is not None}

    def locks(self, changes: tuple[Change, ...] | None = None) -> tuple[dict[str, str], dict[str, str]]:
        """変更（省けば履歴の全部）で、もう変えられなくなった所。

        1つ目は 親の品番 → 変えた部品: その親の下の場所は変えられない（変えた部品とその中の組立品）。
        2つ目は 品番 → 中で変えた場所の名前: その品番は変えられない（場所の親とその上の組立品）。
        場所の変更は版の構成の使用数で倍率を取るので（root_entries）、両方変えると原価が誤る。
        """
        inside: dict[str, str] = {}
        outer: dict[str, str] = {}
        below: dict[str, set[str]] = {}

        def under(code: str) -> set[str]:
            if code not in below:
                below[code] = {code, *usage_per_unit(self.version, code)}
            return below[code]

        for change in self.changes if changes is None else changes:
            if change.kind in _PART_CHANGES:
                for code in under(change.part):
                    inside.setdefault(code, change.part)
            if change.location is not None:
                for code in (self.base, *usage_per_unit(self.version, self.base)):
                    if change.parent in under(code):
                        outer.setdefault(code, _label(change))
            elif change.kind in _PART_CHANGES:
                # 場所なしの変更は製品の中のどこにあっても効くので、その部品の上の組立品も変えられない
                for code in (self.base, *usage_per_unit(self.version, self.base)):
                    if code != change.part and change.part in under(code):
                        outer.setdefault(code, _label(change))
        return inside, outer

    def nesting_conflict(self, change: Change, changes: tuple[Change, ...] | None = None) -> str | None:
        """change を changes（省けば履歴の全部）に足すと部品とその中の場所を両方変えるなら、断る理由の文。"""
        inside, outer = self.locks(changes)
        if change.location is not None and change.parent in inside or (
                change.location is None and change.kind in _PART_CHANGES
                and inside.get(change.part, change.part) != change.part):
            done = inside[change.parent or change.part]
            return f"{done} を変えたので、その中の {_label(change)} は変えられない（親子に重なる・先に {done} の変更を外す）"
        if change.kind in _PART_CHANGES and change.part in outer:
            done = outer[change.part]
            return f"{done} を変えたので、{_label(change)} は変えられない（親子に重なる・先に {done} の変更を外す）"
        return None

    def undo(self) -> ChangeRecord | None:
        """最後の操作を取り消す（まとまりなら丸ごと）。取り消した行の最後の1行を返す。"""
        if not self._history:
            return None
        last = self._history.pop()
        if last.batch is not None:
            while self._history and self._history[-1].batch == last.batch:
                self._history.pop()
        return last

    def remove_batch(self, sequence: int) -> tuple[ChangeRecord, ...]:
        """sequence で始まる1回の操作（まとまりなら丸ごと）を、途中のものでも抜く。

        後ろの行の番号（sequence・batch）は詰め直す。原価の記録はそのまま残す。抜いた行を返す（無ければ空）。
        """
        removed = tuple(r for r in self._history if r.sequence == sequence or r.batch == sequence)
        if not removed or removed[0].sequence != sequence or removed[0].batch not in (None, sequence):
            return ()
        gone = {r.sequence for r in removed}
        kept = [r for r in self._history if r.sequence not in gone]
        shift = len(removed)
        self._history = [
            r if r.sequence < sequence else ChangeRecord(**{
                **r.__dict__,
                "sequence": r.sequence - shift,
                "batch": None if r.batch is None else r.batch - shift,
            })
            for r in kept
        ]
        return removed

    def clear(self) -> None:
        self._history.clear()


_PART_CHANGES = (ChangeKind.REMOVE, ChangeKind.REPLACE, ChangeKind.SET_QUANTITY)  # 部品そのものを変える変更
# 置換で引き継ぐのは計算に入る登録の費目だけ。登録の製造原価などを写すと、代わりの部品自身の値と食い違い二重計上の判定を誤らせる
_INHERITED_HEADS = (CostHead.MATERIAL, CostHead.AMORTIZATION, CostHead.DIRECT, CostHead.PROCESSING)


def _label(change: Change) -> str:
    if change.parent is None:
        return change.part
    return f"{change.parent} の {change.part}" if change.balloon is None else f"{change.parent} の #{change.balloon} {change.part}"


def _apply(entries: list[list], change: Change) -> None:
    if change.kind is ChangeKind.ADD:
        entries.append([change.part, change.quantity, False, None])
    elif change.kind is ChangeKind.REMOVE:
        entries.append([change.part, -change.quantity, False, None])
    elif change.kind is ChangeKind.SET_QUANTITY:
        entries[:] = [e for e in entries if e[0] != change.part]
        entries.append([change.part, change.quantity, False, None])
    elif change.kind is ChangeKind.REPLACE:
        # 足した部品を替えるときは、その行の品番を差し替える。土台の中の部品は「抜く＋足す」で表す
        for entry in entries:
            if entry[0] == change.part and entry[1] > 0 and not entry[2]:
                entry[0] = change.replacement
                return
        entries.append([change.part, -change.quantity, False, None])
        entries.append([change.replacement, change.quantity, False, None])
