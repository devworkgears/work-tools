"""設計検討: 部品の差し替えを、対象の製品にまたがって案ごとに試算する。

検討が持つのは、変えた箇所（製品×風船番号×現行の品番）ごとの、変えた後の品番と数量だけで、部品表は計算のたびに今の物を読む。
部品変更（品番・数量・部品追加）と構成変更は同じ箇所の集まりに書き、後に書いた方が勝つ（足し算しない）。
変えられるのは製品の直下（1階層目）だけ。組立品の中を替える時は、組立品を流用した仮品番を作り、直下で置き換える。
版確定の時だけ、そのときの検討と部品表（対象にかかる分）を写して固める。
"""

import json
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Collection, Container, Iterable, Mapping, NamedTuple, Sequence

from . import settings
from .formula import COMPARE_HEADS, Breakdown, Head, OwnValues, breakdown, registered_own
from .model import BomLine, CostHead, CostRow, Item, Version, Volume
from .normalize import to_code, to_decimal, to_optional_code
from .rollup import CostCalculator

SCHEMA = """
CREATE TABLE IF NOT EXISTS v2_item (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    specification TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    product_spec TEXT NOT NULL DEFAULT '',
    drawing TEXT NOT NULL DEFAULT '',
    lifecycle TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS v2_line (
    parent TEXT NOT NULL,
    balloon TEXT,
    child TEXT NOT NULL,
    quantity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_cost (
    code TEXT PRIMARY KEY,
    material TEXT, direct TEXT, indirect TEXT, adjustment TEXT,
    amortization TEXT, manufacturing TEXT, processing TEXT
);
CREATE TABLE IF NOT EXISTS v2_volume (
    product TEXT PRIMARY KEY,
    annual TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v2_bom_settings (settings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v2_column_map (
    headers TEXT NOT NULL,
    kind TEXT NOT NULL,
    columns TEXT NOT NULL,
    PRIMARY KEY (headers, kind)
);
CREATE TABLE IF NOT EXISTS v2_study (
    study_id INTEGER PRIMARY KEY,
    number TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    approver TEXT NOT NULL DEFAULT '',
    dev_number TEXT NOT NULL DEFAULT '',
    main_product TEXT,
    updated_at TEXT NOT NULL,
    change_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v2_target (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    product TEXT NOT NULL,
    PRIMARY KEY (study_id, position)
);
CREATE TABLE IF NOT EXISTS v2_plan (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    plan TEXT NOT NULL,
    PRIMARY KEY (study_id, plan)
);
CREATE TABLE IF NOT EXISTS v2_slot (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    plan TEXT NOT NULL,
    product TEXT NOT NULL,
    parent TEXT NOT NULL,
    balloon TEXT NOT NULL,
    now TEXT NOT NULL,
    part TEXT NOT NULL,
    quantity TEXT NOT NULL,
    given TEXT,
    seq INTEGER NOT NULL,
    PRIMARY KEY (study_id, plan, product, parent, balloon, now, part)
);
CREATE TABLE IF NOT EXISTS v2_condition (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    product TEXT NOT NULL,
    annual TEXT NOT NULL,
    PRIMARY KEY (study_id, product)
);
CREATE TABLE IF NOT EXISTS v2_temp_item (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    material TEXT,
    base TEXT,
    specification TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (study_id, position)
);
CREATE TABLE IF NOT EXISTS v2_temp_slot (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    balloon TEXT NOT NULL,
    now TEXT NOT NULL,
    part TEXT NOT NULL,
    quantity TEXT NOT NULL,
    given TEXT,
    PRIMARY KEY (study_id, code, balloon, now, part)
);
CREATE TABLE IF NOT EXISTS v2_own (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    plan TEXT NOT NULL,
    product TEXT NOT NULL,
    head TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (study_id, plan, product, head)
);
CREATE TABLE IF NOT EXISTS v2_release (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    taken_at TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    PRIMARY KEY (study_id, version)
);
"""

COST_COLUMNS: tuple[tuple[str, CostHead], ...] = (
    ("material", CostHead.MATERIAL),
    ("direct", CostHead.DIRECT),
    ("indirect", CostHead.INDIRECT),
    ("adjustment", CostHead.ADJUSTMENT),
    ("amortization", CostHead.AMORTIZATION),
    ("manufacturing", CostHead.MANUFACTURING),
    ("processing", CostHead.PROCESSING),
)
BOM_TABLES = ("item", "bom_line", "cost", "volume")
# 品目に付く属性（部品マスタの任意の列）。名前・見出し
ATTRIBUTES: tuple[tuple[str, str], ...] = (("model", "系列"), ("product_spec", "タイプ"), ("drawing", "図面"))
Attributes = dict[str, dict[str, str]]  # 品番 → 属性の名前 → 値（無い列は空文字）

KEEP = object()  # 渡されなかった印（None と分ける）

NOT_TARGET = "対象品番に無い"
NO_PLACE = "箇所が無い"
OTHER_PART = "元の品番が違う"
MOVED = "元の数量が変わった"
NEGATIVE = "数量がマイナス"
UNREGISTERED = "未登録"
WAITING = "単価待ち"  # 変更の問題ではなく、⑦ と Excel の影響の印
DEEP = "2階層目の変更"
# 計算に入れない問題。未登録と元の数量が変わった物は入れる
EXCLUDING = frozenset({NOT_TARGET, NO_PLACE, OTHER_PART, NEGATIVE, DEEP})

REPLACED, DELETED, ADDED, RECOUNTED = "置き換え", "削除", "追加", "数量"
# 数量・台数の桁の上限（これより大きい・細かい値は打ち間違いとして受けない。計算の桁あふれも防ぐ）
MAX_ADJUSTED = 9
MIN_EXPONENT = -6


def nfkc(text: str) -> str:
    """全角の英数字・記号を半角にそろえる（マイナス記号の − も - にする）。"""
    return unicodedata.normalize("NFKC", text).replace("−", "-")


def input_code(value: Any, *, name: str = "品番") -> str:
    """画面から来た品番。全角は半角にそろえる。読めなければ画面向けの文の ValueError。"""
    try:
        return to_code(nfkc(value) if isinstance(value, str) else value, name=name)
    except (TypeError, ValueError):
        if value is None or str(value).strip() == "":
            raise ValueError(f"{name}を入力してください") from None
        raise ValueError(f"{name}の形が違います（{value}）") from None


def known_code(value: Any, known: Container[str], *, name: str = "品番") -> str:
    """画面から来た品番。部品表に全角のまま入っている品番は、そのまま指せるようにする。"""
    if isinstance(value, str) and value.strip() in known:
        return value.strip()
    return input_code(value, name=name)


def known_balloon(value: Any, known: Container[str | None]) -> str | None:
    if isinstance(value, str) and value.strip() in known and value.strip() != "":
        return value.strip()
    return input_optional_code(value, name="風船")


def input_optional_code(value: Any, *, name: str = "品番") -> str | None:
    try:
        return to_optional_code(nfkc(value) if isinstance(value, str) else value, name=name)
    except (TypeError, ValueError):
        raise ValueError(f"{name}の形が違います（{value}）") from None


def input_amount(value: Any, *, name: str = "数量") -> Decimal:
    """画面から来た数量・台数。全角は半角にそろえ、桁の大きすぎる・細かすぎる値は ValueError。"""
    try:
        amount = to_decimal(nfkc(value) if isinstance(value, str) else value, name=name)
    except (TypeError, ValueError):
        raise ValueError(f"{name}は数で入力してください（{value}）") from None
    if amount != 0:
        # normalize() などの演算は 1e400000000 で桁あふれするので、桁の並びだけで見る
        _, digits, exponent = amount.as_tuple()
        trailing = len(digits) - len("".join(map(str, digits)).rstrip("0"))
        if amount.adjusted() > MAX_ADJUSTED or exponent + trailing < MIN_EXPONENT:
            raise ValueError(f"{name}の桁が多すぎます（{value}）")
    return amount

# ⑦ の費目（式の並び）。画面と API はこの名前で指す
HEADS: dict[str, Head] = {
    "material": Head.MATERIAL, "processing": Head.PROCESSING, "amortization": Head.AMORTIZATION,
    "adjustment": Head.ADJUSTMENT, "material_total": Head.MATERIAL_TOTAL, "indirect": Head.INDIRECT,
    "direct": Head.DIRECT, "total": Head.MANUFACTURING,
}
# ⑩ で製品ごとに書き換える費目（製品自身の値。部品の置き換えでは変わらない）
OWN_HEADS: dict[str, Head] = {"processing": Head.PROCESSING, "amortization": Head.AMORTIZATION, "direct": Head.DIRECT}

SlotKey = tuple[str | None, str]  # （風船番号, 品番）


class Slot(NamedTuple):
    """変えた箇所1つ。now は現行の品番（足した箇所は None）、part と quantity は変えた後（消した箇所は part＝now・quantity 0）。

    given は打った時の現行の数量（足した箇所は None）。
    """

    balloon: str | None
    now: str | None
    part: str
    quantity: Decimal
    given: Decimal | None


class Placed(NamedTuple):
    """変えた後の構成の箇所1つ。now・before は現行の品番と数量（足した箇所は None）、slot は変えた箇所の記録（変えていなければ None）。

    lost は、変えた後に部品表から現行の品番が無くなった箇所（構成には入れない）。
    """

    balloon: str | None
    now: str | None
    part: str
    before: Decimal | None
    quantity: Decimal
    slot: Slot | None = None
    lost: bool = False


def place_slots(now: Mapping[SlotKey, Decimal], slots: Iterable[Slot]) -> list[Placed]:
    """現行の直下に、変えた箇所を当てる（現行の並び・無くなった箇所・足した箇所の順）。"""
    changed = {(s.balloon, s.now): s for s in slots if s.now is not None}
    added = {(s.balloon, s.part): s for s in slots if s.now is None}
    out: list[Placed] = []
    for key, before in now.items():
        # 足した後に部品表にも入った箇所は、足した数量をその箇所の数量とみなす（2つ分にしない）
        slot = changed.pop(key, None) or added.pop(key, None)
        out.append(Placed(key[0], key[1], key[1], before, before) if slot is None
                   else Placed(key[0], key[1], slot.part, before, slot.quantity, slot))
    out += [Placed(s.balloon, s.now, s.part, None, s.quantity, s, True) for s in changed.values()]
    return out + [Placed(s.balloon, None, s.part, None, s.quantity, s) for s in added.values()]


