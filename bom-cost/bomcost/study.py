"""設計検討（/v2/）: 部品の差し替えを、対象の製品にまたがって案ごとに試算する。

検討が持つのは「製品×親×風船番号×品番の数量の増減」（変更）だけで、部品表は計算のたびに今の物を読む。
置換・グリッド画面・書き換えの3つの入力は同じ変更の集まりに書き、同じ箇所は後に書いた方が勝つ（足し算しない）。
版数取得の時だけ、そのときの検討と部品表（対象にかかる分）を写して固める。
"""

import json
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

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
    drawing TEXT NOT NULL DEFAULT ''
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
    grid_columns TEXT NOT NULL DEFAULT '[]',
    grid_rows TEXT NOT NULL DEFAULT '[]',
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
    PRIMARY KEY (study_id, position)
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
ATTRIBUTES: tuple[tuple[str, str], ...] = (("model", "代表"), ("product_spec", "規格"), ("drawing", "組立図"))
Attributes = dict[str, dict[str, str]]  # 品番 → 属性の名前 → 値（無い列は空文字）

REPLACE = "replace"
GRID = "grid"
REWRITE = "rewrite"

NOT_TARGET = "対象品番に無い"
NO_PLACE = "箇所が無い"
OTHER_PART = "元の品番が違う"
NEGATIVE = "数量がマイナス"
UNREGISTERED = "未登録"
PAIR_OUT = "置換の相手が外れた"
# 計算に入れない問題。未登録は入れて、結果に不完全の印を付ける
EXCLUDING = frozenset({NOT_TARGET, NO_PLACE, OTHER_PART, NEGATIVE, PAIR_OUT})
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
    grid_columns: list[str] = field(default_factory=list)
    grid_rows: list[str] = field(default_factory=list)


# 置換の行（元品番, 新品番, 風船番号）。風船番号 None は全部の箇所・"" は風船番号の無い箇所
ReplaceRow = tuple[str, str, str | None]
# 仮品番（品番, 名称, 材料費）。部品の原価は製品の材料費にだけ入るので、持つのは材料費だけ（None は未登録）
TempRow = tuple[str, str, Decimal | None]


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
    temps: list[TempRow] = field(default_factory=list)
    # 案 → (製品, 費目の名前 OWN_HEADS) → 書き換えた値
    owns: dict[str, dict[tuple[str, str], Decimal]] = field(default_factory=dict)
    # temps のうち部品表に混ぜた品番（品目の台帳に同じ品番がある仮品番は、台帳の方を使うので混ぜない）
    temp_merged: set[str] = field(default_factory=set)

    def attribute(self, code: str, name: str) -> str:
        return self.attributes.get(code, {}).get(name, "")

    def plan_ids(self) -> list[str]:
        return [p.plan for p in self.plans]

    def plan_changes(self, plan: str) -> list[StoredChange]:
        return [c for c in self.changes if c.plan == plan]


def merge_temps(bom: Version, temps: Sequence[TempRow]) -> set[str]:
    """仮品番のうち bom の品目に無い物を、品目と材料費の行として足す。足した品番を返す。"""
    merged = set()
    for code, name, material in temps:
        if code in bom.items:
            continue
        bom.add_item(Item(code, name))
        bom.add_cost(CostRow(code, {CostHead.MATERIAL: material}))
        merged.add(code)
    return merged


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
    return [line for line in product_lines(bom, product) if line.child == part]


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
    """製品たちの下の部品と、そのうち何製品で使うか。品番の順。"""
    used: dict[str, int] = {}
    for product in dict.fromkeys(products):
        for code in dict.fromkeys(line.child for line in product_lines(bom, product)):
            used[code] = used.get(code, 0) + 1
    return sorted(used.items())


def where_used(bom: Version, part: str, targets: Sequence[str]) -> tuple[list[str], list[str]]:
    """部品を使う製品を（範囲内, 範囲外）で。製品は一番上の品目（どこの子でもない）か対象品番。"""
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
    inside = [t for t in targets if t in ancestors]
    outside = sorted(a for a in ancestors if a not in target_set and a not in parents_of)
    return inside, outside


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


