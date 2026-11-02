"""製造原価の式。足し上げるのは材料費小計だけで、ほかは計算の一番上の品目の値から1回だけ出す。

調整費   ＝（材料費小計 ＋ 社内加工費 ＋ 償却費）× 調整費の率
材料費合計 ＝ 材料費小計 ＋ 社内加工費 ＋ 償却費 ＋ 調整費
間接費   ＝ 材料費合計 × 間接費の率
製造原価  ＝ 材料費合計 ＋ 間接費 ＋ 直接費

端数は丸めない。取り込んだ・登録した調整費・間接費・製造原価は使わない。
"""

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Mapping

from .model import Code, CostHead, Version

ADJUSTMENT_RATE = Decimal("0.03")
INDIRECT_RATE = Decimal("0.11")


class Head(Enum):
    """式で出す費目（③の並び）。6つの id は CostHead と同じ。"""

    MATERIAL = "材料費小計"
    PROCESSING = "社内加工費"
    AMORTIZATION = "償却費"
    ADJUSTMENT = "調整費"
    MATERIAL_TOTAL = "材料費合計"
    INDIRECT = "間接費"
    DIRECT = "直接費"
    MANUFACTURING = "製造原価"


# 検討案の費目変更で製品ごとに書き換えられる費目
EDITABLE_HEADS: tuple[Head, ...] = (Head.PROCESSING, Head.AMORTIZATION, Head.DIRECT)
_BELOW_MATERIAL = frozenset({Head.ADJUSTMENT, Head.MATERIAL_TOTAL, Head.INDIRECT, Head.MANUFACTURING})


@dataclass(frozen=True)
class OwnValues:
    """式の入力のうち、子から足し上げない品目自身の値。None は未登録。"""

    processing: Decimal = Decimal(0)
    amortization: Decimal | None = None
    direct: Decimal | None = None

    def with_value(self, head: Head, value: Decimal) -> "OwnValues":
        field = {Head.PROCESSING: "processing", Head.AMORTIZATION: "amortization", Head.DIRECT: "direct"}[head]
        return replace(self, **{field: value})

    def value(self, head: Head) -> Decimal | None:
        return {Head.PROCESSING: self.processing, Head.AMORTIZATION: self.amortization, Head.DIRECT: self.direct}[head]


def registered_own(version: Version, code: Code) -> OwnValues:
    """品目の登録値から。社内加工費は CSV に出ないので 0。"""
    return OwnValues(Decimal(0), version.cost_value(code, CostHead.AMORTIZATION),
                     version.cost_value(code, CostHead.DIRECT))


@dataclass(frozen=True)
class Breakdown:
    values: Mapping[Head, Decimal | None]
    incomplete: frozenset[Head]  # 未登録を除いた・0 とした値を使った費目

    def value(self, head: Head | CostHead) -> Decimal | None:
        return self.values[Head[head.name]]

    def is_incomplete(self, head: Head | CostHead) -> bool:
        return Head[head.name] in self.incomplete


def _tidy(value: Decimal) -> Decimal:
    """値を変えずに末尾の 0 を落とす（率を掛けると 1133.00 のように桁が増えるため）。"""
    value = value.normalize()
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value


def breakdown(material: Decimal | None, own: OwnValues, *, material_incomplete: bool = False,
              ignore_amortization: bool = False) -> Breakdown:
    """材料費小計と品目自身の値から8費目を出す。

    材料費小計が未登録なら、それに依る費目は全部未登録。償却費・直接費が未登録なら 0 として通し、一部未登録にする。
    ignore_amortization は償却費を 0 として扱い、未登録の印を付けない（償却費の欄は None）。
    """
    incomplete: set[Head] = set()
    if material_incomplete:
        incomplete |= {Head.MATERIAL, *_BELOW_MATERIAL}
    amortization = own.amortization
    if ignore_amortization:
        amortization = None
        amortization_used = Decimal(0)
    else:
        amortization_used = Decimal(0) if amortization is None else amortization
        if amortization is None:
            incomplete |= {Head.AMORTIZATION, *_BELOW_MATERIAL}
    direct_used = Decimal(0) if own.direct is None else own.direct
    if own.direct is None:
        incomplete |= {Head.DIRECT, Head.MANUFACTURING}
    values: dict[Head, Decimal | None] = {
        Head.MATERIAL: material, Head.PROCESSING: own.processing, Head.AMORTIZATION: amortization,
        Head.ADJUSTMENT: None, Head.MATERIAL_TOTAL: None, Head.INDIRECT: None, Head.DIRECT: own.direct,
        Head.MANUFACTURING: None,
    }
    if material is not None:
        base = material + own.processing + amortization_used
        adjustment = base * ADJUSTMENT_RATE
        material_total = base + adjustment
        indirect = material_total * INDIRECT_RATE
        values.update({
            Head.ADJUSTMENT: _tidy(adjustment),
            Head.MATERIAL_TOTAL: _tidy(material_total),
            Head.INDIRECT: _tidy(indirect),
            Head.MANUFACTURING: _tidy(material_total + indirect + direct_used),
        })
    return Breakdown(values, frozenset(incomplete))
