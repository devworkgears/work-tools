"""設計検討（/v2/）: 部品の差し替えを、対象の製品にまたがって案ごとに試算する。

検討が持つのは「製品×親×風船番号×品番の数量の増減」（変更）だけで、部品表は計算のたびに今の物を読む。
部品変更（品番・数量・部品追加）と構成変更は同じ変更の集まりに書き、同じ箇所は後に書いた方が勝つ（足し算しない）。
変えられるのは製品の直下（1階層目）だけ。組立品の中を替える時は、組立品を流用した仮品番を作り、直下で置き換える。
版確定の時だけ、そのときの検討と部品表（対象にかかる分）を写して固める。
"""

import json
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

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
CREATE TABLE IF NOT EXISTS v2_replace (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    plan TEXT NOT NULL,
    position INTEGER NOT NULL,
    old_part TEXT NOT NULL,
    new_part TEXT NOT NULL,
    balloon TEXT,
    PRIMARY KEY (study_id, plan, position)
);
CREATE TABLE IF NOT EXISTS v2_change (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    plan TEXT NOT NULL,
    product TEXT NOT NULL,
    parent TEXT NOT NULL,
    balloon TEXT NOT NULL,
    part TEXT NOT NULL,
    delta TEXT NOT NULL,
    source TEXT NOT NULL,
    origin TEXT,
    seq INTEGER NOT NULL,
    PRIMARY KEY (study_id, plan, product, parent, balloon, part)
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
    PRIMARY KEY (study_id, position)
);
CREATE TABLE IF NOT EXISTS v2_temp_line (
    study_id INTEGER NOT NULL REFERENCES v2_study(study_id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    balloon TEXT NOT NULL,
    part TEXT NOT NULL,
    delta TEXT NOT NULL,
    PRIMARY KEY (study_id, code, balloon, part)
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

REPLACE = "replace"
QUANTITY = "quantity"
ADD = "add"
KEEP = object()  # save_targets で主対象を今のまま持つ印
REWRITE = "rewrite"

NOT_TARGET = "対象品番に無い"
NO_PLACE = "箇所が無い"
OTHER_PART = "元の品番が違う"
NEGATIVE = "数量がマイナス"
UNREGISTERED = "未登録"
PAIR_OUT = "置換の相手が外れた"
WAITING = "単価待ち"  # 変更の問題ではなく、⑦ と Excel の影響の印
DEEP = "2階層目の変更"
# 計算に入れない問題。未登録は入れて、結果に不完全の印を付ける
EXCLUDING = frozenset({NOT_TARGET, NO_PLACE, OTHER_PART, NEGATIVE, PAIR_OUT, DEEP})
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

Place = tuple[str, str, str | None, str]  # 製品・親・風船番号・品番


@dataclass(frozen=True)
class StoredChange:
    plan: str
    product: str
    parent: str
    balloon: str | None
    part: str
    delta: Decimal
    source: str
    origin: str | None
    seq: int

    @property
    def place(self) -> Place:
        return (self.product, self.parent, self.balloon, self.part)


@dataclass(frozen=True)
class CheckedChange:
    change: StoredChange
    before: Decimal
    after: Decimal
    problems: tuple[str, ...]
    added: bool  # その箇所に元は無かった品番

    @property
    def counted(self) -> bool:
        return not EXCLUDING & set(self.problems)


@dataclass
class PlanInfo:
    plan: str


# 置換の行（元品番, 新品番, 風船番号）。風船番号 None は全部の箇所・"" は風船番号の無い箇所
ReplaceRow = tuple[str, str, str | None]
# 仮品番の中の変更（風船番号, 品番, 流用元からの数量の増減）
TempLine = tuple[str | None, str, Decimal]


class Temp(NamedTuple):
    """仮品番。部品の原価は製品の材料費にだけ入るので、値は材料費だけ（None は未登録か、流用元から計算）。

    base は流用元の組立品、lines は流用元の直下からの変更。material が空なら、流用元の材料費に変更の差を足す。
    """

    code: str
    name: str
    material: Decimal | None = None
    base: str | None = None
    lines: tuple[TempLine, ...] = ()


@dataclass
class Workspace:
    """1つの検討の、仮（version=None）か固めた版の中身。bom は仮なら今の部品表・版なら写し。"""

    number: str
    version: int | None
    header: dict[str, Any]
    targets: list[str]
    main_product: str | None
    plans: list[PlanInfo]
    # 案 → (元品番, 新品番, 風船番号)。風船番号 None は全部の箇所・"" は風船番号の無い箇所
    replaces: dict[str, list[ReplaceRow]]
    changes: list[StoredChange]
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

    def use_rates(self) -> settings.Rates:
        return self.rates or settings.rates()

    def attribute(self, code: str, name: str) -> str:
        return self.attributes.get(code, {}).get(name, "")

    def plan_ids(self) -> list[str]:
        return [p.plan for p in self.plans]

    def plan_changes(self, plan: str) -> list[StoredChange]:
        return [c for c in self.changes if c.plan == plan]


def merge_temps(bom: Version, temps: Sequence[Temp]) -> set[str]:
    """仮品番のうち bom の品目に無い物を、品目と材料費の行として足す。足した品番を返す。

    流用した仮品番で材料費を打っていない物は、流用元の材料費＋Σ（増減×部品の材料費）。1つでも分からなければ未登録のまま。
    """
    fresh = {t.code: t for t in temps if t.code not in bom.items}
    derived = {c: t for c, t in fresh.items() if t.base is not None and t.material is None}
    for t in fresh.values():
        bom.add_item(Item(t.code, t.name))
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
        _, after = temp_lines(bom, t)
        if total is None or any(qty < 0 for qty in after.values()):
            return None
        for _, part, delta in t.lines:
            price = unit(part) if part in bom.items else None
            if price is None:
                return None
            total += delta * price
        return total

    for code in derived:
        unit(code)
    for code in derived:
        bom.add_cost(CostRow(code, {CostHead.MATERIAL: values[code]}))
    return set(fresh)


SlotKey = tuple[str | None, str]  # （風船番号, 品番）


def direct_lines(bom: Version, code: str) -> dict[SlotKey, Decimal]:
    """直下の箇所ごとの数量。"""
    out: dict[SlotKey, Decimal] = {}
    for line in bom.children(code, by_balloon=True):
        key = (line.balloon, line.child)
        out[key] = out.get(key, Decimal(0)) + line.quantity
    return out


def temp_lines(bom: Version, temp: Temp) -> tuple[dict[SlotKey, Decimal], dict[SlotKey, Decimal]]:
    """流用した仮品番の直下を（流用元, 変えた後）で返す。"""
    now = direct_lines(bom, temp.base) if temp.base is not None else {}
    after = dict(now)
    for balloon, part, delta in temp.lines:
        after[(balloon, part)] = after.get((balloon, part), Decimal(0)) + delta
    return now, after


def temp_cycle(bom: Version, temps: Mapping[str, Temp], code: str) -> None:
    """仮品番の中に自分が（別の仮品番を通しても）入ると、材料費が決まらないので断る。"""
    stack, seen = [part for _, part, d in temps[code].lines if d > 0], set()
    while stack:
        part = stack.pop()
        if part == code:
            raise ValueError(f"{code} の中に {code} 自身は入れられません")
        if part in temps and part not in seen:
            seen.add(part)
            stack += [k[1] for k, q in temp_lines(bom, temps[part])[1].items() if q > 0]


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
    return list(dict.fromkeys(c.part for c in ws.changes if c.part not in ws.bom.items))


def pair_key(old: str, new: str, balloon: str | None = None) -> str:
    """置換の組から書いた変更の出どころ。風船番号で分けた組は「元品番@風船番号>新品番」。"""
    return f"{old}>{new}" if balloon is None else f"{old}@{balloon}>{new}"


def balloon_group(line: BomLine) -> str:
    """置換のまとまりの風船番号（風船番号の無い箇所は空の文字）。"""
    return line.balloon or ""


def replace_groups(bom: Version, targets: Sequence[str], part: str) -> list[tuple[str, list[str]]]:
    """対象品番の中で part を置き換えられる箇所を、風船番号ごとのまとまり（風船番号, 製品）にする。"""
    groups: dict[str, list[str]] = {}
    for product in targets:
        for line in replace_lines(bom, product, part):
            members = groups.setdefault(balloon_group(line), [])
            if product not in members:
                members.append(product)
    return sorted(groups.items(), key=lambda kv: (kv[0] == "", _natural(kv[0])))


def _natural(text: str) -> tuple[int, str]:
    return (int(text), "") if text.isdigit() else (10 ** 9, text)


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


def occurrences(bom: Version, product: str, part: str) -> list[BomLine]:
    """製品の直下（1階層目）でその品番がある行。"""
    return [line for line in bom.children(product, by_balloon=True) if line.child == part]


def replace_lines(bom: Version, product: str, part: str) -> list[BomLine]:
    """置換が効く箇所。数量がマイナスの行（抜く部品）は置き換える物が無いので外す。"""
    return [line for line in occurrences(bom, product, part) if line.quantity > 0]


def line_quantity(bom: Version, parent: str, balloon: str | None, part: str) -> Decimal | None:
    total: Decimal | None = None
    for line in bom.children(parent, by_balloon=True):
        if line.balloon == balloon and line.child == part:
            total = (total or Decimal(0)) + line.quantity
    return total


def parts_of(bom: Version, products: Sequence[str]) -> list[tuple[str, int]]:
    """製品たちの直下の部品と、そのうち何製品で使うか。品番の順。"""
    used: dict[str, int] = {}
    for product in dict.fromkeys(products):
        for code in dict.fromkeys(line.child for line in bom.children(product)):
            used[code] = used.get(code, 0) + 1
    return sorted(used.items())


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


def check_changes(ws: Workspace, changes: Iterable[StoredChange] | None = None) -> list[CheckedChange]:
    """変更を今の部品表と突き合わせる。どの画面から書いた変更も同じ決まりで見る。"""
    bom = ws.bom
    targets = set(ws.targets)
    usages: dict[str, dict[str, Decimal]] = {}
    result = []
    for c in ws.changes if changes is None else changes:
        problems: list[str] = []
        before = line_quantity(bom, c.parent, c.balloon, c.part)
        if c.product not in targets:
            problems.append(NOT_TARGET)
        if ws.level1 and c.parent != c.product:
            problems.append(DEEP)
        usage = usages.setdefault(c.product, product_usage(bom, c.product))
        if c.parent not in usage:
            problems.append(NO_PLACE)
        elif before is None and c.delta < 0:
            others = [l for l in bom.children(c.parent, by_balloon=True)
                      if c.balloon is not None and l.balloon == c.balloon]
            problems.append(OTHER_PART if others else NO_PLACE)
        base = before or Decimal(0)
        after = base + c.delta
        if after < 0 and NO_PLACE not in problems and OTHER_PART not in problems:
            problems.append(NEGATIVE)
        if c.part not in bom.items:
            problems.append(UNREGISTERED)
        result.append(CheckedChange(c, base, after, tuple(problems), before is None and c.delta > 0))
    # 置換から入った −元・＋新 の組は、片方が外れたら両方外す（片方だけ効くと部品が消える・増える）
    out_pairs = {(r.change.plan, r.change.product, r.change.parent, r.change.balloon, r.change.origin)
                 for r in result if r.change.source == REPLACE and not r.counted}
    return [CheckedChange(r.change, r.before, r.after, r.problems + (PAIR_OUT,), r.added)
            if r.change.source == REPLACE and r.counted
            and (r.change.plan, r.change.product, r.change.parent, r.change.balloon, r.change.origin) in out_pairs
            else r for r in result]


LineKey = tuple[str, str | None, str]  # （親, 風船番号, 品番）


def after_lines(ws: Workspace, plan: str, product: str) -> tuple[dict[LineKey, Decimal], dict[LineKey, Decimal]]:
    """製品の直下の箇所ごとの数量を（今の部品表, 変えた後）で返す。変えた後＝今＋案の変更。"""
    now: dict[LineKey, Decimal] = {(product, *k): q for k, q in direct_lines(ws.bom, product).items()}
    after = dict(now)
    for c in ws.plan_changes(plan):
        if c.product == product and c.parent == product:
            key = (c.parent, c.balloon, c.part)
            after[key] = after.get(key, Decimal(0)) + c.delta
    return now, after


class PartPlace(NamedTuple):
    """変えた後の構成で、部品がある製品の直下の箇所。was は置き換える前の品番（その箇所で減った品番）。

    before は今の数量（置き換えた箇所は元の品番の数量・足した箇所は None）。quantity 0 は、置き換えずに消した箇所。
    """

    product: str
    balloon: str | None
    part: str
    quantity: Decimal
    was: list[str]
    before: Decimal | None


def after_places(ws: Workspace, plan: str) -> list[PartPlace]:
    """対象品番の直下の、変えた後の箇所（対象品番の順・その中は風船の順のまま）。"""
    found = []
    for product in ws.targets:
        now, after = after_lines(ws, plan, product)
        # 足した部品は、同じ風船番号（無い時も）で減った品番の置き換えとは見ない
        extra = {c.place[1:] for c in ws.plan_changes(plan) if c.product == product and c.source == ADD}
        down: dict[str | None, list[str]] = {}
        up: set[str | None] = set()
        for key, qty in after.items():
            if qty < now.get(key, Decimal(0)):
                down.setdefault(key[1], []).append(key[2])
            elif qty > now.get(key, Decimal(0)) and key not in extra:
                up.add(key[1])
        for key, qty in after.items():
            if qty == 0 and (key not in now or key[1] in up):
                continue
            was = sorted(down.get(key[1], [])) if qty > now.get(key, Decimal(0)) and key not in extra else []
            before = now.get(key, now.get((key[0], key[1], was[0])) if len(was) == 1 else None)
            found.append(PartPlace(product, key[1], key[2], qty, was, before))
    return found


class AfterPart(NamedTuple):
    code: str
    users: list[str]  # 変えた後に直下で使う対象品番
    before: int  # 今の部品表で直下で使う対象品番の数
    was: list[str]


def after_parts(ws: Workspace, plan: str) -> list[AfterPart]:
    """対象品番の直下の、変えた後の部品（品番の順）。"""
    before = dict(parts_of(ws.bom, ws.targets))
    found: dict[str, tuple[dict[str, None], set[str]]] = {}
    for p in after_places(ws, plan):
        if p.quantity == 0:
            continue
        users, was = found.setdefault(p.part, ({}, set()))
        users[p.product] = None
        was.update(p.was)
    return [AfterPart(code, list(users), before.get(code, 0), sorted(was)) for code, (users, was) in sorted(found.items())]


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


def held_changes(ws: Workspace, checked: Iterable[CheckedChange], calc: CostCalculator) -> set[tuple[str, Place]]:
    """単価の無い品番に置き換えた箇所の、元の品番の引き（案, 箇所）。単価が分かるまで引かない（引くと、ただで付く値になる）。

    組の見方は ⑥ と同じ（同じ箇所で 0 になった品番が1つ・増えた品番が1つ）。2つ以上ある時は、数の合う相手が1つの時だけ。
    """
    held: set[tuple[str, Place]] = set()
    if not ws.hold:
        return held
    slots: dict[tuple[str, str, str, str | None], list[CheckedChange]] = {}
    for c in checked:
        ch = c.change
        slots.setdefault((ch.plan, ch.product, ch.parent, ch.balloon), []).append(c)
    for members in slots.values():
        up = [c.change for c in members if c.change.delta > 0 and c.change.source != ADD]
        down = [c.change for c in members if c.change.delta < 0 and c.after == 0]
        for new in up:
            if calc.item_cost(new.part) is not None:
                continue
            pair = down if len(up) == 1 else [d for d in down if d.delta == -new.delta]
            if len(pair) == 1:
                held.add((new.plan, pair[0].place))
    return held


class MaterialEffect(NamedTuple):
    value: Decimal | None  # 製品1台あたりの材料費小計の増減。計算に入らない変更・単価の無い変更・それを待つ変更は None
    waiting: bool  # 単価待ち


def material_effects(ws: Workspace, plan: str) -> dict[Place, MaterialEffect]:
    """案の変更1つずつの、材料費小計への効き方。"""
    calc = CostCalculator(ws.bom, CostHead.MATERIAL)
    usages: dict[str, dict[str, Decimal]] = {}
    out: dict[Place, MaterialEffect] = {}
    checked = check_changes(ws, ws.plan_changes(plan))
    held = held_changes(ws, [c for c in checked if c.counted], calc)
    for c in checked:
        ch = c.change
        unit, waiting = None, False
        if c.counted:
            unit = calc.item_cost(ch.part)
            waiting = unit is None or (plan, ch.place) in held
            try:
                if waiting or calc.item_cost(ch.product) is None:  # 今の値が出ない製品は、変えた後の値も出ない
                    unit = None
            except ValueError:
                unit = None
        if ch.product not in usages:
            usages[ch.product] = product_usage(ws.bom, ch.product)
        out[ch.place] = MaterialEffect(
            None if unit is None else ch.delta * usages[ch.product].get(ch.parent, Decimal(0)) * unit, waiting)
    return out


def calculate_heads(ws: Workspace, heads: Sequence[Head] = COMPARE_HEADS) -> dict[Head, dict[str, Any]]:
    """費目ごとに、対象品番ごとの今と案ごとの原価・年間の影響・台数の加重平均。DB は読むだけ（渡した Workspace だけを見る）。

    単価の無い品番への置き換えは、単価が分かるまで現行のまま（不完全の印を付ける）。
    案の値＝（今の材料費小計＋部品の増減）と、製品自身の値（⑩で書き換えた費目はその値）から式で出す。
    base＝年間の影響に入れた製品の、今の値×年間台数の合計（現行比の元）。
    台数の加重平均＝年間の影響 ÷ 年間の影響に入れた製品の年間台数の合計（1台あたりの増減）。台数の合計が 0 なら None。
    """
    bom = ws.bom
    calc = CostCalculator(bom, CostHead.MATERIAL)
    rates = ws.use_rates()
    all_checked = check_changes(ws)
    checked = [c for c in all_checked if c.counted]
    held = held_changes(ws, checked, calc)
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
        try:
            material_result = calc.calculate(product)
        except ValueError:
            material_result = None
        now_material = None if material_result is None else material_result.total
        # 組立品の未登録は子から足し上げて出るので、不完全は子の無い品目の未登録だけ
        now_missing = material_result is None or any(not bom.children(m) for m in material_result.missing)
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
                unit = calc.item_cost(c.change.part)
                if unit is None or (plan, c.change.place) in held:
                    missing = True
                    continue
                delta += c.change.delta * usage.get(c.change.parent, Decimal(0)) * unit
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


def own_rows(ws: Workspace, plan: str) -> list[dict[str, Any]]:
    """⑩ の行: 対象品番ごとの名称と、書き換える費目の今の値（台帳）と書き換えた値。"""
    rows = []
    for product in ws.targets:
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


def _balloon_db(balloon: str | None) -> str:
    return "" if balloon is None else balloon


def _balloon_py(text: str) -> str | None:
    return None if text == "" else text


def _dec(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _txt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class StudyStore:
    """設計検討の表（v2_*）。部品表もこの表に別に持ち、元の画面のグループとは混ぜない。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        # 属性の列より前に作った表には列を足す
        present = {r[1] for r in conn.execute("PRAGMA table_info(v2_item)")}
        for name in (*(n for n, _ in ATTRIBUTES), "lifecycle"):
            if name not in present:
                conn.execute(f"ALTER TABLE v2_item ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        # 置換の組に風船番号の列を足す前に作った表には列を足す（NULL＝全部の箇所）
        if "balloon" not in {r[1] for r in conn.execute("PRAGMA table_info(v2_replace)")}:
            conn.execute("ALTER TABLE v2_replace ADD COLUMN balloon TEXT")
        # 流用元の列を足す前に作った仮品番の表
        if "base" not in {r[1] for r in conn.execute("PRAGMA table_info(v2_temp_item)")}:
            conn.execute("ALTER TABLE v2_temp_item ADD COLUMN base TEXT")
        conn.commit()

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

        空の項目は比べない（設定の項目が増えた版に替えただけでは消さない）。
        """
        now = json.dumps({k: v for k, v in settings.current().to_json().items() if v}, ensure_ascii=False, sort_keys=True)
        row = self._conn.execute("SELECT settings FROM v2_bom_settings").fetchone()
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
        replaces: dict[str, list[ReplaceRow]] = {}
        for plan, old, new, balloon in q("SELECT plan, old_part, new_part, balloon FROM v2_replace WHERE study_id = ? "
                                         "ORDER BY plan, position", (sid,)):
            replaces.setdefault(plan, []).append((old, new, balloon))
        changes = [StoredChange(p, pr, pa, _balloon_py(b), pt, Decimal(d), s, o, seq) for p, pr, pa, b, pt, d, s, o, seq
                   in q("SELECT plan, product, parent, balloon, part, delta, source, origin, seq FROM v2_change "
                        "WHERE study_id = ? ORDER BY seq", (sid,))]
        conditions = {p: Decimal(a) for p, a in q("SELECT product, annual FROM v2_condition WHERE study_id = ?",
                                                  (sid,))}
        owns: dict[str, dict[tuple[str, str], Decimal]] = {}
        for plan, product, head, value in q("SELECT plan, product, head, value FROM v2_own WHERE study_id = ?", (sid,)):
            owns.setdefault(plan, {})[(product, head)] = Decimal(value)
        temps = self._temps(sid)
        bom = self.load_bom()
        merged = merge_temps(bom, temps)
        return Workspace(n, None, header, targets, main, plans or [PlanInfo("A")], replaces, changes, conditions,
                         bom, self.load_attributes(), temps=temps, temp_merged=merged, owns=owns)

    def _temps(self, sid: int) -> list[Temp]:
        q = self._conn.execute
        lines: dict[str, list[TempLine]] = {}
        for code, balloon, part, delta in q(
                "SELECT code, balloon, part, delta FROM v2_temp_line WHERE study_id = ? ORDER BY rowid", (sid,)):
            lines.setdefault(code, []).append((_balloon_py(balloon), part, Decimal(delta)))
        return [Temp(c, nm, _dec(m), base, tuple(lines.get(c, ()))) for c, nm, m, base in q(
            "SELECT code, name, material, base FROM v2_temp_item WHERE study_id = ? ORDER BY position", (sid,))]

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

    def _put(self, sid: int, plan: str, place: Place, delta: Decimal, source: str, origin: str | None = None) -> None:
        product, parent, balloon, part = place
        if delta == 0:
            self._delete(sid, plan, place)
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO v2_change VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, plan, product, parent, _balloon_db(balloon), part, str(delta), source, origin,
             self._next_seq(sid)))

    def _delete(self, sid: int, plan: str, place: Place) -> None:
        product, parent, balloon, part = place
        self._conn.execute(
            "DELETE FROM v2_change WHERE study_id = ? AND plan = ? AND product = ? AND parent = ? AND balloon = ? "
            "AND part = ?", (sid, plan, product, parent, _balloon_db(balloon), part))

    # 対象品番 ---------------------------------------------------------------

    def save_targets(self, number: str, products: Sequence[Any], main_product: Any = KEEP) -> None:
        """対象品番を入れ替える。main_product を渡さなければ、今の主対象を（対象品番に残れば）そのまま持つ。"""
        sid = self.study_id(number)
        codes = list(dict.fromkeys(input_code(p, name="対象品番") for p in products
                                   if p is not None and str(p).strip()))
        if main_product is KEEP:
            main_product = self._conn.execute("SELECT main_product FROM v2_study WHERE study_id = ?",
                                              (sid,)).fetchone()[0]
        main = input_optional_code(main_product, name="主対象")
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
        main = input_optional_code(main_product, name="主対象")
        targets = [r[0] for r in self._conn.execute("SELECT product FROM v2_target WHERE study_id = ?", (sid,))]
        if main is not None and main not in targets:
            raise ValueError(f"主対象は対象品番から選んでください（{main}）")
        with self._conn:
            self._conn.execute("UPDATE v2_study SET main_product = ? WHERE study_id = ?", (main, sid))
            self._touch(sid)

    # 部品変更 -------------------------------------------------------------------

    def apply_replace(self, number: str, plan: str, old: Any, new: Any) -> None:
        """変えた後の構成で、対象品番の全製品の直下の old を new に置き換える（1回きりの操作で、決まりとしては残さない）。

        同じ箇所に new が既にあれば数量を足す。今の部品表に戻った箇所の変更は消える。
        """
        old_code, new_code = input_code(old, name="元品番"), input_code(new, name="新品番")
        if old_code == new_code:
            return
        ws = self.workspace(number)
        refuse_unregistered(ws.bom, [new_code])
        self._replace_at(ws, plan, [(p.product, p.balloon, old_code, new_code)
                                    for p in after_places(ws, plan) if p.part == old_code])

    def apply_places(self, number: str, plan: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """箇所（product・balloon）ごとに old を new に置き換える。1行でも当たらなければ、どれも変えない。"""
        ws = self.workspace(number)
        picks: list[tuple[str, str | None, str, str]] = []
        for r in rows:
            product = input_code(r.get("product"), name="製品")
            if product not in ws.targets:
                raise ValueError(f"{product} は対象品番にありません")
            old, new = input_code(r.get("old"), name="元品番"), input_code(r.get("new"), name="新品番")
            pick = (product, input_optional_code(r.get("balloon"), name="風船"), old)
            if pick in (p[:3] for p in picks):
                raise ValueError(f"{product} の {old} が2行あります")
            if old != new:
                picks.append((*pick, new))
        refuse_unregistered(ws.bom, [p[3] for p in picks])
        if picks:
            self._replace_at(ws, plan, picks)

    def _replace_at(self, ws: Workspace, plan: str, picks: Sequence[tuple[str, str | None, str, str]]) -> None:
        """picks（製品, 風船番号, 元品番, 新品番）を、始める前の構成から見て同時に置き換える。"""
        sid = self.study_id(ws.number)
        states: dict[str, tuple[dict[LineKey, Decimal], dict[LineKey, Decimal]]] = {}
        moved: dict[Place, Decimal] = {}
        if not picks:
            raise ValueError("対象品番の直下に、置き換える品番がありません")
        for product, balloon, old, new in picks:
            if product not in states:
                states[product] = after_lines(ws, plan, product)
            qty = states[product][1].get((product, balloon, old), Decimal(0))
            if qty == 0:
                raise ValueError(f"{old} は {product} の直下にありません（画面を開き直してください）")
            for part, change in ((old, -qty), (new, qty)):
                place = (product, product, balloon, part)
                moved[place] = moved.get(place, Decimal(0)) + change
        with self._conn:
            self._ensure_plan(sid, plan)
            for place, change in moved.items():
                now, after = states[place[0]]
                self._put(sid, plan, place, after.get(place[1:], Decimal(0)) + change - now.get(place[1:], Decimal(0)), REPLACE)
            self._touch(sid)

    def revert_replace(self, number: str, plan: str, part: Any, place: tuple[Any, Any] | None = None) -> None:
        """part が今の部品表より増えた直下の箇所（風船番号）を、今の部品表に戻す。place（製品, 風船番号）があればその箇所だけ。

        もともと part を使っている箇所には触れない（part を打ち替えて戻すと、そこまで変わってしまう）。
        """
        code = input_code(part)
        only = None if place is None else (input_code(place[0], name="製品"), input_optional_code(place[1], name="風船"))
        ws = self.workspace(number)
        sid = self.study_id(number)
        gone: list[Place] = []
        for product in ws.targets:
            slots: dict[str | None, list[StoredChange]] = {}
            for c in ws.plan_changes(plan):
                if c.product == product and c.parent == product and (only is None or only == (product, c.balloon)):
                    slots.setdefault(c.balloon, []).append(c)
            for members in slots.values():
                members = [c for c in members if c.source != ADD]  # 足した部品は置き換えではない
                mine = next((c for c in members if c.part == code and c.delta > 0), None)
                if mine is None:
                    continue
                down = [c for c in members if c.delta < 0]
                if sum(1 for c in members if c.delta > 0) > 1:
                    # 同じ箇所で2つ以上替えた時（風船番号の無い部品表など）は、数の合う相手が1つの時だけ組とみなす
                    down = [c for c in down if c.delta == -mine.delta]
                    if len(down) != 1:
                        raise ValueError(f"{code} の置き換える前が1つに決まりません（{product}）。構成変更で直してください")
                gone += [mine.place, *(c.place for c in down)]
        if not gone:
            raise ValueError(f"{code} に置き換えた箇所はありません")
        with self._conn:
            for place in gone:
                self._delete(sid, plan, place)
            self._touch(sid)

    def save_quantities(self, number: str, plan: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """箇所（product・balloon・part）ごとに、変えた後の数量を入れる（0 は消す・空は今の部品表に戻す）。1行でも当たらなければ、どれも変えない。"""
        ws = self.workspace(number)
        sid = self.study_id(number)
        current = {c.place: c for c in ws.plan_changes(plan)}
        work: dict[Place, Decimal] = {}
        for r in rows:
            product, part = input_code(r.get("product"), name="製品"), input_code(r.get("part"))
            if product not in ws.targets:
                raise ValueError(f"{product} は対象品番にありません")
            now, after = after_lines(ws, plan, product)
            key = (product, input_optional_code(r.get("balloon"), name="風船"), part)
            if key not in after:
                raise ValueError(f"{part} は {product} の直下にありません（画面を開き直してください）")
            value = r.get("quantity")
            before = now.get(key, Decimal(0))
            amount = before if value is None or str(value).strip() == "" else input_amount(value)
            if amount < 0:
                raise ValueError(f"数量は0以上で入力してください（{value}）")
            work[(product, *key)] = amount - before
        with self._conn:
            self._ensure_plan(sid, plan)
            for place, delta in work.items():
                # 置き換えた・足した箇所の数量を直しても、置き換え・追加のまま
                kept = current.get(place)
                self._put(sid, plan, place, delta, QUANTITY if kept is None else kept.source,
                          None if kept is None else kept.origin)
            self._touch(sid)

    def add_part(self, number: str, plan: str, part: Any, quantity: Any, products: Sequence[Any]) -> None:
        """部品を、製品たちの直下に足す（風船番号は無し）。もう直下にある製品が混ざれば、どれも変えない。"""
        ws = self.workspace(number)
        sid = self.study_id(number)
        code, amount = input_code(part), input_amount(quantity)
        refuse_unregistered(ws.bom, [code])
        if amount <= 0:
            raise ValueError(f"数量は0より大きい数で入力してください（{quantity}）")
        codes = list(dict.fromkeys(input_code(p, name="製品") for p in products))
        if not codes:
            raise ValueError("足す製品を選んでください")
        work: dict[Place, Decimal] = {}
        for product in codes:
            if product not in ws.targets:
                raise ValueError(f"{product} は対象品番にありません")
            if product == code:
                raise ValueError(f"{code} を自分の中には足せません")
            now, after = after_lines(ws, plan, product)
            if any(k[2] == code and q != 0 for k, q in after.items()):
                raise ValueError(f"{code} は {product} にもうあります。数量は「製品別」で変えてください")
            key = (product, None, code)
            work[(product, *key)] = amount - now.get(key, Decimal(0))
        with self._conn:
            self._ensure_plan(sid, plan)
            for place, delta in work.items():
                self._put(sid, plan, place, delta, ADD)
            self._touch(sid)

    def add_places(self, number: str, plan: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """部品を、製品の直下の箇所（product・balloon・part・quantity）に足す。同じ箇所にもうあれば、どれも変えない。"""
        ws = self.workspace(number)
        sid = self.study_id(number)
        work: dict[Place, Decimal] = {}
        for r in rows:
            product, part = input_code(r.get("product"), name="製品"), input_code(r.get("part"))
            amount = input_amount(r.get("quantity"))
            if product not in ws.targets:
                raise ValueError(f"{product} は対象品番にありません")
            if product == part:
                raise ValueError(f"{part} を自分の中には足せません")
            if amount <= 0:
                raise ValueError(f"数量は0より大きい数で入力してください（{amount}）")
            now, after = after_lines(ws, plan, product)
            key = (product, input_optional_code(r.get("balloon"), name="風船"), part)
            if (product, *key) in work or after.get(key, Decimal(0)) != 0:
                raise ValueError(f"{part} は {product} の同じ風船番号にもうあります")
            work[(product, *key)] = amount - now.get(key, Decimal(0))
        refuse_unregistered(ws.bom, [place[3] for place in work])
        with self._conn:
            self._ensure_plan(sid, plan)
            for place, delta in work.items():
                self._put(sid, plan, place, delta, ADD)
            self._touch(sid)

    def save_replaces(self, number: str, plan: str, rows: Sequence[Sequence[Any]]) -> None:
        """置換の行（元品番, 新品番[, 風船番号]）を入れ替え、行ごとの変更を書き直す。

        風船番号を付けた行は、その風船番号の箇所だけに効く（無い・None の行は全部の箇所）。
        手で直した箇所（置換以外から書いた変更）はその箇所だけ残す。
        同じ箇所に2回足されないよう、同じ元品番・同じ風船番号の行や、全部の行と風船番号の行が重なれば ValueError で止めて行を示す。
        """
        ws = self.workspace(number)
        sid = self.study_id(number)
        pairs: list[ReplaceRow] = []
        seen: dict[tuple[str, str | None], int] = {}
        for i, row in enumerate(rows, start=1):
            old, new, *rest = row
            balloon = rest[0] if rest else None
            old_code, new_code = input_code(old, name="元品番"), input_code(new, name="新品番")
            balloon = None if balloon is None else nfkc(str(balloon)).strip()
            if old_code == new_code:
                raise ValueError(f"元品番と新品番が同じです（{i}行目）")
            clash = next((j for (o, b), j in seen.items() if o == old_code and (b == balloon or b is None or balloon is None)),
                         None)
            if clash is not None:
                raise ValueError(f"元品番 {old_code} が重複しています（{clash}行目・{i}行目）")
            seen[(old_code, balloon)] = i
            pairs.append((old_code, new_code, balloon))
        before = ws.replaces.get(plan, [])
        with self._conn:
            self._ensure_plan(sid, plan)
            self._conn.execute("DELETE FROM v2_replace WHERE study_id = ? AND plan = ?", (sid, plan))
            self._conn.executemany("INSERT INTO v2_replace (study_id, plan, position, old_part, new_part, balloon) "
                                   "VALUES (?, ?, ?, ?, ?, ?)",
                                   [(sid, plan, i, o, n, b) for i, (o, n, b) in enumerate(pairs)])
            for old, new, balloon in set(before) | set(pairs):
                self._conn.execute("DELETE FROM v2_change WHERE study_id = ? AND plan = ? AND source = ? "
                                   "AND origin = ?", (sid, plan, REPLACE, pair_key(old, new, balloon)))
            manual = {c.place for c in ws.plan_changes(plan) if c.source != REPLACE}
            for old, new, balloon in pairs:
                for product in ws.targets:
                    for line in replace_lines(ws.bom, product, old):
                        if balloon is not None and balloon_group(line) != balloon:
                            continue
                        for place, delta in (((product, line.parent, line.balloon, old), -line.quantity),
                                             ((product, line.parent, line.balloon, new), line.quantity)):
                            if place not in manual:
                                self._put(sid, plan, place, delta, REPLACE, pair_key(old, new, balloon))
            self._touch(sid)

    # ⑧ 構成変更 -------------------------------------------------------------

    def save_rewrite(self, number: str, plan: str, product: Any, rows: Sequence[Mapping[str, Any]]) -> None:
        """1製品の書き換えた直下の構成（balloon・part・quantity）と今の部品表の差を変更にする。"""
        ws = self.workspace(number)
        sid = self.study_id(number)
        product = input_code(product, name="対象品番")
        before: dict[tuple[str, str | None, str], Decimal] = {}
        for line in ws.bom.children(product, by_balloon=True):
            before[line.key] = before.get(line.key, Decimal(0)) + line.quantity
        after: dict[tuple[str, str | None, str], Decimal] = {}
        for r in rows:
            if not str(r.get("part") or "").strip():
                continue
            if input_code(r.get("parent") or product, name="親") != product:
                raise ValueError("組立品の中はここでは変えられません。仮品番で流用して作り、直下で置き換えてください")
            key = (product, input_optional_code(r.get("balloon"), name="風船"), input_code(r["part"]))
            qty = r.get("quantity")
            amount = Decimal(0) if qty is None or str(qty).strip() == "" else input_amount(qty)
            after[key] = after.get(key, Decimal(0)) + amount
        current = {c.place: c for c in ws.plan_changes(plan) if c.product == product and c.parent == product}
        # もう構成にある品番（部品表の取り直しで後から消えた物）は、そのまま保存できる
        there = {k[2] for k in before} | {p[3] for p in current}
        refuse_unregistered(ws.bom, [k[2] for k in after if k[2] not in there])
        keys = set(before) | set(after) | {p[1:] for p in current}
        with self._conn:
            self._ensure_plan(sid, plan)
            for key in sorted(keys, key=lambda k: (k[0], k[1] or "", k[2])):
                place = (product, *key)
                delta = after.get(key, Decimal(0)) - before.get(key, Decimal(0))
                kept = current.get(place)
                if delta == 0:
                    if kept is not None:
                        self._delete(sid, plan, place)
                elif kept is None or kept.delta != delta:
                    self._put(sid, plan, place, delta, REWRITE)
            self._touch(sid)

    # ⑩ 費目変更 -----------------------------------------------------------

    def save_owns(self, number: str, plan: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """案の製品の費目の書き換え（product・head・value）を入れ替える。値が空の行は書き換えなし（台帳の値）。"""
        sid = self.study_id(number)
        values: dict[tuple[str, str], Decimal] = {}
        for r in rows:
            head = str(r.get("head") or "")
            if head not in OWN_HEADS:
                raise ValueError(f"書き換える費目の指定が違います（{head}）")
            value = r.get("value")
            if value is None or str(value).strip() == "":
                continue
            product = input_code(r.get("product"), name="対象品番")
            values[(product, head)] = input_amount(value, name=OWN_HEADS[head].value)
        with self._conn:
            self._ensure_plan(sid, plan)
            self._conn.execute("DELETE FROM v2_own WHERE study_id = ? AND plan = ?", (sid, plan))
            self._conn.executemany("INSERT INTO v2_own VALUES (?, ?, ?, ?, ?)",
                                   [(sid, plan, p, h, str(v)) for (p, h), v in values.items()])
            self._touch(sid)

    # ⑨ 仮品番 ---------------------------------------------------------------

    def save_temps(self, number: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """仮品番の行（code・name・material・base）を入れ替える。品番の無い行は飛ばし、同じ品番が2行あれば ValueError。

        key は前に保存した時の品番（品番を打ち替えた行に、流用の中身を引き継ぎ、使っている所も替える）。流用元を替えた行の中身の変更は消す。
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
            old = kept.get(str(r.get("key") or code))
            base = input_optional_code(r["base"], name="流用元") if "base" in r else old.base if old else None
            same = old is not None and old.base == base
            if base is not None and not same:
                if base == code:
                    raise ValueError(f"流用元が仮品番と同じです（{code}）")
                if not bom.children(base):
                    raise ValueError(f"流用元は、部品表にある組立品を入れてください（{base}）")
            temps.append(Temp(code, str(r.get("name") or "").strip(), material, base, old.lines if same and old else ()))
        renamed = {str(r["key"]): input_code(r["code"]) for r in rows
                   if str(r.get("code") or "").strip() and r.get("key") and str(r["key"]) in kept}
        renamed = {old: new for old, new in renamed.items() if old != new}
        if renamed:
            temps = [t._replace(lines=tuple((b, renamed.get(part, part), d) for b, part, d in t.lines)) for t in temps]
        with self._conn:
            self._rename_changes(sid, renamed)
            self._conn.execute("DELETE FROM v2_temp_item WHERE study_id = ?", (sid,))
            self._conn.execute("DELETE FROM v2_temp_line WHERE study_id = ?", (sid,))
            self._conn.executemany(
                "INSERT INTO v2_temp_item (study_id, position, code, name, material, base) VALUES (?, ?, ?, ?, ?, ?)",
                [(sid, i, t.code, t.name, _txt(t.material), t.base) for i, t in enumerate(temps)])
            for t in temps:
                self._put_temp_lines(sid, t.code, t.lines)
            self._touch(sid)

    def _rename_changes(self, sid: int, renamed: Mapping[str, str]) -> None:
        """変更に出てくる仮品番の品番を替える。同じ箇所に替えた後の品番の変更がもうあれば足す。"""
        if not renamed:
            return
        merged: dict[tuple[Any, ...], list[Any]] = {}
        rows = self._conn.execute("SELECT plan, product, parent, balloon, part, delta, source, origin, seq FROM v2_change "
                                  "WHERE study_id = ? ORDER BY seq", (sid,)).fetchall()
        for plan, product, parent, balloon, part, delta, *rest in rows:
            place = (plan, product, parent, balloon, renamed.get(part, part))
            if place in merged:
                merged[place][0] += Decimal(delta)
            else:
                merged[place] = [Decimal(delta), *rest]
        self._conn.execute("DELETE FROM v2_change WHERE study_id = ?", (sid,))
        self._conn.executemany("INSERT INTO v2_change VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               [(sid, *place, str(delta), *rest) for place, (delta, *rest) in merged.items() if delta != 0])

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

    def _put_temp_lines(self, sid: int, code: str, lines: Iterable[TempLine]) -> None:
        self._conn.executemany("INSERT INTO v2_temp_line VALUES (?, ?, ?, ?, ?)",
                               [(sid, code, _balloon_db(b), part, str(d)) for b, part, d in lines])

    def save_temp_lines(self, number: str, code: Any, rows: Sequence[Mapping[str, Any]]) -> None:
        """流用した仮品番の書き換えた直下（balloon・part・quantity）と流用元の差を、中の変更にする。"""
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
        after: dict[SlotKey, Decimal] = {}
        for r in rows:
            if not str(r.get("part") or "").strip():
                continue
            key = (input_optional_code(r.get("balloon"), name="風船"), input_code(r["part"]))
            qty = r.get("quantity")
            amount = Decimal(0) if qty is None or str(qty).strip() == "" else input_amount(qty)
            if amount < 0:
                raise ValueError(f"数量がマイナスです（{key[1]}）")
            after[key] = after.get(key, Decimal(0)) + amount
        there = {k[1] for k in now} | {part for _, part, _ in temp.lines}
        refuse_unregistered(bom, [k[1] for k in after if k[1] not in there and k[1] not in temps])
        lines = tuple((*k, after.get(k, Decimal(0)) - now.get(k, Decimal(0))) for k in dict.fromkeys([*now, *after]))
        lines = tuple(x for x in lines if x[2] != 0)
        temp_cycle(bom, {**temps, target: temp._replace(lines=lines)}, target)
        with self._conn:
            self._conn.execute("DELETE FROM v2_temp_line WHERE study_id = ? AND code = ?", (sid, target))
            self._put_temp_lines(sid, target, lines)
            self._touch(sid)

    def save_inside(self, ws: Workspace, plan: str, added: Sequence[Temp], lines: Mapping[str, Sequence[TempLine]],
                    picks: Sequence[tuple[str, str | None, str, str]]) -> None:
        """仮品番を足し、仮品番の中（lines）を入れ替え、直下（picks）を置き換える。"""
        sid = self.study_id(ws.number)
        with self._conn:
            at = self._conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM v2_temp_item WHERE study_id = ?", (sid,)).fetchone()[0]
            self._conn.executemany(
                "INSERT INTO v2_temp_item (study_id, position, code, name, material, base) VALUES (?, ?, ?, ?, ?, ?)",
                [(sid, at + i, t.code, t.name, _txt(t.material), t.base) for i, t in enumerate(added)])
            for code, rows in [*((t.code, t.lines) for t in added), *lines.items()]:
                self._conn.execute("DELETE FROM v2_temp_line WHERE study_id = ? AND code = ?", (sid, code))
                self._put_temp_lines(sid, code, rows)
            self._touch(sid)
            if picks:
                self._replace_at(ws, plan, picks)

    def drop_deep_changes(self, number: str, plan: str) -> int:
        """前に入れた2階層目の変更を消し、消した数を返す。"""
        sid = self.study_id(number)
        with self._conn:
            n = self._conn.execute("DELETE FROM v2_change WHERE study_id = ? AND plan = ? AND parent <> product",
                                   (sid, plan)).rowcount
            self._touch(sid)
        return n

    # 計算条件・版 -----------------------------------------------------------

    def save_conditions(self, number: str, annual: Mapping[str, Any]) -> None:
        sid = self.study_id(number)
        values = {input_code(product): str(input_amount(value, name="年間台数"))
                  for product, value in annual.items() if value is not None and str(value).strip() != ""}
        with self._conn:
            self._conn.execute("DELETE FROM v2_condition WHERE study_id = ?", (sid,))
            self._conn.executemany("INSERT INTO v2_condition VALUES (?, ?, ?)",
                                   [(sid, p, v) for p, v in values.items()])
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


def _rates_snapshot(rates: settings.Rates) -> list[str]:
    return [str(rates.adjustment), str(rates.indirect)]


def _snapshot(ws: Workspace) -> dict[str, Any]:
    bom = ws.bom
    codes: set[str] = set(ws.targets)
    for c in ws.changes:
        codes |= {c.parent, c.part}
    for t in ws.temps:
        codes |= {c for c in (t.code, t.base, *(part for _, part, _ in t.lines)) if c is not None}
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
        "replaces": ws.replaces,
        "changes": [[c.plan, c.product, c.parent, c.balloon, c.part, str(c.delta), c.source, c.origin, c.seq]
                    for c in ws.changes],
        "conditions": {k: str(v) for k, v in ws.conditions.items()},
        # 仮品番は bom の写しにも入る（変更に出る品番は写す）。画面で見せるために元の行も残す
        "temps": [[t.code, t.name, _txt(t.material), t.base, [[b, p, str(d)] for b, p, d in t.lines]] for t in ws.temps],
        "level1": ws.level1,
        "hold": ws.hold,
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
    return Workspace(
        number=data["header"]["number"],
        version=version,
        header=data["header"],
        targets=list(data["targets"]),
        main_product=data.get("main_product"),
        plans=[PlanInfo(p["plan"]) for p in data["plans"]],
        # 風船番号の列を足す前に固めた版は (元品番, 新品番) の2つ。全部の箇所として読む
        replaces={k: [(x[0], x[1], x[2] if len(x) > 2 else None) for x in v] for k, v in data["replaces"].items()},
        changes=[StoredChange(p, pr, pa, bl, pt, Decimal(d), s, o, seq) for p, pr, pa, bl, pt, d, s, o, seq
                 in data["changes"]],
        conditions={k: Decimal(v) for k, v in data["conditions"].items()},
        bom=bom,
        attributes=attributes,
        # 流用を足す前に固めた版は (品番, 名称, 材料費) の3つ
        temps=[Temp(c, nm, _dec(m), rest[0] if rest else None,
                    tuple((b, p, Decimal(d)) for b, p, d in rest[1]) if rest else ())
               for c, nm, m, *rest in data.get("temps", [])],
        level1=data.get("level1", False),
        hold=data.get("hold", False),
        temp_merged=set(data.get("temp_merged", [])),
        rates=None if rates is None else settings.Rates(Decimal(rates[0]), Decimal(rates[1])),
        owns={plan: {(p, h): Decimal(v) for p, h, v in rows} for plan, rows in data.get("owns", {}).items()},
    )


def replace_overridden(ws: Workspace, plan: str) -> dict[ReplaceRow, bool]:
    """置換の行ごとに、その行が効く箇所を 手で直した所があるか。

    置換の後に足した製品のように、まだ置換を当てていないだけの箇所には印を付けない。
    """
    stored = {c.place: c for c in ws.plan_changes(plan)}
    result = {}
    for old, new, balloon in ws.replaces.get(plan, []):
        hit = False
        for product in ws.targets:
            for line in replace_lines(ws.bom, product, old):
                if balloon is not None and balloon_group(line) != balloon:
                    continue
                for place in ((product, line.parent, line.balloon, old), (product, line.parent, line.balloon, new)):
                    c = stored.get(place)
                    if c is not None and c.source != REPLACE:
                        hit = True
        result[(old, new, balloon)] = hit
    return result