def placed_lines(placed: Iterable[Placed]) -> dict[SlotKey, Decimal]:
    """変えた後の直下（風船番号と品番ごとの数量・0 は入れない）。"""
    out: dict[SlotKey, Decimal] = {}
    for p in placed:
        if not p.lost and p.quantity != 0:
            out[(p.balloon, p.part)] = out.get((p.balloon, p.part), Decimal(0)) + p.quantity
    return out


def settle_slots(now: Mapping[SlotKey, Decimal], slots: Sequence[Slot]) -> list[Slot]:
    """同じ品番の現行の箇所が変わっていない所に足した物は、その箇所の数量に足す。"""
    changed = {(s.balloon, s.now) for s in slots if s.now is not None}
    return [Slot(s.balloon, s.part, s.part, now[key] + s.quantity, now[key])
            if s.now is None and (key := (s.balloon, s.part)) in now and key not in changed else s for s in slots]


def pair_slots(now: Mapping[SlotKey, Decimal], deltas: Mapping[SlotKey, Decimal], apart: Collection[SlotKey] = ()) -> list[Slot]:
    """増減を箇所にする。同じ風船番号で 0 になった品番と増えた品番を置き換えの組にする（数の合う相手が先）。apart（足した部品）は組にしない。"""
    out: list[Slot] = []
    ups = {k: d for k, d in deltas.items() if d > 0}
    for key, d in deltas.items():
        if d >= 0:
            continue
        before = now.get(key, -d)
        if before + d != 0:
            out.append(Slot(key[0], key[1], key[1], before + d, before))
            continue
        same = [k for k in ups if k[0] == key[0] and k not in apart]
        up = next((k for k in same if ups[k] == -d), next(iter(same), None))
        out.append(Slot(key[0], key[1], key[1], Decimal(0), before) if up is None else Slot(key[0], key[1], up[1], ups.pop(up), before))
    for key, d in ups.items():
        out.append(Slot(key[0], key[1], key[1], now[key] + d, now[key]) if key in now and key not in apart
                   else Slot(key[0], None, key[1], now.get(key, Decimal(0)) + d, None))
    return out


ChangeKey = tuple[str, str, str, str | None, str | None, str | None]


@dataclass(frozen=True)
class Change:
    """案の、変えた箇所1つ。親は製品（前に固めた版には、2階層目の物もある）。"""

    plan: str
    product: str
    parent: str
    slot: Slot
    seq: int = 0

    @property
    def key(self) -> ChangeKey:
        s = self.slot
        return (self.plan, self.product, self.parent, s.balloon, s.now, s.part if s.now is None else None)


@dataclass(frozen=True)
class Checked:
    change: Change
    now: str | None  # 現行の品番（足した箇所は None。足した後に部品表にも入った箇所は、その品番）
    before: Decimal | None  # 現行の数量（足した箇所と、現行の品番が無くなった箇所は None）
    problems: tuple[str, ...]

    @property
    def counted(self) -> bool:
        return not EXCLUDING & set(self.problems)

    @property
    def kind(self) -> str:
        s = self.change.slot
        return kind_of(self.now, s.part, s.quantity)


def kind_of(now: str | None, part: str, quantity: Decimal) -> str:
    """した事: 置き換え・数量・追加・削除。"""
    return ADDED if now is None else DELETED if quantity == 0 else RECOUNTED if now == part else REPLACED


@dataclass
class PlanInfo:
    plan: str


class Temp(NamedTuple):
    """仮品番。部品の原価は製品の材料費にだけ入るので、値は材料費だけ（None は未登録か、流用元から計算）。

    base は流用元の組立品、lines は流用元の直下の変えた箇所。material が空なら、流用元の材料費に変更の差を足す。
    """

    code: str
    name: str
    material: Decimal | None = None
    base: str | None = None
    lines: tuple[Slot, ...] = ()
    specification: str = ""


@dataclass
class Workspace:
    """1つの検討の、仮（version=None）か固めた版の中身。bom は仮なら今の部品表・版なら写し。"""

    number: str
    version: int | None
    header: dict[str, Any]
    targets: list[str]
    main_product: str | None
    plans: list[PlanInfo]
    changes: list[Change]
    conditions: dict[str, Decimal]
    bom: Version
    attributes: Attributes = field(default_factory=dict)
    temps: list[Temp] = field(default_factory=list)
    # 案 → (製品, 費目の名前 OWN_HEADS) → 書き換えた値
    owns: dict[str, dict[tuple[str, str], Decimal]] = field(default_factory=dict)
    # temps のうち部品表に混ぜた品番（品目の台帳に同じ品番がある仮品番は、台帳の方を使うので混ぜない）
    temp_merged: set[str] = field(default_factory=set)
    # 版を取った時の率。None（仮と、率を持たない前の版）は設定の率を使う
    rates: settings.Rates | None = None
    # 2階層目の変更を計算から外すか（外す前に固めた版は、固めた時の計算のまま）
    level1: bool = True
    hold: bool = True  # 単価の無い品番への置き換えを、現行のまま計算する（False は前の版の写し）
    strict: bool = True  # 中の一部の単価が分からない組立品も、単価が無い物とみる（False は前の版の写し）

    def use_rates(self) -> settings.Rates:
        return self.rates or settings.rates()

    def attribute(self, code: str, name: str) -> str:
        return self.attributes.get(code, {}).get(name, "")

    def plan_ids(self) -> list[str]:
        return [p.plan for p in self.plans]

    def plan_changes(self, plan: str) -> list[Change]:
        return [c for c in self.changes if c.plan == plan]

    def placed(self, plan: str, product: str) -> list[Placed]:
        """製品の直下の、変えた後の箇所。"""
        return place_slots(direct_lines(self.bom, product),
                           [c.slot for c in self.changes if c.plan == plan and c.product == c.parent == product])


def merge_temps(bom: Version, temps: Sequence[Temp]) -> set[str]:
    """仮品番のうち bom の品目に無い物を、品目と材料費の行として足す。足した品番を返す。

    流用した仮品番で材料費を打っていない物は、流用元の材料費＋Σ（変えた箇所の差）。1つでも分からなければ未登録のまま。
    """
    fresh = {t.code: t for t in temps if t.code not in bom.items}
    derived = {c: t for c, t in fresh.items() if t.base is not None and t.material is None}
    for t in fresh.values():
        bom.add_item(Item(t.code, t.name, specification=t.specification))
        if t.code not in derived:
            bom.add_cost(CostRow(t.code, {CostHead.MATERIAL: t.material}))
    calc = CostCalculator(bom, CostHead.MATERIAL)
    values: dict[str, Decimal | None] = {}

    def unit(code: str) -> Decimal | None:
        if code in derived:
            if code not in values:
                values[code] = None  # 仮品番どうしが循環した時は未登録
                values[code] = derive(derived[code])
            return values[code]
        try:
            result = calc.calculate(code)
        except ValueError:
            return None
        return None if any(not bom.children(m) for m in result.missing) else result.total

    def derive(t: Temp) -> Decimal | None:
        assert t.base is not None
        total = unit(t.base) if t.base in bom.items else None
        if total is None or any(p.lost for p in temp_placed(bom, t)):
            return None
        now, after = temp_lines(bom, t)
        for key in dict.fromkeys([*now, *after]):
            change = after.get(key, Decimal(0)) - now.get(key, Decimal(0))
            if change == 0:
                continue
            price = unit(key[1]) if key[1] in bom.items else None
            if price is None:
                return None
            total += change * price
        return total

    for code in derived:
        unit(code)
    for code in derived:
        bom.add_cost(CostRow(code, {CostHead.MATERIAL: values[code]}))
    return set(fresh)


def direct_lines(bom: Version, code: str) -> dict[SlotKey, Decimal]:
    """直下の箇所ごとの数量。"""
    out: dict[SlotKey, Decimal] = {}
    for line in bom.children(code, by_balloon=True):
        key = (line.balloon, line.child)
        out[key] = out.get(key, Decimal(0)) + line.quantity
    return out


def temp_placed(bom: Version, temp: Temp) -> list[Placed]:
    """流用した仮品番の直下の、変えた後の箇所。"""
    return place_slots(direct_lines(bom, temp.base) if temp.base is not None else {}, temp.lines)


def temp_lines(bom: Version, temp: Temp) -> tuple[dict[SlotKey, Decimal], dict[SlotKey, Decimal]]:
    """流用した仮品番の直下を（流用元, 変えた後）で返す。"""
    return direct_lines(bom, temp.base) if temp.base is not None else {}, placed_lines(temp_placed(bom, temp))


def temp_cycle(bom: Version, temps: Mapping[str, Temp], code: str) -> None:
    """仮品番の中に自分が（別の仮品番を通しても）入ると、材料費が決まらないので断る。"""
    stack, seen = [s.part for s in temps[code].lines if s.quantity > 0], set()
    while stack:
        part = stack.pop()
        if part == code:
            raise ValueError(f"{code} の中に {code} 自身は入れられません")
        if part in temps and part not in seen:
            seen.add(part)
            stack += [k[1] for k in temp_lines(bom, temps[part])[1]]


def inside_lines(ws: Workspace, code: str) -> dict[SlotKey, Decimal]:
    """組立品の中（直下の箇所と数量）。流用した仮品番は、変えた後の中身。"""
    temp = next((t for t in ws.temps if t.code == code and code in ws.temp_merged and t.base is not None), None)
    lines = direct_lines(ws.bom, code) if temp is None else temp_lines(ws.bom, temp)[1]
    return {k: q for k, q in lines.items() if q != 0}


def refuse_unregistered(bom: Version, codes: Iterable[str]) -> None:
    """新しく打った品番に、部品表にも仮品番にも無い物があれば断る。"""
    lost = [c for c in dict.fromkeys(codes) if c not in bom.items]
    if lost:
        raise ValueError(f"{'・'.join(lost)} は部品表にも仮品番にもありません。新しい品番は、先に仮品番で取ってください")


