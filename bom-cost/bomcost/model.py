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
    PROCESSING = "社内加工費"


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


# グループのマスタ登録（手入力の分）だけを読んだ Version に付ける ID
MANUAL_VERSION_ID = "manual"

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
    """BOM とマスタ登録の両方にあり、マスタ登録を採った品番（構成は親品番・原価は両方に値のある費目があるもの）。"""

    table: str  # "item"・"bom_line"・"cost"・"volume"
    code: str


@dataclass(frozen=True)
class ImportWarning:
    file: str
    row: int | None  # ファイルの行番号（見出しが1行目）
    message: str


@dataclass
class Version:
    """1つのグループの品目・構成・原価・年間台数（version_id はグループの ID）。BOM だけ・マスタ登録だけ・重ねた物のどれにも使う。"""

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
    warnings: tuple[ImportWarning, ...] = ()  # 最後の取り込みの検証の結果（保存と読み戻しで残す）
    dev_number: str = ""  # グループの開発番号
    dev_name: str = ""  # グループの名称
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

    def with_costs(self, rows: Iterable[CostRow]) -> "Version":
        """原価の行（同じ品番は差し替え）を足した写し。品目・構成は元の版と共有する（どちらも変えない前提）。"""
        result = replace(self, _costs={**self._costs, **{row.code: row for row in rows}})
        result._children_index = self._children_index
        return result


def overlay_manual(base: Version, manual: Version) -> Version:
    """グループの BOM（base）にマスタ登録（manual）を重ねた版を作る。元の版は変えない。

    同じ品番はマスタ登録を採る（品目・年間台数）。原価は費目ごとに、マスタ登録に値がある費目はそれ、空なら BOM の値。
    構成は親品番の単位で採る（マスタ登録に子の行がある親は、BOM の子の行を使わない）。
    マスタ登録に隠れた BOM の品番は manual_overlaps に残す。登録の行の出どころは手入力になる。
    """
    result = Version(base.version_id, base.imported_at, base.note, base.source_name,
                     dev_number=base.dev_number, dev_name=base.dev_name)
    overlaps: dict[ManualOverlap, None] = {}

    result.items = dict(base.items)
    for code, item in manual.items.items():
        if code in base.items:
            overlaps[ManualOverlap("item", code)] = None
        result.items[code] = replace(item, source=Source.MANUAL)
    manual_parents = set(manual.parents())
    for parent in manual_parents & set(base.parents()):
        overlaps[ManualOverlap("bom_line", parent)] = None
    result._lines = {k: v for k, v in base._lines.items() if k[0] not in manual_parents}
    result._lines.update(manual._lines)
    result._conflicts = {k: list(v) for k, v in base._conflicts.items() if k[0] not in manual_parents}
    result._conflicts.update({k: list(v) for k, v in manual._conflicts.items()})
    result._costs = dict(base._costs)
    for code, row in manual._costs.items():
        kept = base._costs.get(code)
        if kept is None:
            result._costs[code] = replace(row, source=Source.MANUAL)
            continue
        if any(kept.values[h] is not None and v is not None for h, v in row.values.items()):
            overlaps[ManualOverlap("cost", code)] = None
        merged = {h: v if v is not None else kept.values[h] for h, v in row.values.items()}
        used = any(v is not None for v in row.values.values())
        result._costs[code] = replace(kept, values=merged, source=Source.MANUAL if used else kept.source)
    result.volumes = dict(base.volumes)
    for product, volume in manual.volumes.items():
        if product in base.volumes:
            overlaps[ManualOverlap("volume", product)] = None
        result.volumes[product] = replace(volume, source=Source.MANUAL)

    result.manual_overlaps = tuple(overlaps)
    result.warnings = base.warnings
    return result
