"""入力のデータの形（品目・構成行・原価・年間台数）と、取り込み1回分を持つ版。

型の揺れ（int の品番・"1,600" の金額・"1" のフラグ・float の数量）は各クラスの作成時に
normalize の関数で1回だけそろえる。引数で品番を受ける Version のメソッドも同じ関数を通す。
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Iterable, Mapping

from .normalize import (
    NumberLike,
    to_code,
    to_decimal,
    to_flag,
    to_optional_code,
    to_optional_decimal,
    to_symbol,
)


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


class Source(Enum):
    IMPORTED = "取り込み"
    MANUAL = "手入力"


class VolumeKind(Enum):
    ACTUAL = "量産の実績"
    PLAN = "新製品の計画"


# 手入力の分（マスタ登録）を置く版。この ID は「共通」の登録で、どの版にも重なる。
# 開発番号ごとの登録は manual_version_id(開発番号) の版に置き、その開発番号の版にだけ重なる
MANUAL_VERSION_ID = "manual"


def manual_version_id(dev_number: str = "") -> str:
    """マスタ登録を置く版の ID。開発番号が空なら共通の版 manual。"""
    return f"{MANUAL_VERSION_ID}:{dev_number}" if dev_number else MANUAL_VERSION_ID


def is_manual_version_id(version_id: str) -> bool:
    return version_id == MANUAL_VERSION_ID or version_id.startswith(MANUAL_VERSION_ID + ":")

MBOM_CODE_LENGTH = 11
BASE_VARIATION = "00"

Code = str | int


def base_code(code: Code) -> str:
    """10桁でバリエーションの2桁が 00 の品番は基本の品番（先頭8桁）にする。それ以外はそのまま。"""
    code = to_code(code)
    if len(code) == 10 and code.endswith(BASE_VARIATION):
        return code[:8]
    return code


def mbom_to_ebom(mbom_code: Code) -> str:
    """MBOM の11桁の品番（後ろを 0 で埋めたもの）を EBOM の8桁／10桁の品番に戻す。

    先頭10桁を取り、その後ろ2桁が 00 なら先頭8桁にする。11桁でない・11桁目が 0 でない品番は ValueError。
    """
    code = to_code(mbom_code, name="MBOM の品番")
    if len(code) != MBOM_CODE_LENGTH or not code.endswith("0"):
        raise ValueError(f"MBOM の品番が11桁（末尾 0）でない: {mbom_code!r}")
    return base_code(code[:10])


@dataclass(frozen=True)
class Item:
    """品目。区分（製品／部品）と状態（量産／廃止）は元の記号のまま文字列で持つ（計算には使わない）。"""

    code: str
    name: str
    kind: str = ""
    lifecycle: str = ""
    has_children: bool = False
    source: Source = Source.IMPORTED
    specification: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", to_code(self.code))
        object.__setattr__(self, "name", "" if self.name is None else str(self.name).strip())
        object.__setattr__(self, "specification", "" if self.specification is None else str(self.specification).strip())
        object.__setattr__(self, "kind", to_symbol(self.kind, name="区分"))
        object.__setattr__(self, "lifecycle", to_symbol(self.lifecycle, name="状態"))
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
    """原価の1行。code は EBOM の品番、mbom_code は元の MBOM の品番。値の None は未登録（0 円ではない）。"""

    code: str
    values: Mapping[CostHead, Decimal | None]
    mbom_code: str | None = None
    source: Source = Source.IMPORTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", to_code(self.code))
        object.__setattr__(self, "mbom_code", to_optional_code(self.mbom_code, name="MBOM の品番"))
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
        """11桁の MBOM の品番から作る。品番は EBOM の品番に戻して持つ。"""
        return cls(code=mbom_to_ebom(mbom_code), values=values, mbom_code=to_code(mbom_code), source=source)

    def value(self, head: CostHead) -> Decimal | None:
        return self.values[head]


@dataclass(frozen=True)
class Volume:
    """製品の年間台数。種別は出どころで決まる（取り込み＝量産の実績・手入力＝新製品の計画）。"""

    product: str
    annual: Decimal
    source: Source = Source.IMPORTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "product", to_code(self.product, name="製品の品番"))
        object.__setattr__(self, "annual", to_decimal(self.annual, name="年間台数"))

    @property
    def kind(self) -> VolumeKind:
        return VolumeKind.PLAN if self.source is Source.MANUAL else VolumeKind.ACTUAL


@dataclass(frozen=True)
class BomConflict:
    """同じ「親品番＋風船番号＋子品番」で数量が食い違う重複。計算には最初に読んだ数量を使う。"""

    key: tuple[str, str | None, str]
    quantities: tuple[Decimal, ...]


@dataclass(frozen=True)
class ManualOverlap:
    """取り込みの版とマスタ登録の両方にあり、取り込みの版（BOM）を採った品番（構成は親品番）。"""

    table: str  # "item"・"bom_line"・"cost"・"volume"
    code: str


@dataclass(frozen=True)
class ImportWarning:
    file: str
    row: int | None  # ファイルの行番号（見出しが1行目）
    message: str


@dataclass
class Version:
    """取り込み1回分の品目・構成・原価・年間台数。"""

    version_id: str
    imported_at: datetime | None = None
    note: str = ""
    source_name: str = ""
    items: dict[str, Item] = field(default_factory=dict)
    _lines: dict[tuple[str, str | None, str], BomLine] = field(default_factory=dict, repr=False)
    _conflicts: dict[tuple[str, str | None, str], list[Decimal]] = field(default_factory=dict, repr=False)
    _costs: dict[str, CostRow] = field(default_factory=dict, repr=False)
    volumes: dict[str, Volume] = field(default_factory=dict)
    manual_overlaps: tuple[ManualOverlap, ...] = ()
    warnings: tuple[ImportWarning, ...] = ()  # 取り込みの検証の結果（保存と読み戻しで残す）
    dev_number: str = ""  # 開発番号（空なら保存のときに版 ID を入れる）
    dev_name: str = ""  # 開発番号の名称
    revision: int = 1  # 同じ開発番号の中の第何版か
    _children_index: tuple[tuple[int, int], dict[str, tuple[BomLine, ...]]] | None = field(
        default=None, init=False, repr=False, compare=False)

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

    def add_conflict(self, key: tuple[str, str | None, str], quantities: Iterable[Decimal]) -> None:
        """保存から読み戻すときに、食い違った数量をそのまま戻す。"""
        self._conflicts[key] = list(quantities)

    def add_cost(self, row: CostRow) -> None:
        self._costs[row.code] = row

    def add_costs(self, rows: Iterable[CostRow]) -> None:
        for row in rows:
            self.add_cost(row)

    def add_volume(self, volume: Volume) -> None:
        self.volumes[volume.product] = volume

    @property
    def lines(self) -> tuple[BomLine, ...]:
        return tuple(self._lines.values())

    @property
    def cost_rows(self) -> tuple[CostRow, ...]:
        return tuple(self._costs.values())

    @property
    def conflicts(self) -> tuple[BomConflict, ...]:
        return tuple(BomConflict(key, tuple(qs)) for key, qs in self._conflicts.items())

    def conflicts_under(self, parents: Iterable[Code]) -> tuple[BomConflict, ...]:
        wanted = {to_code(p) for p in parents}
        return tuple(c for c in self.conflicts if c.key[0] in wanted)

    def children(self, parent: Code, by_balloon: bool = False) -> tuple[BomLine, ...]:
        """子の行。by_balloon=False なら同じ子品番の数量を合算し、風船番号は None にする。"""
        parent = to_code(parent)
        own = self._by_parent().get(parent, ())
        if by_balloon:
            return tuple(own)
        merged: dict[str, Decimal] = {}
        for line in own:
            merged[line.child] = merged.get(line.child, Decimal(0)) + line.quantity
        return tuple(BomLine(parent, None, child, qty) for child, qty in merged.items())

    def _by_parent(self) -> dict[str, tuple[BomLine, ...]]:
        # 全行を毎回なめると足し上げ・使用数のたどりが「行の数 × 親の数」になる。行は足すだけ（overlay_manual は
        # _lines を直接足す）なので、辞書と行の数が変わったら作り直す
        key = (id(self._lines), len(self._lines))
        if self._children_index is None or self._children_index[0] != key:
            grouped: dict[str, list[BomLine]] = {}
            for line in self._lines.values():
                grouped.setdefault(line.parent, []).append(line)
            self._children_index = (key, {p: tuple(lines) for p, lines in grouped.items()})
        return self._children_index[1]

    def parents(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(line.parent for line in self._lines.values()))

    def cost_row(self, code: Code) -> CostRow | None:
        return self._costs.get(to_code(code))

    def cost_value(self, code: Code, head: CostHead) -> Decimal | None:
        row = self.cost_row(code)
        return None if row is None else row.value(head)

    def volume(self, product: Code) -> Volume | None:
        return self.volumes.get(to_code(product))


def overlay_manual(base: Version, *layers: Version) -> Version:
    """取り込みの版の下にマスタ登録（layers・強い順）を敷いた版を作る。元の版は変えない。

    同じ品番は左を採る: 取り込みの版 ＞ layers[0] ＞ layers[1] …（品目・原価・年間台数のそれぞれ）。
    構成は親品番の単位で採る（取り込みの版に子の行がある親は、登録の子の行を使わない）。
    取り込みの版に隠れた登録は manual_overlaps に残す。登録の行の出どころは手入力になる。
    """
    result = Version(base.version_id, base.imported_at, base.note, base.source_name,
                     dev_number=base.dev_number, dev_name=base.dev_name, revision=base.revision)
    overlaps: dict[ManualOverlap, None] = {}
    base_parents = set(base.parents())

    result.items = dict(base.items)
    result._lines = dict(base._lines)
    result._conflicts = {k: list(v) for k, v in base._conflicts.items()}
    result._costs = dict(base._costs)
    result.volumes = dict(base.volumes)
    taken_parents = set(base_parents)
    for layer in layers:
        for code, item in layer.items.items():
            if code in base.items:
                overlaps[ManualOverlap("item", code)] = None
            result.items.setdefault(code, replace(item, source=Source.MANUAL))
        layer_parents = set(layer.parents())
        for parent in layer_parents & base_parents:
            overlaps[ManualOverlap("bom_line", parent)] = None
        new_parents = layer_parents - taken_parents
        result._lines.update({k: v for k, v in layer._lines.items() if k[0] in new_parents})
        result._conflicts.update({k: list(v) for k, v in layer._conflicts.items() if k[0] in new_parents})
        taken_parents |= new_parents
        for code, row in layer._costs.items():
            taken = result._costs.get(code)
            if taken is None:
                result._costs[code] = replace(row, source=Source.MANUAL)
                continue
            # 費目ごとに、強い側が空欄の所だけ登録で埋める（BOM で未登録の製造原価などを登録で補える）
            if code in base._costs and any(base._costs[code].values[h] is not None and v is not None
                                           for h, v in row.values.items()):
                overlaps[ManualOverlap("cost", code)] = None
            merged = {h: v if v is not None else row.values[h] for h, v in taken.values.items()}
            result._costs[code] = replace(taken, values=merged)
        for product, volume in layer.volumes.items():
            if product in base.volumes:
                overlaps[ManualOverlap("volume", product)] = None
            result.volumes.setdefault(product, replace(volume, source=Source.MANUAL))

    result.manual_overlaps = tuple(overlaps)
    result.warnings = base.warnings
    return result