def unregistered_parts(ws: Workspace) -> list[str]:
    """変更に出てくる品番のうち、品目の台帳にも仮品番にも無い物（変更に出た順）。"""
    return list(dict.fromkeys(c.slot.part for c in ws.changes if c.slot.part not in ws.bom.items))


def refuse_loop(ws: Workspace, product: str, part: str) -> None:
    """製品の中に、その製品を含む品番を入れると原価が決まらないので断る。"""
    stack, seen = [part], set()
    while stack:
        code = stack.pop()
        if code == product:
            raise ValueError(f"{part} を {product} の中には入れられません（{product} を含んでいます）" if part != product
                             else f"{product} を自分の中には入れられません")
        if code not in seen:
            seen.add(code)
            stack += [k[1] for k in inside_lines(ws, code)]


def next_plan_id(existing: Iterable[str]) -> str:
    taken = set(existing)
    for code in range(ord("A"), ord("Z") + 1):
        if chr(code) not in taken:
            return chr(code)
    raise ValueError("案は26個までです")


# 部品表をたどる -----------------------------------------------------------


def product_lines(bom: Version, product: str) -> list[BomLine]:
    """製品の下の全部の構成行（深さ優先・同じ親の子は1回だけ）。"""
    result: list[BomLine] = []
    seen: set[str] = set()

    def walk(code: str, path: frozenset[str]) -> None:
        if code in seen or code in path:
            return
        seen.add(code)
        for line in bom.children(code, by_balloon=True):
            result.append(line)
            walk(line.child, path | {code})

    walk(product, frozenset())
    return result


def product_usage(bom: Version, product: str) -> dict[str, Decimal]:
    """製品1台あたりの各品目の数（製品自身は 1）。循環は止める。"""
    usage: dict[str, Decimal] = {product: Decimal(1)}

    def walk(code: str, multiplier: Decimal, path: frozenset[str]) -> None:
        if code in path:
            return
        for line in bom.children(code, by_balloon=True):
            qty = multiplier * line.quantity
            usage[line.child] = usage.get(line.child, Decimal(0)) + qty
            walk(line.child, qty, path | {code})

    walk(product, Decimal(1), frozenset())
    return usage


def where_used(bom: Version, part: str, targets: Sequence[str]) -> tuple[list[str], list[str], int]:
    """部品を直下で使う製品を（範囲内, 範囲外, 組立品の中でだけ使う製品の数）で。製品は一番上の品目（どこの子でもない）か対象品番。"""
    part = to_code(part)
    parents_of: dict[str, set[str]] = {}
    for line in bom.lines:
        parents_of.setdefault(line.child, set()).add(line.parent)
    ancestors: set[str] = set()
    stack = [part]
    while stack:
        for p in parents_of.get(stack.pop(), ()):
            if p not in ancestors:
                ancestors.add(p)
                stack.append(p)
    target_set = set(targets)
    direct = parents_of.get(part, set())
    users = [t for t in targets if t in ancestors] + sorted(a for a in ancestors if a not in target_set and a not in parents_of)
    inside = [p for p in users if p in direct and p in target_set]
    outside = [p for p in users if p in direct and p not in target_set]
    return inside, outside, len(users) - len(inside) - len(outside)


# 確かめる・計算する ---------------------------------------------------------


def check_changes(ws: Workspace, changes: Iterable[Change] | None = None) -> list[Checked]:
    """変更を今の部品表と突き合わせる。どの画面から書いた変更も同じ決まりで見る。"""
    bom = ws.bom
    targets = set(ws.targets)
    usages: dict[str, dict[str, Decimal]] = {}
    found: dict[ChangeKey, Placed] = {}  # 箇所 → 今の部品表に当てた物
    result = []
    chosen = list(ws.changes if changes is None else changes)
    groups: dict[tuple[str, str, str], list[Change]] = {}
    for c in chosen:
        groups.setdefault((c.plan, c.product, c.parent), []).append(c)
    for (plan, product, parent), members in groups.items():
        for p in place_slots(direct_lines(bom, parent), [c.slot for c in members]):
            if p.slot is not None:
                found[Change(plan, product, parent, p.slot).key] = p
    for c in chosen:
        s, p = c.slot, found[c.key]
        problems: list[str] = []
        if c.product not in targets:
            problems.append(NOT_TARGET)
        if ws.level1 and c.parent != c.product:
            problems.append(DEEP)
        usage = usages.setdefault(c.product, product_usage(bom, c.product))
        if c.parent not in usage:
            problems.append(NO_PLACE)
        elif p.lost:
            problems.append(OTHER_PART if s.balloon is not None and any(k[0] == s.balloon for k in direct_lines(bom, c.parent)) else NO_PLACE)
        elif p.before != s.given:
            problems.append(MOVED)
        if s.quantity < 0 and s.quantity != p.before:
            problems.append(NEGATIVE)
        if any(code is not None and code not in bom.items for code in (s.now, s.part)):
            problems.append(UNREGISTERED)
        result.append(Checked(c, p.now, p.before, tuple(problems)))
    return result


class PartPlace(NamedTuple):
    """変えた後の構成で、部品がある製品の直下の箇所（同じ風船番号と品番は1つにまとめる）。was は置き換える前の品番。

    before は現行の数量（足した箇所は None）。quantity 0 は、消した箇所。
    """

    product: str
    balloon: str | None
    part: str
    quantity: Decimal
    was: list[str]
    before: Decimal | None
    nows: list[str | None]  # まとめた箇所ごとの、現行の品番


def after_places(ws: Workspace, plan: str, products: Sequence[str] | None = None) -> list[PartPlace]:
    """製品（渡さなければ対象品番）の直下の、変えた後の箇所（製品の順・その中は部品表の順のまま）。"""
    found = []
    for product in ws.targets if products is None else products:
        groups: dict[tuple[SlotKey, bool], list[Placed]] = {}
        for p in ws.placed(plan, product):
            if not p.lost:
                groups.setdefault(((p.balloon, p.part), p.quantity == 0), []).append(p)
        for ((balloon, part), _), members in groups.items():
            nows = list(dict.fromkeys(p.now for p in members if p.now is not None))
            befores = [p.before for p in members if p.before is not None]
            found.append(PartPlace(product, balloon, part, sum((p.quantity for p in members), Decimal(0)),
                                   [] if nows in ([], [part]) else sorted(nows), sum(befores, Decimal(0)) if befores else None,
                                   [p.now for p in members]))
    return found


def problem_count(ws: Workspace) -> int:
    return sum(1 for c in check_changes(ws) if c.problems)


def calculate(ws: Workspace, head: Head | CostHead = Head.MANUFACTURING) -> dict[str, Any]:
    """1つの費目の、対象品番ごとの今と案ごとの原価・年間の影響・台数の加重平均。"""
    head = Head[head.name]
    return calculate_heads(ws, (head,))[head]


def own_values(ws: Workspace, plan: str, product: str, own: OwnValues) -> OwnValues:
    """製品自身の値に、案で書き換えた費目を当てる。"""
    for key, head in OWN_HEADS.items():
        value = ws.owns.get(plan, {}).get((product, key))
        if value is not None:
            own = own.with_value(head, value)
    return own


def annual_of(ws: Workspace, product: str) -> Decimal | None:
    """製品の年間台数。検討で入れた値、無ければ部品表の値。"""
    given = ws.conditions.get(product)
    volume = ws.bom.volume(product)
    return given if given is not None else None if volume is None else volume.annual


class Price(NamedTuple):
    value: Decimal | None  # 材料費。循環して決まらない・1つも分からなければ None
    full: bool  # 中の部品の材料費が全部分かるか


class Prices:
    """品番ごとの材料費。"""

    def __init__(self, bom: Version) -> None:
        self._bom = bom
        self._calc = CostCalculator(bom, CostHead.MATERIAL)
        self._memo: dict[str, Price] = {}

    def of(self, code: str) -> Price:
        if code not in self._memo:
            try:
                result = self._calc.calculate(code)
            except ValueError:  # 構成が循環している
                self._memo[code] = Price(None, False)
            else:
                # 組立品の未登録は子から足し上げて出るので、分からないのは子の無い品目の未登録だけ
                full = result.total is not None and not any(not self._bom.children(m) for m in result.missing)
                self._memo[code] = Price(result.total, full)
        return self._memo[code]


class Effect(NamedTuple):
    value: Decimal | None  # 親1つあたりの材料費の増減。計算に入れない変更は None
    missing: bool  # 単価が分からず、差が足りない


def change_effect(ws: Workspace, c: Checked, prices: Prices) -> Effect:
    """変更1つの効き方。単価の分からない品番への置き換えは、分かるまで現行のまま（引くと、ただで付く値になる）。"""
    s = c.change.slot
    if not c.counted:
        return Effect(None, False)
    before = c.before or Decimal(0)
    if c.now == s.part:
        price = prices.of(s.part)
        return Effect(None, True) if price.value is None else Effect((s.quantity - before) * price.value, ws.strict and not price.full)
    new = prices.of(s.part) if s.quantity != 0 else Price(Decimal(0), True)
    if (not new.full if ws.strict else new.value is None) and (c.now is None or ws.hold):
        return Effect(None, True)
    old = Price(Decimal(0), True) if c.now is None else prices.of(c.now)
    return Effect(s.quantity * (new.value or Decimal(0)) - before * (old.value or Decimal(0)), new.value is None or old.value is None)


def material_effects(ws: Workspace, plan: str) -> dict[ChangeKey, Effect]:
    """案の変更1つずつの、製品1台あたりの材料費小計への効き方。"""
    prices = Prices(ws.bom)
    usages: dict[str, dict[str, Decimal]] = {}
    out: dict[ChangeKey, Effect] = {}
    for c in check_changes(ws, ws.plan_changes(plan)):
        ch = c.change
        effect = change_effect(ws, c, prices)
        if effect.value is not None:
            if prices.of(ch.product).value is None:  # 現行の値が出ない製品は、変えた後の値も出ない
                effect = Effect(None, effect.missing)
            else:
                usage = usages.setdefault(ch.product, product_usage(ws.bom, ch.product))
                effect = Effect(effect.value * usage.get(ch.parent, Decimal(0)), effect.missing)
        out[ch.key] = effect
    return out


