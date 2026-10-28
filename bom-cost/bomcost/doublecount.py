"""二重計上の判定。推定なので警告にとどめ、計算は止めない。"""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterable

from .model import BASE_SUFFIX, BASIC_HEADS, Code, CostHead, Version
from .normalize import NumberLike, to_code, to_decimal

# 「親の値 ≒ 子の足し上げ」とみなす幅（子の足し上げに対する割合）
DEFAULT_NEAR_TOLERANCE = Decimal("0.10")
# 親の値が子の足し上げのこの割合より小さければ「ずっと小さい」
DEFAULT_SMALL_RATIO = Decimal("0.5")


class Verdict(Enum):
    INCLUDES_CHILDREN_LIKELY = "下位込みらしい"
    OWN_ONLY_LIKELY = "自分だけらしい"
    MANUFACTURING_IS_SUM_LIKELY = "製造原価は合計らしい"
    UNDETERMINABLE = "判定できない"
    UNCLEAR = "どちらとも言えない"


@dataclass(frozen=True)
class Judgement:
    item: str
    head: CostHead
    item_value: Decimal | None
    compared_value: Decimal | None  # 子の足し上げ、または4費目の合計
    verdict: Verdict


def judge_double_counting(
    version: Version,
    items: Iterable[Code] | None = None,
    near_tolerance: NumberLike = DEFAULT_NEAR_TOLERANCE,
    small_ratio: NumberLike = DEFAULT_SMALL_RATIO,
    suffix: str = BASE_SUFFIX,
) -> tuple[Judgement, ...]:
    """親の品目ごと・4費目ごとに親の登録値と子の足し上げを比べ、製造原価は4費目の合計と比べる。

    items を省くと、版の構成で子を持つ品目（4費目）と製造原価が登録された全品目を見る。
    """
    near = to_decimal(near_tolerance, name="≒の幅")
    small = to_decimal(small_ratio, name="ずっと小さいの割合")
    if items is None:
        parents = list(version.parents())
        with_manufacturing = [
            code
            for code in _cost_codes(version, suffix)
            if version.cost_value(code, CostHead.MANUFACTURING, suffix) is not None
        ]
        targets = list(dict.fromkeys(parents + with_manufacturing))
    else:
        targets = [to_code(c) for c in items]

    results: list[Judgement] = []
    for code in targets:
        if version.children(code):
            for head in BASIC_HEADS:
                results.append(_judge_children(version, code, head, near, small, suffix))
        manufacturing = _judge_manufacturing(version, code, near, suffix)
        if manufacturing is not None:
            results.append(manufacturing)
    return tuple(results)


def tally(judgements: Iterable[Judgement]) -> dict[CostHead, Counter[Verdict]]:
    """費目ごとの判定の数。「材料費は多くの親で下位込みらしい」のような傾向を見るため。"""
    counts: dict[CostHead, Counter[Verdict]] = {}
    for j in judgements:
        counts.setdefault(j.head, Counter())[j.verdict] += 1
    return counts


def _cost_codes(version: Version, suffix: str) -> list[str]:
    codes = list(version.items)
    codes += [line.parent for line in version.lines] + [line.child for line in version.lines]
    return [c for c in dict.fromkeys(codes) if version.cost_row(c, suffix) is not None]


def _judge_children(
    version: Version, code: str, head: CostHead, near: Decimal, small: Decimal, suffix: str
) -> Judgement:
    parent_value = version.cost_value(code, head, suffix)
    children_sum = Decimal(0)
    for line in version.children(code):
        child_value = version.cost_value(line.child, head, suffix)
        if child_value is None:
            return Judgement(code, head, parent_value, None, Verdict.UNDETERMINABLE)
        children_sum += line.quantity * child_value
    if parent_value is None or children_sum <= 0:
        return Judgement(code, head, parent_value, children_sum, Verdict.UNDETERMINABLE)
    if abs(parent_value - children_sum) <= near * children_sum:
        verdict = Verdict.INCLUDES_CHILDREN_LIKELY
    elif parent_value < small * children_sum:
        verdict = Verdict.OWN_ONLY_LIKELY
    else:
        verdict = Verdict.UNCLEAR
    return Judgement(code, head, parent_value, children_sum, verdict)


def _judge_manufacturing(version: Version, code: str, near: Decimal, suffix: str) -> Judgement | None:
    manufacturing = version.cost_value(code, CostHead.MANUFACTURING, suffix)
    if manufacturing is None:
        return None
    values = [version.cost_value(code, head, suffix) for head in BASIC_HEADS]
    if any(v is None for v in values):
        return Judgement(code, CostHead.MANUFACTURING, manufacturing, None, Verdict.UNDETERMINABLE)
    basic_sum = sum(values, Decimal(0))
    if basic_sum > 0 and abs(manufacturing - basic_sum) <= near * basic_sum:
        verdict = Verdict.MANUFACTURING_IS_SUM_LIKELY
    else:
        verdict = Verdict.UNCLEAR
    return Judgement(code, CostHead.MANUFACTURING, manufacturing, basic_sum, verdict)
