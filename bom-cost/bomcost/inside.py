"""組立品の中の部品の置き換え（/v2/ の ⑪）。

組立品の中が変わると、組立品も別の品番になる。中の部品を替えると、その上の組立品を流用した仮品番を下から順に作り、
製品の直下の組立品を仮品番に置き換える。もう仮品番になっている組立品は、使っている所が全部同じ中身になる時だけ、その仮品番の中を直す。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from itertools import count
from typing import Any, Iterable, Mapping, NamedTuple, Sequence, Union

from .study import (
    SlotKey,
    Temp,
    Workspace,
    after_places,
    direct_lines,
    input_code,
    input_optional_code,
    inside_lines,
    refuse_unregistered,
    temp_cycle,
    temp_lines,
    TempLine,
)

Path = tuple[str, ...]  # 製品の直下の組立品から、部品の親までの品番
Top = tuple[str, str, str | None, str]  # （案, 製品, 風船番号, 品番）＝製品の直下の箇所


class Node(NamedTuple):
    """中が変わって、新しい品番が要る組立品。lines の品番は、その中でも新しい品番が要る物は Node。"""

    code: str
    lines: frozenset[tuple[tuple[str | None, "Ref"], Decimal]]


Ref = Union[str, Node]
Content = dict[tuple[str | None, Ref], Decimal]


class Edit(NamedTuple):
    """path の一番下の組立品の中の old を new に。balloons が None なら全部の箇所、top が None なら直下に path[0] がある全部の製品。"""

    path: Path
    old: str
    new: str
    balloons: tuple[str | None, ...] | None
    top: tuple[str, str | None] | None


class InsideLine(NamedTuple):
    balloon: str | None
    quantity: Decimal
    was: list[str]  # 仮品番の中で、流用元から置き換えた前の品番


class InsideRow(NamedTuple):
    path: Path
    part: str
    lines: list[InsideLine]


def temp_of(ws: Workspace, code: str) -> Temp | None:
    """流用した仮品番なら、その行。"""
    if code not in ws.temp_merged:
        return None
    return next((t for t in ws.temps if t.code == code and t.base is not None), None)


def _tops(ws: Workspace, plan: str) -> dict[str, list[tuple[str, str | None]]]:
    """直下の品番 → 使う箇所（製品, 風船番号）。"""
    tops: dict[str, list[tuple[str, str | None]]] = {}
    for p in after_places(ws, plan):
        if p.quantity != 0:
            tops.setdefault(p.part, []).append((p.product, p.balloon))
    return tops


def inside_rows(ws: Workspace, plan: str) -> tuple[list[InsideRow], dict[str, list[tuple[str, str | None]]]]:
    """対象品番の直下の組立品の中にある部品（全階層）と、直下の組立品 → 使う箇所（製品, 風船番号）。"""
    rows: list[InsideRow] = []
    below: dict[str, dict[str, list[InsideLine]]] = {}

    def lines(code: str) -> dict[str, list[InsideLine]]:
        if code not in below:
            swaps = _swaps(ws, code)
            parts: dict[str, list[InsideLine]] = {}
            for (balloon, part), qty in inside_lines(ws, code).items():
                parts.setdefault(part, []).append(InsideLine(balloon, qty, swaps.get((balloon, part), [])))
            below[code] = parts
        return below[code]

    def walk(path: Path) -> None:
        for part, found in lines(path[-1]).items():
            rows.append(InsideRow(path, part, found))
            if part not in path and lines(part):
                walk((*path, part))

    tops = _tops(ws, plan)
    tops = {code: places for code, places in tops.items() if lines(code)}
    for code in tops:
        walk((code,))
    return rows, tops


def _swaps(ws: Workspace, code: str) -> dict[SlotKey, list[str]]:
    """仮品番の中で増えた箇所 → 同じ風船番号で減った品番。"""
    temp = temp_of(ws, code)
    if temp is None:
        return {}
    now, after = temp_lines(ws.bom, temp)
    down: dict[str | None, list[str]] = {}
    for key, qty in now.items():
        if after[key] < qty:
            down.setdefault(key[0], []).append(key[1])
    return {k: sorted(down[k[0]]) for k, q in after.items() if q > now.get(k, Decimal(0)) and k[0] in down}


def input_edits(rows: Sequence[Mapping[str, Any]]) -> list[Edit]:
    """送られた行（path・old・new、任意で balloon、任意で product と top）。"""
    edits: list[Edit] = []
    for r in rows:
        path = r.get("path")
        if not isinstance(path, list) or not path:
            raise ValueError("送る形が違います（path は配列）")
        old, new = input_code(r.get("old"), name="元品番"), input_code(r.get("new"), name="新品番")
        edit = Edit(tuple(input_code(c) for c in path), old, new,
                    (input_optional_code(r["balloon"], name="風船"),) if "balloon" in r else None,
                    (input_code(r["product"], name="製品"), input_optional_code(r.get("top"), name="風船")) if "product" in r else None)
        if any(e[:2] == edit[:2] and e[3:] == edit[3:] for e in edits):
            raise ValueError(f"{path[-1]} の中の {old} が2行あります")
        if old != new:
            edits.append(edit)
    return edits


@dataclass
class InsideChange:
    """中の置き換えで保存する物。"""

    added: list[Temp] = field(default_factory=list)
    lines: dict[str, tuple[TempLine, ...]] = field(default_factory=dict)  # 中を直す仮品番
    picks: list[tuple[str, str | None, str, str]] = field(default_factory=list)  # 直下の（製品, 風船番号, 元, 新）


class Rebuild:
    """edits を当てた後の、組立品の中身。"""

    def __init__(self, ws: Workspace, plan: str, edits: Sequence[Edit]) -> None:
        self.ws = ws
        self.plan = plan
        self.edits = list(edits)
        self._lines: dict[str, dict[SlotKey, Decimal]] = {}
        self._contents: dict[tuple[tuple[int, ...], Path], Content] = {}
        self._in_place: dict[str, bool] = {}
        refuse_unregistered(ws.bom, [e.new for e in self.edits])
        self.tops: list[Top] = [(p.plan, *place, code) for p in ws.plans
                                for code, places in _tops(ws, p.plan).items() for place in places]
        # 直下の箇所 → 当たる edits の番号
        self.at: dict[Top, tuple[int, ...]] = {
            top: tuple(i for i, e in enumerate(self.edits)
                       if top[0] == plan and e.path[0] == top[3] and e.top in (None, top[1:3])) for top in self.tops}
        used = {i for ids in self.at.values() for i in ids}
        for i, e in enumerate(self.edits):
            if i not in used:
                raise ValueError(f"{e.path[0]} は対象品番の直下にありません（画面を開き直してください）")
        self.occurrences: dict[str, list[tuple[Top, Path]]] = {}
        temps = {t.code for t in ws.temps if t.base is not None and t.code in ws.temp_merged}
        if temps:
            paths: dict[str, list[tuple[str, Path]]] = {}  # 直下の品番 → 下にある（仮品番, そこまでの path）
            for top in self.tops:
                if top[3] not in paths:
                    paths[top[3]] = [(p[-1], p) for p in self._paths((top[3],)) if p[-1] in temps]
                for temp, path in paths[top[3]]:
                    self.occurrences.setdefault(temp, []).append((top, path))
        self.changed = [top for top in self.tops if self.at[top]]
        self.nodes: list[Node] = []  # 新しい品番が要る組立品（中の物から順）
        self.fixed: dict[str, Content] = {}  # 中を直す仮品番
        for top in self.changed:
            self._gather(top, (top[3],))

    def _paths(self, path: Path) -> Iterable[Path]:
        if not self.lines(path[-1]):
            return
        yield path
        for part in dict.fromkeys(k[1] for k in self.lines(path[-1])):
            if part not in path:
                yield from self._paths((*path, part))

    def lines(self, code: str) -> dict[SlotKey, Decimal]:
        if code not in self._lines:
            self._lines[code] = inside_lines(self.ws, code)
        return self._lines[code]

    def content(self, top: Top, path: Path) -> Content:
        key = (self.at[top], path)
        if key in self._contents:
            return self._contents[key]
        now = self.lines(path[-1])
        after: Content = dict(now)
        edits = [self.edits[i] for i in self.at[top]]

        moved: set[SlotKey] = set()

        def move(slot: SlotKey, new: Ref) -> None:
            if slot in moved:
                raise ValueError(f"{path[-1]} の中の {slot[1]} が2行あります")
            moved.add(slot)
            after[slot] = after.get(slot, Decimal(0)) - now[slot]
            after[(slot[0], new)] = after.get((slot[0], new), Decimal(0)) + now[slot]

        below = {e.path[len(path)] for e in edits if e.path[:len(path)] == path and len(e.path) > len(path)}
        for e in edits:
            if e.path != path:
                continue
            if e.old in below:
                raise ValueError(f"{e.old} を置き換える行と、{e.old} の中を変える行は、一緒に入れられません")
            slots = [k for k in now if k[1] == e.old and (e.balloons is None or k[0] in e.balloons)]
            if not slots:
                raise ValueError(f"{e.old} は {path[-1]} の中にありません（画面を開き直してください）")
            for slot in slots:
                move(slot, e.new)
        for child in sorted(below):
            slots = [k for k in now if k[1] == child]
            if not slots:
                raise ValueError(f"{child} は {path[-1]} の中にありません（画面を開き直してください）")
            ref = self.ref(top, (*path, child))
            for slot in slots if ref != child else ():
                move(slot, ref)
        self._contents[key] = {k: q for k, q in after.items() if q != 0}
        return self._contents[key]

    def ref(self, top: Top, path: Path) -> Ref:
        """その箇所の組立品の、変えた後の品番。流用元と同じ中身に戻れば流用元・新しい品番が要れば Node。"""
        after = self.content(top, path)
        base = self.base(path[-1])
        if base != path[-1] and self.at[top] and after == self.lines(base):
            return base
        if after == self.lines(path[-1]) or self.in_place(path[-1]):
            return path[-1]
        return Node(path[-1], frozenset(after.items()))

    def in_place(self, code: str) -> bool:
        """流用した仮品番で、使っている所が（ほかの案も含めて）全部同じ中身になるか。"""
        if code not in self._in_place:
            places = self.occurrences.get(code, [])
            self._in_place[code] = bool(places) and len({frozenset(self.content(*p).items()) for p in places}) == 1
        return self._in_place[code]

    def _gather(self, top: Top, path: Path) -> None:
        edits = [self.edits[i] for i in self.at[top]]
        for child in dict.fromkeys(e.path[len(path)] for e in edits if e.path[:len(path)] == path and len(e.path) > len(path)):
            self._gather(top, (*path, child))
        ref = self.ref(top, path)
        if isinstance(ref, Node):
            if ref not in self.nodes:
                self.nodes.append(ref)
        elif ref == path[-1] and self.content(top, path) != self.lines(ref):
            self.fixed[ref] = self.content(top, path)

    def base(self, code: str) -> str:
        temp = temp_of(self.ws, code)
        return code if temp is None or temp.base is None else temp.base

    def needs(self) -> list[dict[str, Any]]:
        """新しい品番が要る組立品: 品番・名称・変わる中身・替わる製品・入れておく品番の案。"""
        taken = {*self.ws.bom.items, *(t.code for t in self.ws.temps)}
        found = []
        given: dict[tuple[str, frozenset[Any]], str] = {}  # 流用元も中身も同じ物は、同じ品番
        for node in self.nodes:
            base = self.base(node.code)
            code = given.get((base, node.lines)) or self._same_temp(node)
            if code is None:
                code = next(c for c in (f"{base}-K{n}" for n in count(1)) if c not in taken)
                taken.add(code)
            given[(base, node.lines)] = code
            item = self.ws.bom.items.get(node.code)
            found.append({"code": node.code, "name": item.name if item else "", "suggested": code, "changes": self._changes(node),
                          "products": list(dict.fromkeys(top[1] for top in self.changed if self._holds(top, node)))})
        return found

    def _holds(self, top: Top, node: Node) -> bool:
        """変えた後の、その直下の箇所の中に node があるか。"""
        stack: list[Ref] = [self.ref(top, (top[3],))]
        while stack:
            at = stack.pop()
            if at == node:
                return True
            if isinstance(at, Node):
                stack += [k[1] for k, _ in at.lines]
            else:
                stack += [k[1] for k in self.fixed.get(at, {})]
        return False

    def _changes(self, node: Node) -> list[str]:
        now = self.lines(node.code)
        after = dict(node.lines)
        texts = []
        for (balloon, part), qty in now.items():
            if after.get((balloon, part), Decimal(0)) >= qty:
                continue
            new = [k[1] for k in after if k[0] == balloon and after[k] > now.get(k, Decimal(0))]  # type: ignore[call-overload]
            inner = [x for x in new if isinstance(x, Node) and x.code == part]
            texts.append(f"{part} の中" if inner else f"{part} → {'・'.join(x if isinstance(x, str) else x.code for x in new)}")
        return list(dict.fromkeys(texts))

    def _same_temp(self, node: Node) -> str | None:
        """流用元も中身も同じ仮品番がもうあれば、その品番。"""
        for t in self.ws.temps:
            if t.base == self.base(node.code) and t.code in self.ws.temp_merged and t.code != node.code \
                    and frozenset(self.lines(t.code).items()) == node.lines and t.code not in self.fixed:
                return t.code
        return None

    def change(self, codes: Sequence[Mapping[str, Any]]) -> InsideChange:
        """codes（needs と同じ並びの、組立品の品番 code・新しい品番 new・名称 name）で、保存する物を作る。"""
        if [c.get("code") for c in codes] != [n.code for n in self.nodes]:
            raise ValueError("ほかで変更があったので、もう一度やり直してください")
        out = InsideChange()
        new_codes: dict[Node, str] = {}
        made: dict[str, tuple[str, frozenset[TempLine]]] = {}  # 新しい品番 → （流用元, 中の変更）
        temps = {t.code: t for t in self.ws.temps}

        def plain(content: Iterable[tuple[tuple[str | None, Ref], Decimal]]) -> dict[SlotKey, Decimal]:
            result: dict[SlotKey, Decimal] = {}
            for (balloon, part), qty in content:
                key = (balloon, part if isinstance(part, str) else new_codes[part])
                result[key] = result.get(key, Decimal(0)) + qty
            return result

        def delta(base: str, after: dict[SlotKey, Decimal]) -> tuple[TempLine, ...]:
            now = direct_lines(self.ws.bom, base)
            lines = ((*k, after.get(k, Decimal(0)) - now.get(k, Decimal(0))) for k in dict.fromkeys([*now, *after]))
            return tuple(x for x in lines if x[2] != 0)

        for node, given in zip(self.nodes, codes):
            new = input_code(given.get("new"), name="新しい品番")
            new_codes[node] = new
            base = self.base(node.code)
            lines = delta(base, plain(node.lines))
            if new in made:
                if made[new] == (base, frozenset(lines)):
                    continue
                raise ValueError(f"新しい品番 {new} が、中身の違う2つの組立品に入っています")
            made[new] = (base, frozenset(lines))
            old = temps.get(new)
            if old is not None and new in self.ws.temp_merged and old.base == base and new not in self.fixed \
                    and set(old.lines) == set(lines):
                continue  # 同じ中身の仮品番を使う
            if old is not None:
                raise ValueError(f"{new} は、別の中身の仮品番で使っています")
            if new in self.ws.bom.items:
                raise ValueError(f"{new} は部品表にある品番です")
            temps[new] = Temp(new, str(given.get("name") or "").strip(), None, base, lines)
            out.added.append(temps[new])
        for code, content in self.fixed.items():
            out.lines[code] = delta(self.base(code), plain(content.items()))
            temps[code] = temps[code]._replace(lines=out.lines[code])
        for code in [t.code for t in out.added] + list(out.lines):
            temp_cycle(self.ws.bom, temps, code)
        for top in self.changed:
            ref = self.ref(top, (top[3],))
            if ref != top[3]:
                out.picks.append((top[1], top[2], top[3], ref if isinstance(ref, str) else new_codes[ref]))
        return out