def calculate_heads(ws: Workspace, heads: Sequence[Head] = COMPARE_HEADS) -> dict[Head, dict[str, Any]]:
    """費目ごとに、対象品番ごとの今と案ごとの原価・年間の影響・台数の加重平均。DB は読むだけ（渡した Workspace だけを見る）。

    単価の無い品番への置き換えは、単価が分かるまで現行のまま（不完全の印を付ける）。
    案の値＝（今の材料費小計＋部品の増減）と、製品自身の値（⑩で書き換えた費目はその値）から式で出す。
    base＝年間の影響に入れた製品の、今の値×年間台数の合計（現行比の元）。
    台数の加重平均＝年間の影響 ÷ 年間の影響に入れた製品の年間台数の合計（1台あたりの増減）。台数の合計が 0 なら None。
    """
    bom = ws.bom
    prices = Prices(bom)
    rates = ws.use_rates()
    all_checked = check_changes(ws)
    checked = [c for c in all_checked if c.counted]
    # 計算から外した変更がある製品・案は、値に不完全の印を付ける
    dropped = {(c.change.plan, c.change.product) for c in all_checked if not c.counted}
    plans = ws.plan_ids()
    results: dict[Head, dict[str, Any]] = {
        h: {"head": h, "plans": plans, "rows": [], "impact": {p: Decimal(0) for p in plans},
            "impact_incomplete": {p: False for p in plans}, "counted": {p: Decimal(0) for p in plans},
            "base": {p: Decimal(0) for p in plans}}
        for h in heads}

    def pick(b: Breakdown | None, head: Head) -> tuple[Decimal | None, bool]:
        if b is None:
            return None, True
        if head is Head.MATERIAL:
            return b.value(head), False
        return b.value(head), b.is_incomplete(head)

    for product in ws.targets:
        own = registered_own(bom, product)
        now_material, full = prices.of(product)
        now_missing = not full
        now_b = None if now_material is None else breakdown(now_material, own, rates=rates)
        annual = annual_of(ws, product)
        usage = product_usage(bom, product)
        after_b: dict[str, tuple[Breakdown | None, bool]] = {}
        for plan in plans:
            delta = Decimal(0)
            missing = False
            for c in checked:
                if c.change.plan != plan or c.change.product != product:
                    continue
                effect = change_effect(ws, c, prices)
                missing |= effect.missing
                if effect.value is not None:
                    delta += usage.get(c.change.parent, Decimal(0)) * effect.value
            b = None if now_material is None else breakdown(now_material + delta, own_values(ws, plan, product, own),
                                                               rates=rates)
            after_b[plan] = (b, missing)
        for head in heads:
            res = results[head]
            now, now_flag = pick(now_b, head)
            row: dict[str, Any] = {
                "product": product,
                "name": bom.items[product].name if product in bom.items else None,
                "annual": annual,
                "now": now,
                "now_incomplete": now_missing or now_flag,
                "plans": {},
                "incomplete": {},
            }
            for plan in plans:
                b, missing = after_b[plan]
                after, flag = pick(b, head)
                row["plans"][plan] = after
                row["incomplete"][plan] = now_missing or flag or missing or (plan, product) in dropped
                if after is None or now is None or annual is None:
                    res["impact_incomplete"][plan] = True
                else:
                    res["impact"][plan] += (after - now) * annual
                    res["counted"][plan] += annual
                    res["base"][plan] += now * annual
                    res["impact_incomplete"][plan] |= row["incomplete"][plan]
            res["rows"].append(row)
    for res in results.values():
        counted = res.pop("counted")
        res["weighted"] = {p: res["impact"][p] / counted[p] if counted[p] else None for p in plans}
    return results


def own_rows(ws: Workspace, plan: str, outside: bool = False) -> list[dict[str, Any]]:
    """⑩ の行: 対象品番ごとの名称と、書き換える費目の今の値（台帳）と書き換えた値。outside なら、書き換えたまま対象品番から外した製品も。"""
    rows = []
    others = [p for p in dict.fromkeys(p for p, _ in ws.owns.get(plan, {})) if p not in ws.targets] if outside else []
    for product in [*ws.targets, *others]:
        own = registered_own(ws.bom, product)
        values = {}
        for key, head in OWN_HEADS.items():
            now = own.value(head)
            # 加工費は台帳に無ければ 0 で計算する（未登録の印を付けない）ので、今の値は台帳の値のまま見せる
            if head is Head.PROCESSING:
                now = ws.bom.cost_value(product, CostHead.PROCESSING)
            values[key] = {"now": now, "after": ws.owns.get(plan, {}).get((product, key))}
        rows.append({"product": product, "name": ws.bom.items[product].name if product in ws.bom.items else None,
                     "heads": values})
    return rows


# 保存 -----------------------------------------------------------------------


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _balloon_py(text: str) -> str | None:
    return None if text == "" else text


def _slot_db(s: Slot) -> tuple[str, str, str, str, str | None]:
    return (s.balloon or "", s.now or "", s.part, str(s.quantity), _txt(s.given))


def _slot_py(balloon: str, now: str, part: str, quantity: str, given: str | None) -> Slot:
    return Slot(balloon or None, now or None, part, Decimal(quantity), _dec(given))


