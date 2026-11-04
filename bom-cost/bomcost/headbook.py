"""②の費目（製品×社内加工費・償却費・直接費）を Excel のブックで書き出し、打ち替えたブックを取り込む。

流れはマスタ登録のブックと同じ（書き出す → 打ち込む → 確認 → 決定）。確認（plan_heads）は何も保存せず、
決定は変わった値だけを②の費目変更（/api/group/heads/bulk と同じ記録）で入れる。値は検討案の変更後の値。
"""

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .formula import EDITABLE_HEADS, Head
from .importer import load_openpyxl
from .masterbook import BookError, _read_sheets, _text
from .normalize import to_code, to_decimal

SHEET = "費目"
PREMISES = "前提"
CODE = "製品の品番"
NAME = "名称"
HEADERS: tuple[str, ...] = (CODE, NAME, *(h.value for h in EDITABLE_HEADS))
TRIAL_KEY = "検討案"
_UNSAFE = re.compile(r'[\\/:*?"<>|\s\x00-\x1f]')


@dataclass(frozen=True)
class HeadValues:
    """ブックの1行に出す製品と、費目ごとの今の変更後の値（None は未登録）。"""

    product: str
    name: str
    values: dict[Head, Decimal | None]


@dataclass(frozen=True)
class HeadChange:
    row: int
    product: str
    head: Head
    current: Decimal | None
    value: Decimal


@dataclass
class HeadPlan:
    """ブックと今の変更後の値の差。errors が空のときだけ決定できる。"""

    changes: list[HeadChange] = field(default_factory=list)
    unchanged: int = 0  # 値が1つも変わっていない製品の行
    errors: list[BookError] = field(default_factory=list)

    def items(self) -> list[dict[str, Any]]:
        """/api/group/heads/bulk の items の形（製品ごとに変わった費目だけ）。"""
        by_product: dict[str, dict[str, str]] = {}
        for c in self.changes:
            by_product.setdefault(c.product, {})[c.head.name] = format(c.value, "f")
        return [{"product": p, "values": v} for p, v in by_product.items()]


def book_name(stem: str, version_text: str, name: str) -> str:
    """置き場のファイル名。版と検討案ごとに1つ（ファイル名に使えない文字は _ に）。"""
    return _UNSAFE.sub("_", f"{stem}-heads-{version_text}-{name}") + ".xlsx"


def heads_book_bytes(rows: list[HeadValues], version_text: str, name: str, now: datetime) -> bytes:
    """費目のシートと前提のシートのブック。未登録は空のセル。"""
    openpyxl = load_openpyxl()
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append(list(HEADERS))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in rows:
        ws.append([r.product, r.name, *(r.values.get(h) for h in EDITABLE_HEADS)])
    ws.freeze_panes = "C2"
    for i, header in enumerate(HEADERS):
        ws.column_dimensions[ws.cell(1, i + 1).column_letter].width = max(12, len(header) * 2 + 2)
    premises = wb.create_sheet(PREMISES)
    for key, value in (("版", version_text), (TRIAL_KEY, name), ("値", "変更後（空は未登録）"),
                       ("書き出した日時", now.strftime("%Y-%m-%d %H:%M"))):
        premises.append([key, value])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def plan_heads(data: bytes, name: str, current: dict[str, HeadValues]) -> HeadPlan:
    """ブックを読み、current（検討案の試算がある製品 → 今の変更後の値）との差と行の誤りを返す。何も保存しない。

    空のセルは、今が未登録なら変えない。登録のある値を空にした行は誤り（費目変更で未登録には戻せない）。
    読めないブックは ValueError 以外の例外（zipfile など）も出るので、呼ぶ側でまとめて受ける。
    """
    sheets = _read_sheets(data)
    plan = HeadPlan()
    premises = {_text(r[0]): _text(r[1]) for r in sheets.get(PREMISES, []) if len(r) >= 2}
    if premises.get(TRIAL_KEY, name) != name:
        plan.errors.append(BookError(PREMISES, None, f"検討案 {premises[TRIAL_KEY]} のブック（開いているのは {name}）"))
        return plan
    if SHEET not in sheets:
        plan.errors.append(BookError(SHEET, None, "シートが無い"))
        return plan
    rows = sheets[SHEET]
    head = [_text(v) for v in rows[0]] if rows else []
    missing = [h for h in HEADERS if h != NAME and h not in head]
    if missing:
        plan.errors.append(BookError(SHEET, 1, f"列が無い: {'・'.join(missing)}"))
        return plan
    index = {h: head.index(h) for h in HEADERS if h in head}
    seen: dict[str, int] = {}
    for number, raw in enumerate(rows[1:], start=2):
        values = {h: (raw[i] if i < len(raw) else None) for h, i in index.items()}
        if all(_text(v) == "" for v in values.values()):
            continue
        try:
            changes = _plan_row(values, name, current, seen, number)
        except (ValueError, TypeError) as exc:
            plan.errors.append(BookError(SHEET, number, str(exc)))
            continue
        if changes:
            plan.changes.extend(changes)
        else:
            plan.unchanged += 1
    return plan


def _plan_row(values: dict[str, Any], name: str, current: dict[str, HeadValues], seen: dict[str, int],
              number: int) -> list[HeadChange]:
    if _text(values[CODE]) == "":
        raise ValueError(f"{CODE}が空")
    product = to_code(values[CODE], name=CODE)
    if product in seen:
        raise ValueError(f"製品 {product} は {seen[product]} 行目にもある")
    seen[product] = number
    now = current.get(product)
    if now is None:
        raise ValueError(f"製品 {product} は検討案 {name} の試算に無い")
    changes = []
    for h in EDITABLE_HEADS:
        cell = values[h.value]
        old = now.values.get(h)
        if _text(cell) == "":
            if old is not None:
                raise ValueError(f"{h.value}が空（費目変更で未登録には戻せない）")
            continue
        value = to_decimal(cell, name=h.value)
        if value < 0:
            raise ValueError(f"{h.value}は 0 以上にする")
        if old is None or value != old:
            changes.append(HeadChange(number, product, h, old, value))
    return changes


def plan_json(plan: HeadPlan, current: dict[str, HeadValues]) -> dict[str, Any]:
    return {
        "products": len({c.product for c in plan.changes}),
        "changed": len(plan.changes),
        "unchanged": plan.unchanged,
        "changes": [{"row": c.row, "product": c.product, "product_name": current[c.product].name,
                     "head": c.head.name, "head_label": c.head.value,
                     "current": None if c.current is None else format(c.current, "f"), "value": format(c.value, "f")}
                    for c in plan.changes],
        "errors": [{"sheet": e.sheet, "row": e.row, "message": e.message} for e in plan.errors],
    }
