"""構成の箇所（/v2/ の ⑧構成変更・⑤構成一覧）: 製品の直下と、組立品の中を、現行と変えた後の組で並べる。"""

from decimal import Decimal
from typing import Collection, Mapping, NamedTuple, Sequence

from .study import ADD, SlotKey, Workspace, after_lines, check_changes, direct_lines, temp_lines


class Slot(NamedTuple):
    """1つの箇所。now は現行の品番（足した箇所は None）、part は変えた後の品番（消した箇所は現行のままで quantity が 0）。"""

    balloon: str | None
    now: str | None
    part: str
    before: Decimal | None
    quantity: Decimal


class Structure(NamedTuple):
    direct: dict[str, list[Slot]]  # 製品 → 直下の箇所
    inside: dict[str, list[Slot]]  # 組立品（変えた後の品番）→ 中の箇所
    bad: set[tuple[str, str | None, str]]  # 要確認の（製品, 風船番号, 品番）


def slots(now: Mapping[SlotKey, Decimal], after: Mapping[SlotKey, Decimal], apart: Collection[SlotKey] = ()) -> list[Slot]:
    """無くなった箇所と同じ風船番号に増えた品番があれば、置き換えとして1つの箇所にする。apart（足した部品）は組にしない。"""
    found: list[Slot] = []
    added = [k for k, q in after.items() if k not in now and q != 0]
    for (balloon, part), before in now.items():
        quantity = after.get((balloon, part), Decimal(0))
        swap = next((k for k in added if k[0] == balloon and k not in apart), None) if quantity == 0 and before != 0 else None
        if swap is not None:
            added.remove(swap)
            found.append(Slot(balloon, part, swap[1], before, after[swap]))
        elif before != 0 or quantity != 0:
            found.append(Slot(balloon, part, part, before, quantity))
    return found + [Slot(k[0], None, k[1], None, after[k]) for k in added]


def structure(ws: Workspace, plan: str, products: Sequence[str]) -> Structure:
    """products の直下と、その下にある組立品の中（全階層）。"""
    temps = {t.code: t for t in ws.temps if t.base is not None and t.code in ws.temp_merged}
    direct: dict[str, list[Slot]] = {}
    for product in products:
        now, after = after_lines(ws, plan, product)
        apart = {(c.balloon, c.part) for c in ws.plan_changes(plan) if c.product == product and c.parent == product and c.source == ADD}
        direct[product] = slots({k[1:]: q for k, q in now.items()}, {k[1:]: q for k, q in after.items()}, apart)
    inside: dict[str, list[Slot]] = {}
    stack = [s.part for found in direct.values() for s in found]
    seen: set[str] = set()
    while stack:
        code = stack.pop()
        if code in seen:
            continue
        seen.add(code)
        lines = direct_lines(ws.bom, code)
        found = slots(*temp_lines(ws.bom, temps[code])) if code in temps else slots(lines, lines)
        if found:
            inside[code] = found
            stack += [s.part for s in found]
    bad = {(c.change.product, c.change.balloon, c.change.part) for c in check_changes(ws, ws.plan_changes(plan))
           if c.problems and c.change.parent == c.change.product}
    return Structure(direct, inside, bad)
