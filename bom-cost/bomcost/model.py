"""入力のデータの形（品目・構成行・原価・年間台数）と、取り込み1回分を持つ版。

型の揺れ（int の品番・"1,600" の金額・"1" のフラグ・float の数量）は各クラスの作成時に
normalize の関数で1回だけそろえる。引数で品番を受ける Version のメソッドも同じ関数を通す。
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Iterable, Mapping

from .normalize import NumberLike, to_code, to_decimal, to_flag, to_optional_code, to_optional_decimal


class CostHead(Enum):
    MATERIAL = "材料費"
    DIRECT = "直接費"
    INDIRECT = "間接費"
    ADJUSTMENT = "調整費"
    AMORTIZATION = "償却費"
    MANUFACTURING = "製造原価"


# 製造原価と比べる4費目
BASIC_HEADS: tuple[CostHead, ...] = (
    CostHead.MATERIAL,
    CostHead.DIRECT,
    CostHead.INDIRECT,
    CostHead.ADJUSTMENT,
)


class ItemKind(Enum):
    PRODUCT = "製品"
    PART = "部品"


class Lifecycle(Enum):
    MASS_PRODUCTION = "量産"
    DISCONTINUED = "廃止"


class Source(Enum):
    IMPORTED = "取り込み"
    MANUAL = "手入力"


class VolumeKind(Enum):
    ACTUAL = "量産の実績"
    PLAN = "新製品の計画"


MBOM_SUFFIX_DIGITS = 3
BASE_SUFFIX = "000"

Code = str | int


def split_mbom_code(mbom_code: Code) -> tuple[str, str]:
    """原価の表の品番（構成の品番＋下3桁）を、構成の品番と下3桁に分ける。"""
    code = to_code(mbom_code, name="原価の品番")
    suffix = code[-MBOM_SUFFIX_DIGITS:]
    if len(code) <= MBOM_SUFFIX_DIGITS or not suffix.isdigit():
        raise ValueError(f"品番の末尾が数字3桁でない: {mbom_code!r}")
    return code[:-MBOM_SUFFIX_DIGITS], suffix


@dataclass(frozen=True)
class Item:
    code: str
    name: str
    kind: ItemKind
    lifecycle: Lifecycle = Lifecycle.MASS_PRODUCTION
    has_children: bool = False
    source: Source = Source.IMPORTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", to_code(self.code))
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "has_children", to_flag(self.has_children, name="子部品の有無"))


@dataclass(frozen=True)
class BomLine:
    """構成行。数量がマイナスの行は抜く部品（正しいデータ）。"""

    parent: str
    balloon: str | None
    child: str
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent", to_code(self.parent, name="親品番"))
        object.__setattr__(self, "balloon", to_optional_code(self.balloon, name="風船番号"))
        object.__setattr__(self, "child", to_code(self.child, name="子品番"))
        object.__setattr__(self, "quantity", to_decimal(self.quantity, name="数量"))

    @property
    def key(self) -> tuple[str, str | None, str]:
        return (self.parent, self.balloon, self.child)


@dataclass(frozen=True)
class CostRow:
    """原価の1行。code は下3桁を外した構成の品番。値の None は未登録（0 円ではない）。"""

    code: str
    values: Mapping[CostHead, Decimal | None]
    suffix: str = BASE_SUFFIX
    source: Source = Source.IMPORTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", to_code(self.code))
        object.__setattr__(self, "suffix", to_code(self.suffix, name="下3桁"))
        normalized = {
            head: to_optional_decimal(self.values.get(head), name=head.value) for head in CostHead
        }
        object.__setattr__(self, "values", normalized)

    @classmethod
    def from_mbom(
        cls,
        mbom_code: Code,
        values: Mapping[CostHead, NumberLike | None],
        source: Source = Source.IMPORTED,
    ) -> "CostRow":
        code, suffix = split_mbom_code(mbom_code)
        return cls(code=code, values=values, suffix=suffix, source=source)

    def value(self, head: CostHead) -> Decimal | None:
        return self.values[head]


@dataclass(frozen=True)
class Volume:
    product: str
    annual: Decimal
    kind: VolumeKind

    def __post_init__(self) -> None:
        object.__setattr__(self, "product", to_code(self.product, name="製品の品番"))
        object.__setattr__(self, "annual", to_decimal(self.annual, name="年間台数"))


@dataclass(frozen=True)
class BomConflict:
    """同じ「親品番＋風船番号＋子品番」で数量が食い違う重複。計算には最初に読んだ数量を使う。"""

    key: tuple[str, str | None, str]
    quantities: tuple[Decimal, ...]


@dataclass
class Version:
    """取り込み1回分の品目・構成・原価・年間台数。"""

    version_id: str
    imported_on: date | None = None
    note: str = ""
    items: dict[str, Item] = field(default_factory=dict)
    _lines: dict[tuple[str, str | None, str], BomLine] = field(default_factory=dict, repr=False)
    _conflicts: dict[tuple[str, str | None, str], list[Decimal]] = field(default_factory=dict, repr=False)
    _costs: dict[tuple[str, str], CostRow] = field(default_factory=dict, repr=False)
    volumes: dict[str, Volume] = field(default_factory=dict)

    def add_item(self, item: Item) -> None:
        self.items[item.code] = item

    def add_items(self, items: Iterable[Item]) -> None:
        for item in items:
            self.add_item(item)

    def add_bom_lines(self, lines: Iterable[BomLine]) -> None:
        """重複行は1つに畳む。何回読み込ませても結果は同じ。数量の食い違いは conflicts に残す。"""
        for line in lines:
            kept = self._lines.get(line.key)
            if kept is None:
                self._lines[line.key] = line
                continue
            if kept.quantity == line.quantity:
                continue
            seen = self._conflicts.setdefault(line.key, [kept.quantity])
            if line.quantity not in seen:
                seen.append(line.quantity)

    def add_cost(self, row: CostRow) -> None:
        self._costs[(row.code, row.suffix)] = row

    def add_costs(self, rows: Iterable[CostRow]) -> None:
        for row in rows:
            self.add_cost(row)

    def add_volume(self, volume: Volume) -> None:
        self.volumes[volume.product] = volume

    @property
    def lines(self) -> tuple[BomLine, ...]:
        return tuple(self._lines.values())

    @property
    def conflicts(self) -> tuple[BomConflict, ...]:
        return tuple(BomConflict(key, tuple(qs)) for key, qs in self._conflicts.items())

    def conflicts_under(self, parents: Iterable[Code]) -> tuple[BomConflict, ...]:
        wanted = {to_code(p) for p in parents}
        return tuple(c for c in self.conflicts if c.key[0] in wanted)

    def children(self, parent: Code, by_balloon: bool = False) -> tuple[BomLine, ...]:
        """子の行。by_balloon=False なら同じ子品番の数量を合算し、風船番号は None にする。"""
        parent = to_code(parent)
        own = [line for line in self._lines.values() if line.parent == parent]
        if by_balloon:
            return tuple(own)
        merged: dict[str, Decimal] = {}
        for line in own:
            merged[line.child] = merged.get(line.child, Decimal(0)) + line.quantity
        return tuple(BomLine(parent, None, child, qty) for child, qty in merged.items())

    def parents(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(line.parent for line in self._lines.values()))

    def cost_row(self, code: Code, suffix: str = BASE_SUFFIX) -> CostRow | None:
        return self._costs.get((to_code(code), to_code(suffix, name="下3桁")))

    def cost_value(self, code: Code, head: CostHead, suffix: str = BASE_SUFFIX) -> Decimal | None:
        row = self.cost_row(code, suffix)
        return None if row is None else row.value(head)

    def volume(self, product: Code) -> Volume | None:
        return self.volumes.get(to_code(product))