def _dec(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _txt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class _Places(dict[str, list[Placed]]):
    """製品 → 直下の箇所（聞かれた製品だけ数える）。"""

    def __init__(self, ws: Workspace, plan: str) -> None:
        super().__init__()
        self._ws, self._plan = ws, plan

    def __missing__(self, product: str) -> list[Placed]:
        self[product] = self._ws.placed(self._plan, product)
        return self[product]


class StudyStore:
    """設計検討の表（v2_*）。部品表もこの表に持つ。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        # 属性の列より前に作った表には列を足す
        present = {r[1] for r in conn.execute("PRAGMA table_info(v2_item)")}
        for name in (*(n for n, _ in ATTRIBUTES), "lifecycle"):
            if name not in present:
                conn.execute(f"ALTER TABLE v2_item ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        # 流用元の列を足す前に作った仮品番の表
        temp_columns = {r[1] for r in conn.execute("PRAGMA table_info(v2_temp_item)")}
        if "base" not in temp_columns:
            conn.execute("ALTER TABLE v2_temp_item ADD COLUMN base TEXT")
        # 仕様の列を足す前に作った仮品番の表
        if "specification" not in temp_columns:
            conn.execute("ALTER TABLE v2_temp_item ADD COLUMN specification TEXT NOT NULL DEFAULT ''")
        conn.commit()
        self._take_deltas()

    def _take_deltas(self) -> None:
        """増減で持っていた前の版の変更を、箇所に移す（前の表は名前を変えて残す）。

        現行の数量が要るので、部品表が空の間は移さずに、取り込んだ時に移す。
        """
        c = self._conn
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        old = [t for t in ("v2_change", "v2_temp_line") if t in tables]
        if not old:
            return
        bom = self.load_bom()
        if not bom.lines and any(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in old):
            return
        with c:
            if "v2_change" in old:
                rows = [(sid, plan, product, parent, _balloon_py(b), part, Decimal(d), source) for sid, plan, product, parent, b, part, d, source in c.execute(
                    "SELECT study_id, plan, product, parent, balloon, part, delta, source FROM v2_change ORDER BY study_id, seq")]
                for sid in dict.fromkeys(r[0] for r in rows):
                    for change in _changes_from_deltas(bom, [r[1:] for r in rows if r[0] == sid]):
                        self._insert(sid, change.plan, change.product, change.slot, change.parent)
            if "v2_temp_line" in old:
                bases = {(sid, code): base for sid, code, base in c.execute("SELECT study_id, code, base FROM v2_temp_item")}
                lines: dict[tuple[int, str], dict[SlotKey, Decimal]] = {}
                for sid, code, b, part, d in c.execute("SELECT study_id, code, balloon, part, delta FROM v2_temp_line ORDER BY rowid"):
                    lines.setdefault((sid, code), {})[(_balloon_py(b), part)] = Decimal(d)
                for (sid, code), deltas in lines.items():
                    base = bases.get((sid, code))
                    self._put_temp_lines(sid, code, pair_slots(direct_lines(bom, base) if base else {}, deltas))
            for table in old:
                name = next(n for n in (f"{table}_old{i or ''}" for i in range(99)) if n not in tables)
                c.execute(f"ALTER TABLE {table} RENAME TO {name}")

    # 部品表 -----------------------------------------------------------------

    def replace_bom(self, version: Version, tables: Iterable[str], attributes: Attributes | None = None) -> None:
        """tables（item・bom_line・cost・volume）の中身を version の物に入れ替える。attributes は品目の属性。"""
        tables = set(tables)
        attributes = attributes or {}
        c = self._conn
        with c:
            if "item" in tables:
                c.execute("DELETE FROM v2_item")
                c.executemany(
                    f"INSERT INTO v2_item (code, name, kind, specification, lifecycle, {', '.join(n for n, _ in ATTRIBUTES)}) "
                    f"VALUES ({', '.join('?' * (5 + len(ATTRIBUTES)))})",
                    [(i.code, i.name, i.kind, i.specification, i.lifecycle,
                      *(attributes.get(i.code, {}).get(n, "") for n, _ in ATTRIBUTES)) for i in version.items.values()])
            if "bom_line" in tables:
                c.execute("DELETE FROM v2_line")
                c.executemany("INSERT INTO v2_line VALUES (?, ?, ?, ?)",
                              [(l.parent, l.balloon, l.child, str(l.quantity)) for l in version.lines])
            if "cost" in tables:
                c.execute("DELETE FROM v2_cost")
                c.executemany(f"INSERT INTO v2_cost VALUES ({', '.join('?' * (1 + len(COST_COLUMNS)))})",
                              [(r.code, *(_txt(r.value(h)) for _, h in COST_COLUMNS)) for r in version.cost_rows])
            if "volume" in tables:
                c.execute("DELETE FROM v2_volume")
                c.executemany("INSERT INTO v2_volume VALUES (?, ?)",
                              [(v.product, str(v.annual)) for v in version.volumes.values()])
        self._take_deltas()

    def save_columns(self, headers: Sequence[str], kind: str, columns: Mapping[str, str]) -> None:
        """取り込みの列の対応（項目 → 見出し）を、見出しの並びと表の種類ごとに残す。"""
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO v2_column_map VALUES (?, ?, ?)",
                               (json.dumps(list(headers), ensure_ascii=False), kind, json.dumps(dict(columns), ensure_ascii=False)))

    def find_columns(self, headers: Sequence[str], kind: str) -> dict[str, str] | None:
        row = self._conn.execute("SELECT columns FROM v2_column_map WHERE headers = ? AND kind = ?",
                                 (json.dumps(list(headers), ensure_ascii=False), kind)).fetchone()
        return None if row is None else json.loads(row[0])

    def drop_old_bom(self) -> bool:
        """前に見た設定と今の設定が違えば、取り込んだ部品表を消す（前の設定で読んだ値が残らないように）。違えば True。

        空の項目は比べない（設定の項目が増えた版に替えただけでは消さない）。全部空の時（settings.json の置き忘れ）も消さない。
        """
        values = {k: v for k, v in settings.current().to_json().items() if v}
        now = json.dumps(values, ensure_ascii=False, sort_keys=True)
        row = self._conn.execute("SELECT settings FROM v2_bom_settings").fetchone()
        if row is not None and not values:
            return False
        if row is not None and row[0] == now:
            return False
        with self._conn:
            if row is not None:
                for table in ("v2_item", "v2_line", "v2_cost", "v2_volume", "v2_bom_settings", "v2_column_map"):
                    self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute("INSERT INTO v2_bom_settings VALUES (?)", (now,))
        return row is not None

    def load_bom(self) -> Version:
        v = Version("v2")
        q = self._conn.execute
        for code, name, kind, spec, life in q("SELECT code, name, kind, specification, lifecycle FROM v2_item ORDER BY rowid"):
            v.add_item(Item(code, name, kind, life, specification=spec))
        v.add_bom_lines(BomLine(p, b, ch, Decimal(qty))
                        for p, b, ch, qty in q("SELECT parent, balloon, child, quantity FROM v2_line ORDER BY rowid"))
        names = ", ".join(n for n, _ in COST_COLUMNS)
        for row in q(f"SELECT code, {names} FROM v2_cost ORDER BY rowid"):
            v.add_cost(CostRow(row[0], {h: _dec(t) for (_, h), t in zip(COST_COLUMNS, row[1:])}))
        for product, annual in q("SELECT product, annual FROM v2_volume ORDER BY rowid"):
            v.add_volume(Volume(product, Decimal(annual)))
        return v

    def load_attributes(self) -> Attributes:
        names = [n for n, _ in ATTRIBUTES]
        rows = self._conn.execute(f"SELECT code, {', '.join(names)} FROM v2_item ORDER BY rowid")
        return {row[0]: dict(zip(names, row[1:])) for row in rows}

    def bom_counts(self) -> dict[str, int]:
        q = self._conn.execute
        return {"item": q("SELECT COUNT(*) FROM v2_item").fetchone()[0],
                "bom_line": q("SELECT COUNT(*) FROM v2_line").fetchone()[0],
                "cost": q("SELECT COUNT(*) FROM v2_cost").fetchone()[0],
                "volume": q("SELECT COUNT(*) FROM v2_volume").fetchone()[0]}

    # 検討 -------------------------------------------------------------------

    def create_study(self, title: str = "", owner: str = "", approver: str = "", dev_number: str = "") -> str:
        """新しい検討を作り、検討番号（D-0001 の形）を返す。"""
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO v2_study (number, title, owner, approver, dev_number, updated_at) "
                "VALUES ('', ?, ?, ?, ?, ?)", (title, owner, approver, dev_number, _now()))
            number = f"D-{cur.lastrowid:04d}"
            self._conn.execute("UPDATE v2_study SET number = ? WHERE study_id = ?", (number, cur.lastrowid))
            self._conn.execute("INSERT INTO v2_plan (study_id, plan) VALUES (?, 'A')", (cur.lastrowid,))
        return number

    def list_studies(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT s.number, s.title, s.owner, s.updated_at, "
            "(SELECT MAX(version) FROM v2_release r WHERE r.study_id = s.study_id) "
            "FROM v2_study s ORDER BY s.study_id DESC").fetchall()
        return [{"number": n, "title": t, "owner": o, "updated_at": u, "latest_version": v}
                for n, t, o, u, v in rows]

    def study_id(self, number: str) -> int:
        row = self._conn.execute("SELECT study_id FROM v2_study WHERE number = ?", (number.strip(),)).fetchone()
        if row is None:
            raise KeyError(f"検討番号 {number} は登録されていません")
        return row[0]

    def versions(self, number: str) -> list[int]:
        sid = self.study_id(number)
        return [r[0] for r in self._conn.execute(
            "SELECT version FROM v2_release WHERE study_id = ? ORDER BY version", (sid,))]

    def update_header(self, number: str, title: str, owner: str, approver: str, dev_number: str) -> None:
        sid = self.study_id(number)
        with self._conn:
            self._conn.execute(
                "UPDATE v2_study SET title = ?, owner = ?, approver = ?, dev_number = ?, updated_at = ? "
                "WHERE study_id = ?", (title, owner, approver, dev_number, _now(), sid))

    def _touch(self, sid: int) -> None:
        self._conn.execute("UPDATE v2_study SET updated_at = ? WHERE study_id = ?", (_now(), sid))

    def workspace(self, number: str, version: int | None = None) -> Workspace:
        if version is not None:
            return self._release_workspace(number, version)
        sid = self.study_id(number)
        q = self._conn.execute
        n, title, owner, approver, dev_number, main, updated = q(
            "SELECT number, title, owner, approver, dev_number, main_product, updated_at FROM v2_study "
            "WHERE study_id = ?", (sid,)).fetchone()
        header = {"number": n, "title": title, "owner": owner, "approver": approver, "dev_number": dev_number,
                  "updated_at": updated}
        targets = [r[0] for r in q("SELECT product FROM v2_target WHERE study_id = ? ORDER BY position", (sid,))]
        plans = [PlanInfo(p) for (p,) in q(
            "SELECT plan FROM v2_plan WHERE study_id = ? ORDER BY plan", (sid,))]
        changes = [Change(plan, product, parent, _slot_py(*slot), seq) for plan, product, parent, *slot, seq
                   in q("SELECT plan, product, parent, balloon, now, part, quantity, given, seq FROM v2_slot "
                        "WHERE study_id = ? ORDER BY seq", (sid,))]
        conditions = {p: Decimal(a) for p, a in q("SELECT product, annual FROM v2_condition WHERE study_id = ?",
                                                  (sid,))}
        owns: dict[str, dict[tuple[str, str], Decimal]] = {}
        for plan, product, head, value in q("SELECT plan, product, head, value FROM v2_own WHERE study_id = ?", (sid,)):
            owns.setdefault(plan, {})[(product, head)] = Decimal(value)
        temps = self._temps(sid)
        bom = self.load_bom()
        merged = merge_temps(bom, temps)
        return Workspace(n, None, header, targets, main, plans or [PlanInfo("A")], changes, conditions,
                         bom, self.load_attributes(), temps=temps, temp_merged=merged, owns=owns)

    def _temps(self, sid: int) -> list[Temp]:
        q = self._conn.execute
        lines: dict[str, list[Slot]] = {}
        for code, *slot in q("SELECT code, balloon, now, part, quantity, given FROM v2_temp_slot WHERE study_id = ? ORDER BY rowid", (sid,)):
            lines.setdefault(code, []).append(_slot_py(*slot))
        return [Temp(c, nm, _dec(m), base, tuple(lines.get(c, ())), spec) for c, nm, m, base, spec in q(
            "SELECT code, name, material, base, specification FROM v2_temp_item WHERE study_id = ? ORDER BY position", (sid,))]

    def add_plan(self, number: str) -> str:
        """空の案を次の名前（A〜Z の空き）で足し、その名前を返す。"""
        sid = self.study_id(number)
        taken = [r[0] for r in self._conn.execute("SELECT plan FROM v2_plan WHERE study_id = ?", (sid,))]
        plan = next_plan_id(taken)
        with self._conn:
            self._ensure_plan(sid, plan)
            self._touch(sid)
        return plan

    def _ensure_plan(self, sid: int, plan: str) -> None:
        if not plan or len(plan) != 1 or not "A" <= plan <= "Z":
            raise ValueError(f"案の名前は A〜Z で指定してください（{plan}）")
        self._conn.execute("INSERT OR IGNORE INTO v2_plan (study_id, plan) VALUES (?, ?)", (sid, plan))

    def _next_seq(self, sid: int) -> int:
        self._conn.execute("UPDATE v2_study SET change_seq = change_seq + 1 WHERE study_id = ?", (sid,))
        return self._conn.execute("SELECT change_seq FROM v2_study WHERE study_id = ?", (sid,)).fetchone()[0]

    def _insert(self, sid: int, plan: str, product: str, slot: Slot, parent: str | None = None) -> None:
        self._conn.execute("INSERT OR REPLACE INTO v2_slot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (sid, plan, product, parent or product, *_slot_db(slot), self._next_seq(sid)))

    def _drop(self, sid: int, plan: str, product: str, slot: Slot | None) -> None:
        """変えた箇所の記録を消す（現行に戻る）。"""
        if slot is not None:
            self._conn.execute("DELETE FROM v2_slot WHERE study_id = ? AND plan = ? AND product = ? AND parent = ? AND balloon = ? AND now = ? "
                               "AND part = ?", (sid, plan, product, product, slot.balloon or "", slot.now or "", slot.part))

    def _set(self, sid: int, plan: str, product: str, placed: Placed, part: str, quantity: Decimal) -> None:
        """箇所の品番と数量を替える。現行と同じになれば記録は残さない。"""
        self._drop(sid, plan, product, placed.slot)
        if placed.now is None:
            if quantity != 0:
                self._insert(sid, plan, product, Slot(placed.balloon, None, part, quantity, None))
            return
        if quantity == 0:
            part = placed.now  # 消した箇所は、置き換えた品番を持たない
        if part != placed.now or quantity != placed.before:
            self._insert(sid, plan, product, Slot(placed.balloon, placed.now, part, quantity, placed.before))

    def _settle(self, sid: int, plan: str, before: Workspace, products: Iterable[str]) -> None:
        """今の操作で現行の箇所が元に戻り、足してあった同じ品番と重なった所は、1つの箇所に足す。"""
        def doubled(ws: Workspace, product: str) -> list[Placed]:
            return [p for p in ws.placed(plan, product) if p.slot is not None and p.slot.now is None and p.now is not None]

        after = self.workspace(before.number)
        for product in dict.fromkeys(products):
            known = {(p.balloon, p.now) for p in doubled(before, product)}
            for p in doubled(after, product):
                if (p.balloon, p.now) not in known:
                    self._set(sid, plan, product, p, p.part, p.quantity + (p.before or Decimal(0)))

    # 対象品番 ---------------------------------------------------------------

    def save_targets(self, number: str, products: Sequence[Any], main_product: Any = KEEP) -> None:
        """対象品番を入れ替える。main_product を渡さなければ、今の主対象を（対象品番に残れば）そのまま持つ。"""
        sid = self.study_id(number)
        items = self.load_bom().items
        codes = list(dict.fromkeys(known_code(p, items, name="対象品番") for p in products
                                   if p is not None and str(p).strip()))
        if main_product is KEEP:
            main_product = self._conn.execute("SELECT main_product FROM v2_study WHERE study_id = ?",
                                              (sid,)).fetchone()[0]
        main = main_product if main_product in codes else input_optional_code(main_product, name="主対象")
        if main is not None and main not in codes:
            main = None
        with self._conn:
            self._conn.execute("DELETE FROM v2_target WHERE study_id = ?", (sid,))
            self._conn.executemany("INSERT INTO v2_target VALUES (?, ?, ?)",
                                   [(sid, i, code) for i, code in enumerate(codes)])
            self._conn.execute("UPDATE v2_study SET main_product = ? WHERE study_id = ?", (main, sid))
            self._touch(sid)

    def save_main(self, number: str, main_product: Any) -> None:
        """⑤構成一覧の並びの基準にする主対象を1つ選ぶ（空なら外す）。対象品番の中からだけ選べる。"""
        sid = self.study_id(number)
        targets = [r[0] for r in self._conn.execute("SELECT product FROM v2_target WHERE study_id = ?", (sid,))]
        main = main_product if main_product in targets else input_optional_code(main_product, name="主対象")
        if main is not None and main not in targets:
            raise ValueError(f"主対象は対象品番から選んでください（{main}）")
        with self._conn:
            self._conn.execute("UPDATE v2_study SET main_product = ? WHERE study_id = ?", (main, sid))
            self._touch(sid)

    # 部品変更 -------------------------------------------------------------------

    def _row_product(self, ws: Workspace, row: Mapping[str, Any]) -> str:
        product = known_code(row.get("product"), ws.targets, name="製品")
        if product not in ws.targets:
            raise ValueError(f"{product} は対象品番にありません")
        return product

    @staticmethod
    def _row_slots(placed: Sequence[Placed], row: Mapping[str, Any], part: str) -> list[Placed]:
        """行が指す箇所。now（現行の品番）があれば balloon と now、足した箇所と now の無い行は balloon と変えた後の品番 part で指す。"""
        balloon = known_balloon(row.get("balloon"), {p.balloon for p in placed})
        now = row["now"] if "now" in row else KEEP
        return [p for p in placed if not p.lost and p.balloon == balloon
                and (p.part == part and (now is KEEP or p.now is None) if now in (KEEP, None) else p.now == now)]

    def apply_replace(self, number: str, plan: str, old: Any, new: Any) -> None:
        """変えた後の構成で、対象品番の全製品の直下の old を new に置き換える（1回きりの操作で、決まりとしては残さない）。"""
        ws = self.workspace(number)
        old_code, new_code = known_code(old, ws.bom.items, name="元品番"), known_code(new, ws.bom.items, name="新品番")
        if old_code == new_code:
            return
        rows = [{"product": product, "balloon": p.balloon, "now": p.now, "old": old_code, "new": new_code}
                for product in ws.targets for p in ws.placed(plan, product) if not p.lost and p.part == old_code and p.quantity != 0]
        if not rows:
            raise ValueError("対象品番の直下に、置き換える品番がありません")
        self.apply_places(number, plan, rows)

    def apply_places(self, number: str, plan: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """箇所（product・balloon・old、任意で now）ごとに old を new に置き換える。1行でも当たらなければ、どれも変えない。"""
        ws = self.workspace(number)
        work: list[tuple[str, Placed, str]] = []
        seen: set[tuple[str, str | None, str | None, str]] = set()
        placed_of = _Places(ws, plan)
        safe: set[tuple[str, str]] = set()
        for r in rows:
            product = self._row_product(ws, r)
            placed = placed_of[product]
            old = known_code(r.get("old"), {p.part for p in placed}, name="元品番")
            new = known_code(r.get("new"), ws.bom.items, name="新品番")
            found = [p for p in self._row_slots(placed, r, old) if p.quantity != 0]
            if not found:
                raise ValueError(f"{old} は {product} の直下にありません（画面を開き直してください）")
            for p in found:
                if (product, p.balloon, p.now, p.part) in seen:
                    raise ValueError(f"{product} の {old} が2行あります")
                seen.add((product, p.balloon, p.now, p.part))
                if p.part != new:
                    refuse_unregistered(ws.bom, [new] if new != p.now else [])
                    if (product, new) not in safe:
                        refuse_loop(ws, product, new)
                        safe.add((product, new))
                    work.append((product, p, new))
        self._apply(ws, plan, work)

    def _apply(self, ws: Workspace, plan: str, work: Sequence[tuple[str, Placed, str]]) -> None:
        """（製品, 箇所, 新品番）を置き換える。足した箇所が同じ風船番号と品番に重なったら、1つに足す。"""
        sid = self.study_id(ws.number)
        with self._conn:
            self._ensure_plan(sid, plan)
            added: dict[tuple[str, SlotKey], Decimal] = {}
            for product, p, new in work:
                if p.now is None:
                    self._drop(sid, plan, product, p.slot)
                    added[(product, (p.balloon, new))] = added.get((product, (p.balloon, new)), Decimal(0)) + p.quantity
                else:
                    self._set(sid, plan, product, p, new, p.quantity)
            for (product, key), quantity in added.items():
                self._add(sid, plan, self.workspace(ws.number), product, key, quantity)
            self._settle(sid, plan, ws, [product for product, _, _ in work])
            self._touch(sid)

    def _add(self, sid: int, plan: str, ws: Workspace, product: str, key: SlotKey, quantity: Decimal) -> None:
        """足した箇所にする。消した箇所・もう足した箇所と同じなら、その箇所の数量にする。"""
        same = [p for p in ws.placed(plan, product) if not p.lost and (p.balloon, p.part) == key]
        at = next((p for p in same if p.now is None), next((p for p in same if p.quantity == 0), None))
        self._set(sid, plan, product, at or Placed(key[0], None, key[1], None, Decimal(0)), key[1], quantity + (at.quantity if at else 0))

    def save_quantities(self, number: str, plan: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """箇所（product・balloon・part、任意で now）ごとに、変えた後の数量を入れる（0 は消す・空は現行の数量に戻し、足した箇所は消す）。

        1行でも当たらなければ、どれも変えない。
        """
        ws = self.workspace(number)
        sid = self.study_id(number)
        work: list[tuple[str, Placed, Decimal]] = []
        placed_of = _Places(ws, plan)
        for r in rows:
            product = self._row_product(ws, r)
            placed = placed_of[product]
            part = known_code(r.get("part"), {p.part for p in placed})
            found = self._row_slots(placed, r, part)
            if not found:
                raise ValueError(f"{part} は {product} の直下にありません（画面を開き直してください）")
            if len(found) > 1:
                raise ValueError(f"{product} の同じ風船番号に {part} が {len(found)} つあります。構成変更で直してください")
            value = r.get("quantity")
            amount = (found[0].before or Decimal(0)) if value is None or str(value).strip() == "" else input_amount(value)
            if amount < 0 and amount != found[0].before:
                raise ValueError(f"数量は0以上で入力してください（{value}）")
            work.append((product, found[0], amount))
        with self._conn:
            self._ensure_plan(sid, plan)
            for product, p, amount in work:
                self._set(sid, plan, product, p, p.part, amount)
            self._settle(sid, plan, ws, [product for product, _, _ in work])
            self._touch(sid)

    def back_places(self, number: str, plan: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """箇所（product・balloon・now・part）を現行の品番と数量に戻す（足した箇所と、現行の品番が無くなった箇所の変更は消える）。"""
        ws = self.workspace(number)
        sid = self.study_id(number)
        work: list[tuple[str, Slot]] = []
        for r in rows:
            product = self._row_product(ws, r)
            found = [p.slot for p in ws.placed(plan, product) if p.slot is not None and p.balloon == (r.get("balloon") or None)
                     and p.now == r.get("now") and p.part == r.get("part")]
            if not found:
                raise ValueError(f"{r.get('part')} は {product} の直下で変わっていません（画面を開き直してください）")
            work += [(product, s) for s in found if s is not None]
        with self._conn:
            for product, slot in work:
                self._drop(sid, plan, product, slot)
            self._settle(sid, plan, ws, [product for product, _ in work])
            self._touch(sid)

    def add_part(self, number: str, plan: str, part: Any, quantity: Any, products: Sequence[Any]) -> None:
        """部品を、製品たちの直下に足す（風船番号は無し）。もう直下にある製品が混ざれば、どれも変えない。"""
        ws = self.workspace(number)
        code = known_code(part, ws.bom.items)
        codes = list(dict.fromkeys(known_code(p, ws.targets, name="製品") for p in products))
        if not codes:
            raise ValueError("足す製品を選んでください")
        for product in codes:
            if product in ws.targets and any(p.part == code and p.quantity != 0 and not p.lost for p in ws.placed(plan, product)):
                raise ValueError(f"{code} は {product} にもうあります。数量は「製品別」で変えてください")
        self.add_places(number, plan, [{"product": product, "part": code, "quantity": quantity} for product in codes])

    def add_places(self, number: str, plan: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """部品を、製品の直下の箇所（product・balloon・part・quantity）に足す。同じ箇所にもうあれば、どれも変えない。"""
        ws = self.workspace(number)
        sid = self.study_id(number)
        work: dict[tuple[str, SlotKey], Decimal] = {}
        for r in rows:
            product = self._row_product(ws, r)
            placed = ws.placed(plan, product)
            part = known_code(r.get("part"), ws.bom.items)
            amount = input_amount(r.get("quantity"))
            refuse_loop(ws, product, part)
            if amount <= 0:
                raise ValueError(f"数量は0より大きい数で入力してください（{r.get('quantity')}）")
            key = (known_balloon(r.get("balloon"), {p.balloon for p in placed}), part)
            if (product, key) in work or key in placed_lines(placed):
                raise ValueError(f"{part} は {product} の同じ風船番号にもうあります")
            work[(product, key)] = amount
        refuse_unregistered(ws.bom, [key[1] for _, key in work])
        with self._conn:
            self._ensure_plan(sid, plan)
            for (product, key), amount in work.items():
                self._add(sid, plan, ws, product, key, amount)
            self._settle(sid, plan, ws, [product for product, _ in work])
            self._touch(sid)

    # ⑩ 費目変更 -----------------------------------------------------------

    def save_owns(self, number: str, plan: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """案の製品の費目の書き換え（product・head・value）を、対象品番の分だけ入れ替える。値が空の行は書き換えなし（台帳の値）。"""
        sid = self.study_id(number)
        values: dict[tuple[str, str], Decimal] = {}
        targets = {r[0] for r in self._conn.execute("SELECT product FROM v2_target WHERE study_id = ?", (sid,))}
        for r in rows:
            head = str(r.get("head") or "")
            if head not in OWN_HEADS:
                raise ValueError(f"書き換える費目の指定が違います（{head}）")
            value = r.get("value")
            if value is None or str(value).strip() == "":
                continue
            product = known_code(r.get("product"), targets, name="対象品番")
            values[(product, head)] = input_amount(value, name=OWN_HEADS[head].value)
        with self._conn:
            self._ensure_plan(sid, plan)
            # 対象品番から外している製品の分は、戻した時のために残す
            self._conn.execute("DELETE FROM v2_own WHERE study_id = ? AND plan = ? AND product IN "
                               "(SELECT product FROM v2_target WHERE study_id = ?)", (sid, plan, sid))
            self._conn.executemany("INSERT OR REPLACE INTO v2_own VALUES (?, ?, ?, ?, ?)",
                                   [(sid, plan, p, h, str(v)) for (p, h), v in values.items()])
            self._touch(sid)

    # ⑨ 仮品番 ---------------------------------------------------------------

    def save_temps(self, number: str, rows: Sequence[Mapping[str, Any]], known: Collection[str] | None = None) -> None:
        """仮品番の行（code・name・material・base）を入れ替える。品番の無い行は飛ばし、同じ品番が2行あれば ValueError。

        key は前に保存した時の品番（品番を打ち替えた行に、流用の中身を引き継ぎ、使っている所も替える）。流用元を替えた行の中身の変更は消す。
        known は画面が知っている仮品番（渡せば、知らない仮品番＝ほかの窓でできた物は消さずに後ろへ残す）。
        """
        sid = self.study_id(number)
        bom = self.load_bom()
        kept = {t.code: t for t in self._temps(sid)}
        temps: list[Temp] = []
        seen: dict[str, int] = {}
        for i, r in enumerate(rows, start=1):
            if not str(r.get("code") or "").strip():
                continue
            code = input_code(r.get("code"))
            if code in seen:
                raise ValueError(f"品番 {code} が重複しています（{seen[code]}行目・{i}行目）")
            seen[code] = i
            value = r.get("material")
            material = None if value is None or str(value).strip() == "" else input_amount(value, name="材料費")
            if material is not None and material < 0:
                raise ValueError(f"材料費は0以上で入力してください（{code}）")
            old = kept.get(str(r.get("key") or code))
            base = (None if r["base"] is None or str(r["base"]).strip() == "" else known_code(r["base"], bom.items, name="流用元")) if "base" in r else old.base if old else None
            same = old is not None and old.base == base
            if base is not None and not same:
                if base == code:
                    raise ValueError(f"流用元が仮品番と同じです（{code}）")
                if not bom.children(base):
                    raise ValueError(f"流用元は、部品表にある組立品を入れてください（{base}）")
            temps.append(Temp(code, str(r.get("name") or "").strip(), material, base, old.lines if same and old else (),
                              str(r.get("specification") or "").strip() if "specification" in r else old.specification if old else ""))
        if known is not None:
            shown = {*known, *(str(r.get("key") or "") for r in rows)}
            hidden = [t for t in kept.values() if t.code not in shown]
            for t in hidden:
                if t.code in seen:
                    raise ValueError(f"品番 {t.code} は、ほかの窓で仮品番になりました（画面を開き直してください）")
            temps += hidden
        renamed = {str(r["key"]): input_code(r["code"]) for r in rows
                   if str(r.get("code") or "").strip() and r.get("key") and str(r["key"]) in kept}
        renamed = {old: new for old, new in renamed.items() if old != new}
        if renamed:
            temps = [t._replace(lines=tuple(s._replace(part=renamed.get(s.part, s.part)) for s in t.lines)) for t in temps]
        # 使っている仮品番（変更に出てくる・ほかの仮品番の中にある）は消さない。中身は仮品番の側にしか無く、消すと戻せないため
        gone = set(kept) - {t.code for t in temps} - set(renamed)
        if gone:
            used = {c for row in self._conn.execute("SELECT parent, part FROM v2_slot WHERE study_id = ?", (sid,)) for c in row}
            used |= {s.part for t in temps for s in t.lines}
            busy = sorted(gone & used)
            if busy:
                raise ValueError(f"仮品番 {'・'.join(busy)} は構成変更で使っているので消せません（先に構成変更で元に戻してください）")
        with self._conn:
            self._rename_changes(sid, renamed)
            self._conn.execute("DELETE FROM v2_temp_item WHERE study_id = ?", (sid,))
            self._conn.execute("DELETE FROM v2_temp_slot WHERE study_id = ?", (sid,))
            self._conn.executemany(
                "INSERT INTO v2_temp_item (study_id, position, code, name, material, base, specification) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(sid, i, t.code, t.name, _txt(t.material), t.base, t.specification) for i, t in enumerate(temps)])
            for t in temps:
                self._put_temp_lines(sid, t.code, t.lines)
            self._touch(sid)

    def _rename_changes(self, sid: int, renamed: Mapping[str, str]) -> None:
        """変更に出てくる仮品番の品番を替える。同じ箇所に替えた後の品番の足した箇所がもうあれば足す。"""
        if not renamed:
            return
        merged: dict[ChangeKey, Change] = {}
        for plan, product, parent, *slot in self._conn.execute(
                "SELECT plan, product, parent, balloon, now, part, quantity, given FROM v2_slot WHERE study_id = ? ORDER BY seq", (sid,)).fetchall():
            s = _slot_py(*slot)
            change = Change(plan, product, parent, s._replace(part=renamed.get(s.part, s.part)))
            if change.key in merged:
                change = Change(plan, product, parent, change.slot._replace(quantity=change.slot.quantity + merged[change.key].slot.quantity))
            merged[change.key] = change
        self._conn.execute("DELETE FROM v2_slot WHERE study_id = ?", (sid,))
        for c in merged.values():
            self._insert(sid, c.plan, c.product, c.slot, c.parent)

    def rename_temp(self, number: str, code: Any, new: Any) -> None:
        """仮品番の品番を替える。"""
        old_code, new_code = input_code(code), input_code(new, name="新しい品番")
        temps = self._temps(self.study_id(number))
        if old_code not in {t.code for t in temps}:
            raise ValueError(f"{old_code} は仮品番ではありません")
        if new_code in self.load_bom().items:
            raise ValueError(f"{new_code} は部品表にある品番です")
        self.save_temps(number, [{"key": t.code, "code": new_code if t.code == old_code else t.code, "name": t.name,
                                  "material": t.material, "base": t.base} for t in temps])

    def _put_temp_lines(self, sid: int, code: str, lines: Iterable[Slot]) -> None:
        merged: dict[tuple[str | None, str | None, str], Slot] = {}
        for s in lines:
            key = (s.balloon, s.now, s.part)
            merged[key] = s if key not in merged else s._replace(quantity=s.quantity + merged[key].quantity)
        self._conn.executemany("INSERT OR REPLACE INTO v2_temp_slot VALUES (?, ?, ?, ?, ?, ?, ?)", [(sid, code, *_slot_db(s)) for s in merged.values()])

    def save_temp_lines(self, number: str, code: Any, rows: Sequence[Mapping[str, Any]]) -> None:
        """流用した仮品番の書き換えた直下（balloon・part・quantity・now＝流用元の品番）を、変えた箇所にする。行の無い流用元の箇所は消した箇所。

        now の無い行だけの時は、同じ風船番号で 0 になった品番と増えた品番を置き換えの組にする。
        """
        sid = self.study_id(number)
        target = input_code(code)
        temps = {t.code: t for t in self._temps(sid)}
        temp = temps.get(target)
        if temp is None or temp.base is None:
            raise ValueError(f"{target} は流用した仮品番ではありません")
        bom = self.load_bom()
        now = direct_lines(bom, temp.base)
        if not now:
            raise ValueError(f"流用元 {temp.base} が部品表にありません。取り込み直してから変えてください")
        there = {k[1] for k in now} | {s.part for s in temp.lines}
        slots: dict[tuple[str | None, str | None, str | None], Slot] = {}
        after: dict[SlotKey, Decimal] = {}
        for r in rows:
            if not str(r.get("part") or "").strip():
                continue
            balloon = known_balloon(r.get("balloon"), {k[0] for k in now})
            part = known_code(r["part"], {*bom.items, *there})
            qty = r.get("quantity")
            amount = Decimal(0) if qty is None or str(qty).strip() == "" else input_amount(qty)
            if amount < 0:
                raise ValueError(f"数量がマイナスです（{part}）")
            after[(balloon, part)] = after.get((balloon, part), Decimal(0)) + amount
            origin = r.get("now")
            if origin is not None and (balloon, origin) in now:
                slots[(balloon, origin, None)] = Slot(balloon, origin, part if amount else origin, amount, now[(balloon, origin)])
            elif amount != 0:
                key = (balloon, None, part)
                slots[key] = Slot(balloon, None, part, amount + (slots[key].quantity if key in slots else 0), None)
        refuse_unregistered(bom, [k[1] for k in after if k[1] not in there and k[1] not in temps])
        if any("now" in r for r in rows):
            lines = [slots.pop((b, p, None), Slot(b, p, p, Decimal(0), q)) for (b, p), q in now.items()]
            lines = settle_slots(now, [s for s in lines if s.part != s.now or s.quantity != s.given] + list(slots.values()))
        else:
            lines = pair_slots(now, {k: d for k in dict.fromkeys([*now, *after]) if (d := after.get(k, Decimal(0)) - now.get(k, Decimal(0))) != 0})
        temp_cycle(bom, {**temps, target: temp._replace(lines=tuple(lines))}, target)
        with self._conn:
            self._conn.execute("DELETE FROM v2_temp_slot WHERE study_id = ? AND code = ?", (sid, target))
            self._put_temp_lines(sid, target, lines)
            self._touch(sid)

    def save_inside(self, ws: Workspace, plan: str, added: Sequence[Temp], lines: Mapping[str, Sequence[Slot]],
                    picks: Sequence[tuple[str, Placed, str]]) -> None:
        """仮品番を足し、仮品番の中（lines）を入れ替え、直下（picks＝製品・箇所・新品番）を置き換える。"""
        sid = self.study_id(ws.number)
        with self._conn:
            at = self._conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM v2_temp_item WHERE study_id = ?", (sid,)).fetchone()[0]
            self._conn.executemany(
                "INSERT INTO v2_temp_item (study_id, position, code, name, material, base, specification) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(sid, at + i, t.code, t.name, _txt(t.material), t.base, t.specification) for i, t in enumerate(added)])
            for code, rows in [*((t.code, t.lines) for t in added), *lines.items()]:
                self._conn.execute("DELETE FROM v2_temp_slot WHERE study_id = ? AND code = ?", (sid, code))
                self._put_temp_lines(sid, code, rows)
            self._apply(ws, plan, picks)

    def drop_deep_changes(self, number: str, plan: str) -> int:
        """前に入れた2階層目の変更を消し、消した数を返す。"""
        sid = self.study_id(number)
        with self._conn:
            n = self._conn.execute("DELETE FROM v2_slot WHERE study_id = ? AND plan = ? AND parent <> product",
                                   (sid, plan)).rowcount
            self._touch(sid)
        return n

    # 計算条件・版 -----------------------------------------------------------

    def save_conditions(self, number: str, annual: Mapping[str, Any]) -> None:
        """年間台数を、対象品番の分だけ入れ替える（外している製品の分は、戻した時のために残す）。"""
        sid = self.study_id(number)
        targets = {r[0] for r in self._conn.execute("SELECT product FROM v2_target WHERE study_id = ?", (sid,))}
        values = {known_code(product, targets): input_amount(value, name="年間台数")
                  for product, value in annual.items() if value is not None and str(value).strip() != ""}
        for product, value in values.items():
            if value < 0:
                raise ValueError(f"年間台数は0以上で入力してください（{product}）")
        with self._conn:
            self._conn.execute("DELETE FROM v2_condition WHERE study_id = ? AND product IN "
                               "(SELECT product FROM v2_target WHERE study_id = ?)", (sid, sid))
            self._conn.executemany("INSERT OR REPLACE INTO v2_condition VALUES (?, ?, ?)",
                                   [(sid, p, str(v)) for p, v in values.items()])
            self._touch(sid)

    def take_release(self, number: str) -> int:
        """今の仮と、対象にかかる部品表の写しを次の版として固める。"""
        ws = self.workspace(number)
        sid = self.study_id(number)
        snapshot = _snapshot(ws)
        with self._conn:
            row = self._conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM v2_release WHERE study_id = ?",
                                     (sid,)).fetchone()
            version = row[0]
            self._conn.execute("INSERT INTO v2_release VALUES (?, ?, ?, ?)",
                               (sid, version, _now(), json.dumps(snapshot, ensure_ascii=False)))
        return version

    def _release_workspace(self, number: str, version: int) -> Workspace:
        sid = self.study_id(number)
        row = self._conn.execute("SELECT snapshot FROM v2_release WHERE study_id = ? AND version = ?",
                                 (sid, version)).fetchone()
        if row is None:
            raise KeyError(f"{number} の版{version} はありません")
        return _from_snapshot(json.loads(row[0]), version)


def _changes_from_deltas(bom: Version, rows: Iterable[tuple[str, str, str, str | None, str, Decimal, str]]) -> list[Change]:
    """前の版の変更（案・製品・親・風船番号・品番・増減・書いた操作）を、箇所にする。"""
    groups: dict[tuple[str, str, str], tuple[dict[SlotKey, Decimal], set[SlotKey]]] = {}
    for plan, product, parent, balloon, part, delta, source in rows:
        deltas, apart = groups.setdefault((plan, product, parent), ({}, set()))
        deltas[(balloon, part)] = delta
        if source == "add":
            apart.add((balloon, part))
    return [Change(plan, product, parent, slot, i) for (plan, product, parent), (deltas, apart) in groups.items()
            for i, slot in enumerate(pair_slots(direct_lines(bom, parent), deltas, apart))]


def _rates_snapshot(rates: settings.Rates) -> list[str]:
    return [str(rates.adjustment), str(rates.indirect)]


def _snapshot(ws: Workspace) -> dict[str, Any]:
    bom = ws.bom
    codes: set[str] = set(ws.targets)
    for c in ws.changes:
        codes |= {x for x in (c.parent, c.slot.now, c.slot.part) if x is not None}
    for t in ws.temps:
        codes |= {c for c in (t.code, t.base, *(s.part for s in t.lines)) if c is not None}
    stack = list(codes)
    while stack:
        for line in bom.children(stack.pop(), by_balloon=True):
            if line.child not in codes:
                codes.add(line.child)
                stack.append(line.child)
    return {
        "header": ws.header,
        "targets": ws.targets,
        "main_product": ws.main_product,
        "rates": _rates_snapshot(ws.use_rates()),
        "plans": [{"plan": p.plan} for p in ws.plans],
        "slots": [[c.plan, c.product, c.parent, *_slot_db(c.slot), c.seq] for c in ws.changes],
        "conditions": {k: str(v) for k, v in ws.conditions.items()},
        # 仮品番は bom の写しにも入る（変更に出る品番は写す）。画面で見せるために元の行も残す
        "temps": [[t.code, t.name, _txt(t.material), t.base, [list(_slot_db(s)) for s in t.lines], t.specification] for t in ws.temps],
        "level1": ws.level1,
        "hold": ws.hold,
        "strict": ws.strict,
        "temp_merged": sorted(ws.temp_merged),
        "owns": {plan: [[p, h, str(v)] for (p, h), v in values.items()] for plan, values in ws.owns.items()},
        "bom": {
            "items": [[i.code, i.name, i.kind, i.specification, *(ws.attribute(i.code, n) for n, _ in ATTRIBUTES), i.lifecycle]
                      for i in bom.items.values() if i.code in codes],
            "lines": [[l.parent, l.balloon, l.child, str(l.quantity)] for l in bom.lines if l.parent in codes],
            "costs": [[r.code, *(_txt(r.value(h)) for _, h in COST_COLUMNS)] for r in bom.cost_rows
                      if r.code in codes],
            "volumes": [[v.product, str(v.annual)] for v in bom.volumes.values() if v.product in codes],
        },
    }


def _from_snapshot(data: dict[str, Any], version: int) -> Workspace:
    bom = Version(f"v2-{version}")
    b = data["bom"]
    attributes: Attributes = {}
    for code, name, kind, spec, *extra in b["items"]:
        # 状態は属性の後ろ（足す前に固めた版には無い）
        bom.add_item(Item(code, name, kind, extra[len(ATTRIBUTES)] if len(extra) > len(ATTRIBUTES) else "", specification=spec))
        attributes[code] = {n: (extra[i] if i < len(extra) else "") for i, (n, _) in enumerate(ATTRIBUTES)}
    bom.add_bom_lines(BomLine(p, bl, ch, Decimal(q)) for p, bl, ch, q in b["lines"])
    for row in b["costs"]:
        bom.add_cost(CostRow(row[0], {h: _dec(t) for (_, h), t in zip(COST_COLUMNS, row[1:])}))
    for product, annual in b["volumes"]:
        bom.add_volume(Volume(product, Decimal(annual)))
    rates = data.get("rates")  # 率を残す前に固めた版には無い
    by_slot = "slots" in data  # 箇所で持つ前に固めた版は、増減で残っている

    def temp(code: str, name: str, material: str | None, *rest: Any) -> Temp:
        base, rows = (rest[0], rest[1]) if rest else (None, [])  # 流用を足す前に固めた版は (品番, 名称, 材料費) の3つ
        spec = rest[2] if len(rest) > 2 else ""  # 仕様を足す前に固めた版には無い
        lines = ([_slot_py(*s) for s in rows] if by_slot
                 else pair_slots(direct_lines(bom, base) if base else {}, {(b, p): Decimal(d) for b, p, d in rows}))
        return Temp(code, name, _dec(material), base, tuple(lines), spec)

    if by_slot:
        changes = [Change(plan, product, parent, _slot_py(*slot), seq) for plan, product, parent, *slot, seq in data["slots"]]
    else:
        changes = _changes_from_deltas(bom, [(p, pr, pa, bl, pt, Decimal(d), s) for p, pr, pa, bl, pt, d, s, *_ in data["changes"]])
    return Workspace(
        number=data["header"]["number"],
        version=version,
        header=data["header"],
        targets=list(data["targets"]),
        main_product=data.get("main_product"),
        plans=[PlanInfo(p["plan"]) for p in data["plans"]],
        changes=changes,
        conditions={k: Decimal(v) for k, v in data["conditions"].items()},
        bom=bom,
        attributes=attributes,
        temps=[temp(*row) for row in data.get("temps", [])],
        level1=data.get("level1", False),
        hold=data.get("hold", False),
        strict=data.get("strict", False),
        temp_merged=set(data.get("temp_merged", [])),
        rates=None if rates is None else settings.Rates(Decimal(rates[0]), Decimal(rates[1])),
        owns={plan: {(p, h): Decimal(v) for p, h, v in rows} for plan, rows in data.get("owns", {}).items()},
    )
