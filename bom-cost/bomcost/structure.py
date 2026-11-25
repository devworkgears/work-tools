"""構成の箇所（⑧構成変更・⑤構成一覧）: 製品の直下と、組立品の中を、現行と変えた後の組で並べる。"""

from typing import NamedTuple, Sequence

from .study import Placed, Workspace, check_changes, direct_lines, place_slots, temp_placed


class Structure(NamedTuple):
    direct: dict[str, list[Placed]]  # 製品 → 直下の箇所（現行の品番が無くなった箇所も出す）
    inside: dict[str, list[Placed]]  # 組立品（変えた後の品番）→ 中の箇所
    bad: set[tuple[str, str | None, str | None, str]]  # 要確認の（製品, 風船番号, 現行の品番, 変えた後の品番）


def structure(ws: Workspace, plan: str, products: Sequence[str]) -> Structure:
    """products の直下と、その下にある組立品の中（全階層）。"""
    temps = {t.code: t for t in ws.temps if t.base is not None and t.code in ws.temp_merged}
    direct = {product: [p for p in ws.placed(plan, product) if p.lost or p.before or p.quantity] for product in products}
    inside: dict[str, list[Placed]] = {}
    stack = [p.part for found in direct.values() for p in found if not p.lost]
    seen: set[str] = set()
    while stack:
        code = stack.pop()
        if code in seen:
            continue
        seen.add(code)
        placed = temp_placed(ws.bom, temps[code]) if code in temps else place_slots(direct_lines(ws.bom, code), ())
        found = [p for p in placed if not p.lost and (p.before or p.quantity)]
        if found:
            inside[code] = found
            stack += [p.part for p in found]
    bad = {(c.change.product, c.change.slot.balloon, c.now, c.change.slot.part)
           for c in check_changes(ws, ws.plan_changes(plan)) if c.problems and c.change.parent == c.change.product}
    return Structure(direct, inside, bad)
