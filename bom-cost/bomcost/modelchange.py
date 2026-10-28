"""モデルチェンジ: 元の製品と新しい製品の構成を並べ、対応を3段で決める。

1. 同じ品番は自動で対応させ、数量の差を出す
2. 品番が違う部品は部品の対応表（元の部品 → 新しい部品）で結ぶ
3. 片方にしか無い部品は、足した／抜いた部品として出す
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Mapping

from .model import Code, Version
from .mold import usage_per_unit
from .normalize import to_code
from .rollup import StructureOverrides


class MatchKind(Enum):
    SAME = "同じ品番"
    MAPPED = "対応表"
    ADDED = "足した"
    REMOVED = "抜いた"


@dataclass(frozen=True)
class StructureRow:
    kind: MatchKind
    old_part: str | None
    new_part: str | None
    old_quantity: Decimal
    new_quantity: Decimal

    @property
    def difference(self) -> Decimal:
        return self.new_quantity - self.old_quantity


def compare_structures(
    old_version: Version,
    old_product: Code,
    new_version: Version,
    new_product: Code,
    part_map: Mapping[Code, Code] | None = None,
    direct_only: bool = False,
    old_overrides: StructureOverrides | None = None,
    new_overrides: StructureOverrides | None = None,
) -> tuple[StructureRow, ...]:
    """2つの製品の構成を1台あたりの使用数で比べる。

    既定は子を持たない品目（末端）を、階層ごとに数量を掛けた使用数で比べる。
    direct_only=True なら直下の子だけを比べる（組立品どうしを対応表で結ぶとき）。
    """
    old = _quantities(old_version, to_code(old_product), direct_only, old_overrides)
    new = _quantities(new_version, to_code(new_product), direct_only, new_overrides)
    rows: list[StructureRow] = []
    used_old: set[str] = set()
    used_new: set[str] = set()

    for code in old:
        if code in new:
            rows.append(StructureRow(MatchKind.SAME, code, code, old[code], new[code]))
            used_old.add(code)
            used_new.add(code)
    for old_code, new_code in (part_map or {}).items():
        old_code, new_code = to_code(old_code), to_code(new_code)
        if old_code in old and new_code in new and old_code not in used_old and new_code not in used_new:
            rows.append(StructureRow(MatchKind.MAPPED, old_code, new_code, old[old_code], new[new_code]))
            used_old.add(old_code)
            used_new.add(new_code)
    for code, qty in old.items():
        if code not in used_old:
            rows.append(StructureRow(MatchKind.REMOVED, code, None, qty, Decimal(0)))
    for code, qty in new.items():
        if code not in used_new:
            rows.append(StructureRow(MatchKind.ADDED, None, code, Decimal(0), qty))
    return tuple(rows)


def _quantities(
    version: Version, product: str, direct_only: bool, overrides: StructureOverrides | None
) -> dict[str, Decimal]:
    over = {to_code(k): tuple(v) for k, v in (overrides or {}).items()}

    def children(code: str):
        return over[code] if code in over else version.children(code)

    if direct_only:
        result: dict[str, Decimal] = {}
        for line in children(product):
            result[line.child] = result.get(line.child, Decimal(0)) + line.quantity
    else:
        usage = usage_per_unit(version, product, overrides)
        result = {code: qty for code, qty in usage.items() if not children(code)}
    return {code: qty for code, qty in result.items() if qty != 0}