def calculate_heads(ws: Workspace, heads: Sequence[Head] = COMPARE_HEADS) -> dict[Head, dict[str, Any]]:
    """費目ごとに、対象品番ごとの今と案ごとの原価・年間の影響・台数の加重平均。DB は読むだけ（渡した Workspace だけを見る）。

    案の値＝（今の材料費小計＋部品の増減）と、製品自身の値（⑩で書き換えた費目はその値）から式で出す。
    台数の加重平均＝年間の影響 ÷ 年間の影響に入れた製品の年間台数の合計（1台あたりの増減）。台数の合計が 0 なら None。
    """
    bom = ws.bom
    calc = CostCalculator(bom, CostHead.MATERIAL)
    all_checked = check_changes(ws)
    checked = [c for c in all_checked if c.counted]
    # 計算から外した変更がある製品・案は、値に不完全の印を付ける
    dropped = {(c.change.plan, c.change.product) for c in all_checked if not c.counted}
    plans = ws.plan_ids()
    results: dict[Head, dict[str, Any]] = {
        h: {"head": h, "plans": plans, "rows": [], "impact": {p: Decimal(0) for p in plans},
            "impact_incomplete": {p: False for p in plans}, "counted": {p: Decimal(0) for p in plans}}
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
        now_b = None if now_material is None else breakdown(now_material, own)
        annual = ws.conditions.get(product)
        if annual is None and bom.volume(product) is not None:
            annual = bom.volume(product).annual
        usage = product_usage(bom, product)
        after_b: dict[str, tuple[Breakdown | None, bool]] = {}
        for plan in plans:
            delta = Decimal(0)
            missing = False
            for c in checked:
                if c.change.plan != plan or c.change.product != product:
                    continue
                unit = calc.item_cost(c.change.part)
                if unit is None:
                    missing = True
                    continue
                delta += c.change.delta * usage.get(c.change.parent, Decimal(0)) * unit
            b = None if now_material is None else breakdown(now_material + delta, own_values(ws, plan, product, own))
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
            # 社内加工費は台帳に無ければ 0 で計算する（未登録の印を付けない）ので、今の値は台帳の値のまま見せる
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
        for name, _ in ATTRIBUTES:
            if name not in present:
                conn.execute(f"ALTER TABLE v2_item ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        # 置換の組に風船番号の列を足す前に作った表には列を足す（NULL＝全部の箇所）
        if "balloon" not in {r[1] for r in conn.execute("PRAGMA table_info(v2_replace)")}:
            conn.execute("ALTER TABLE v2_replace ADD COLUMN balloon TEXT")
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
                    f"INSERT INTO v2_item (code, name, kind, specification, {', '.join(n for n, _ in ATTRIBUTES)}) "
                    f"VALUES ({', '.join('?' * (4 + len(ATTRIBUTES)))})",
                    [(i.code, i.name, i.kind, i.specification,
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

    def load_bom(self) -> Version:
        v = Version("v2")
        q = self._conn.execute
        for code, name, kind, spec in q("SELECT code, name, kind, specification FROM v2_item ORDER BY rowid"):
            v.add_item(Item(code, name, kind, specification=spec))
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
        plans = [PlanInfo(p, json.loads(c), json.loads(r)) for p, c, r in q(
            "SELECT plan, grid_columns, grid_rows FROM v2_plan WHERE study_id = ? ORDER BY plan", (sid,))]
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
        temps: list[TempRow] = [(c, nm, _dec(m)) for c, nm, m in q(
            "SELECT code, name, material FROM v2_temp_item WHERE study_id = ? ORDER BY position", (sid,))]
        bom = self.load_bom()
        merged = merge_temps(bom, temps)
        return Workspace(n, None, header, targets, main, plans or [PlanInfo("A")], replaces, changes, conditions,
                         bom, self.load_attributes(), temps=temps, temp_merged=merged, owns=owns)

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

    def save_targets(self, number: str, products: Sequence[Any], main_product: Any = None) -> None:
        sid = self.study_id(number)
        codes = list(dict.fromkeys(input_code(p, name="対象品番") for p in products
                                   if p is not None and str(p).strip()))
        main = input_optional_code(main_product, name="主対象")
        if main is not None and main not in codes:
            main = None
        with self._conn:
            self._conn.execute("DELETE FROM v2_target WHERE study_id = ?", (sid,))
            self._conn.executemany("INSERT INTO v2_target VALUES (?, ?, ?)",
                                   [(sid, i, code) for i, code in enumerate(codes)])
            self._conn.execute("UPDATE v2_study SET main_product = ? WHERE study_id = ?", (main, sid))
            self._touch(sid)

    # ④ 置換 -----------------------------------------------------------------

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

    # ⑤ グリッド画面 ---------------------------------------------------------------

    def save_grid(self, number: str, plan: str, columns: Sequence[Any], rows: Sequence[Any],
                cells: Sequence[Mapping[str, Any]]) -> None:
        """グリッド画面の見出し（親品番）・行（品番）と、書き換えたセル（product・part・value・row）を保存する。

        セルの値はその製品でのその品番の増減。今ある箇所が分かればそこへ、無ければ同じ列の1つ上の行で
        この回に書いたマイナスの箇所（置き換えの相手）へ、それも無ければ製品の直下に置く。
        製品の中で箇所が2つ以上ある品番のセルは、1つに潰れるので受けない（書き換えで直す）。
        """
        ws = self.workspace(number)
        sid = self.study_id(number)
        col_codes = list(dict.fromkeys(input_code(c, name="親品番") for c in columns if c is not None and str(c).strip()))
        row_codes = list(dict.fromkeys(input_code(r, name="品番") for r in rows if r is not None and str(r).strip()))
        parsed = []
        for cell in cells:
            value = cell.get("value")
            amount = None if value is None or str(value).strip() == "" else input_amount(value)
            product, part = input_code(cell.get("product"), name="親品番"), input_code(cell.get("part"))
            if grid_locked(ws, plan, product, part):
                raise ValueError(f"{part} は {product} の中で複数の箇所にあります。書き換えで直してください")
            row = cell.get("row") or 0
            if not isinstance(row, int) or isinstance(row, bool):
                raise ValueError("送る形が違います（行の番号）")
            parsed.append((product, part, amount, row))
        # マイナスを先に書き、プラスの置き場の相手にする
        parsed.sort(key=lambda p: (p[2] is None or p[2] >= 0, p[3]))
        with self._conn:
            self._ensure_plan(sid, plan)
            self._conn.execute("UPDATE v2_plan SET grid_columns = ?, grid_rows = ? WHERE study_id = ? AND plan = ?",
                               (json.dumps(col_codes, ensure_ascii=False), json.dumps(row_codes, ensure_ascii=False),
                                sid, plan))
            current = {c.place: c for c in ws.plan_changes(plan)}
            written: list[tuple[int, Place]] = []  # (行, この回に書いたマイナスの箇所)
            for product, part, amount, row in parsed:
                mine = [c for c in current.values() if c.product == product and c.part == part]
                if amount is None or amount == 0:
                    for c in mine:
                        self._delete(sid, plan, c.place)
                        current.pop(c.place, None)
                    continue
                if mine:
                    place = mine[0].place
                else:
                    place = self._grid_place(ws, product, part, amount, row, written)
                self._put(sid, plan, place, amount, GRID)
                current[place] = StoredChange(plan, *place, amount, GRID, None, 1 << 62)
                if amount < 0:
                    written.append((row, place))
            self._touch(sid)

    @staticmethod
    def _grid_place(ws: Workspace, product: str, part: str, amount: Decimal, row: int,
                  written: list[tuple[int, Place]]) -> Place:
        found = occurrences(ws.bom, product, part)
        if found:
            return (product, found[0].parent, found[0].balloon, part)
        if amount > 0:
            pair = [p for r, p in written if p[0] == product and r == row - 1]
            if pair:
                return (product, pair[0][1], pair[0][2], part)
        return (product, product, None, part)

    # ⑧ 書き換え -------------------------------------------------------------

    def save_rewrite(self, number: str, plan: str, product: Any, rows: Sequence[Mapping[str, Any]]) -> None:
        """1製品の書き換えた全構成（parent・balloon・part・quantity）と今の部品表の差を変更にする。"""
        ws = self.workspace(number)
        sid = self.study_id(number)
        product = input_code(product, name="対象品番")
        before: dict[tuple[str, str | None, str], Decimal] = {}
        for line in product_lines(ws.bom, product):
            before[line.key] = before.get(line.key, Decimal(0)) + line.quantity
        after: dict[tuple[str, str | None, str], Decimal] = {}
        for r in rows:
            if not str(r.get("part") or "").strip():
                continue
            key = (input_code(r.get("parent") or "", name="親"), input_optional_code(r.get("balloon"), name="風船"),
                   input_code(r["part"]))
            qty = r.get("quantity")
            amount = Decimal(0) if qty is None or str(qty).strip() == "" else input_amount(qty)
            after[key] = after.get(key, Decimal(0)) + amount
        current = {c.place: c for c in ws.plan_changes(plan) if c.product == product}
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

    # ⑩ 製品の費目 -----------------------------------------------------------

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
        """仮品番の行（code・name・material）を入れ替える。品番の無い行は飛ばし、同じ品番が2行あれば ValueError。"""
        sid = self.study_id(number)
        temps: list[TempRow] = []
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
            temps.append((code, str(r.get("name") or "").strip(), material))
        with self._conn:
            self._conn.execute("DELETE FROM v2_temp_item WHERE study_id = ?", (sid,))
            self._conn.executemany("INSERT INTO v2_temp_item VALUES (?, ?, ?, ?, ?)",
                                   [(sid, i, c, nm, _txt(m)) for i, (c, nm, m) in enumerate(temps)])
            self._touch(sid)

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


def _snapshot(ws: Workspace) -> dict[str, Any]:
    bom = ws.bom
    codes: set[str] = set(ws.targets)
    for c in ws.changes:
        codes |= {c.parent, c.part}
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
        "plans": [{"plan": p.plan, "grid_columns": p.grid_columns, "grid_rows": p.grid_rows} for p in ws.plans],
        "replaces": ws.replaces,
        "changes": [[c.plan, c.product, c.parent, c.balloon, c.part, str(c.delta), c.source, c.origin, c.seq]
                    for c in ws.changes],
        "conditions": {k: str(v) for k, v in ws.conditions.items()},
        # 仮品番は bom の写しにも入る（変更に出る品番は写す）。画面で見せるために元の行も残す
        "temps": [[c, nm, _txt(m)] for c, nm, m in ws.temps],
        "temp_merged": sorted(ws.temp_merged),
        "owns": {plan: [[p, h, str(v)] for (p, h), v in values.items()] for plan, values in ws.owns.items()},
        "bom": {
            "items": [[i.code, i.name, i.kind, i.specification, *(ws.attribute(i.code, n) for n, _ in ATTRIBUTES)]
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
        bom.add_item(Item(code, name, kind, specification=spec))
        attributes[code] = {n: (extra[i] if i < len(extra) else "") for i, (n, _) in enumerate(ATTRIBUTES)}
    bom.add_bom_lines(BomLine(p, bl, ch, Decimal(q)) for p, bl, ch, q in b["lines"])
    for row in b["costs"]:
        bom.add_cost(CostRow(row[0], {h: _dec(t) for (_, h), t in zip(COST_COLUMNS, row[1:])}))
    for product, annual in b["volumes"]:
        bom.add_volume(Volume(product, Decimal(annual)))
    return Workspace(
        number=data["header"]["number"],
        version=version,
        header=data["header"],
        targets=list(data["targets"]),
        main_product=data.get("main_product"),
        plans=[PlanInfo(p["plan"], p["grid_columns"], p["grid_rows"]) for p in data["plans"]],
        # 風船番号の列を足す前に固めた版は (元品番, 新品番) の2つ。全部の箇所として読む
        replaces={k: [(x[0], x[1], x[2] if len(x) > 2 else None) for x in v] for k, v in data["replaces"].items()},
        changes=[StoredChange(p, pr, pa, bl, pt, Decimal(d), s, o, seq) for p, pr, pa, bl, pt, d, s, o, seq
                 in data["changes"]],
        conditions={k: Decimal(v) for k, v in data["conditions"].items()},
        bom=bom,
        attributes=attributes,
        temps=[(c, nm, _dec(m)) for c, nm, m in data.get("temps", [])],
        temp_merged=set(data.get("temp_merged", [])),
        owns={plan: {(p, h): Decimal(v) for p, h, v in rows} for plan, rows in data.get("owns", {}).items()},
    )


def replace_overridden(ws: Workspace, plan: str) -> dict[ReplaceRow, bool]:
    """置換の行ごとに、その行が効く箇所を グリッド画面・書き換えで手で直した所があるか。

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


def grid_places(ws: Workspace, plan: str, product: str, part: str) -> set[tuple[str, str | None]]:
    """製品の中でその品番がある箇所（親・風船番号）。部品表の行と、案の変更の両方から。"""
    places = {(l.parent, l.balloon) for l in occurrences(ws.bom, product, part)}
    places |= {(c.parent, c.balloon) for c in ws.plan_changes(plan) if c.product == product and c.part == part}
    return places


def grid_locked(ws: Workspace, plan: str, product: str, part: str) -> bool:
    """グリッド画面のセルは製品×品番の合計なので、箇所が2つ以上あると直した値をどこに書くか決まらない。"""
    return len(grid_places(ws, plan, product, part)) > 1
