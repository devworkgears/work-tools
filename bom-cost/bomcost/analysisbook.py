"""③の版と検討案を範囲に、ピボットで切れる縦長のブック（分析用）を作る。

シートはどれも Excel のテーブル（見出し1行）で、数は数値のセル・未登録は空のセル。
構成の「材料費」はその場所が製品の材料費小計に足す分（下位込みの登録値がある組立品の下は 0、
未登録の部品は空）で、製品ごとに足すと製品原価の材料費小計になる。「製造原価への寄与」はそれに
(1＋調整費の率)×(1＋間接費の率) を掛けた値。検討案の行は版の構成（土台 ×1）に変更の行（抜く分は負の数）を足したもの。
"""

import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .api import MASTER_HEADS, _View, version_label
from .comparebook import _UNSAFE
from .formula import ADJUSTMENT_RATE, INDIRECT_RATE, Head
from .group import _Structure
from .importer import load_openpyxl
from .model import CostHead, Version
from .rollup import Scope, ScopePolicy
from .scenario import ChangeKind, Scenario

if TYPE_CHECKING:
    from .group import Group

CURRENT = "現行"
FACTOR = (1 + ADJUSTMENT_RATE) * (1 + INDIRECT_RATE)

STRUCTURE = "構成"
PRODUCT_COST = "製品原価"
CHANGES = "変更"
ITEMS = "品目"
PRODUCTS = "製品"
TRIALS = "検討案"
PREMISES = "前提"

# シート → (テーブルの名前, 見出し)
SHEETS: dict[str, tuple[str, tuple[str, ...]]] = {
    STRUCTURE: ("Structure", ("検討案", "製品", "親品番", "部品", "階層", "員数", "1台あたりの数", "材料費の単価",
                              "材料費", "製造原価への寄与", "年間台数", "年間の金額")),
    PRODUCT_COST: ("ProductCost", ("検討案", "製品", "費目", "値", "年間の金額")),
    CHANGES: ("Changes", ("検討案", "製品", "種類", "場所", "元", "先", "値")),
    ITEMS: ("Items", ("品番", "名称", "記号・寸法", "区分", "子部品の有無", *(h.value for h in MASTER_HEADS))),
    PRODUCTS: ("Products", ("品番", "名称", "開発番号", "年間台数")),
    TRIALS: ("Trials", ("案名", "版", "変更の件数")),
    PREMISES: ("Premises", ("項目", "値")),
}


@dataclass(frozen=True)
class Place:
    """構成のシートの1行（製品1台あたり）。material は材料費小計に足す分（未登録は None）。"""

    parent: str
    part: str
    level: int
    quantity: Decimal
    usage: Decimal
    unit: Decimal | None
    material: Decimal | None


def analysis_file_name(version_text: str, names: list[str]) -> str:
    return _UNSAFE.sub("_", f"原価分析_{version_text}_{'・'.join(names)}") + ".xlsx"


def places(version: Version, policy: ScopePolicy, view: _View, structure: _Structure, product: str,
           entries: list[tuple[str, Decimal, bool, str | None]]) -> list[Place]:
    """製品の構成の場所ごとの行。entries は Scenario.root_entries の形（現行は製品 ×1 だけ）。

    材料費は計算の層の足し上げ（rollup）と同じ道で分ける: 登録値があって子が無いか下位込みの品目はその値で、
    下は 0。登録値が無い組立品は 0 で子へ。変更の行は場所の親の下に置き、その下もたどる。
    """
    usage = structure.usage(product)
    depth = _depths(structure, product)
    rows: list[Place] = []

    def walk(code: str, parent: str, level: int, quantity: Decimal, per_unit: Decimal, covered: bool,
             path: frozenset[str]) -> None:
        if code in path:
            raise ValueError(f"構成が循環している: {code}")
        own = version.cost_value(code, CostHead.MATERIAL)
        lines = version.children(code)
        whole = own is not None and (not lines or policy.scope_for(code, CostHead.MATERIAL) is Scope.INCLUDES_CHILDREN)
        if covered:
            material: Decimal | None = Decimal(0)
        elif own is not None:
            material = per_unit * own
        else:
            material = Decimal(0) if lines else None
        rows.append(Place(parent, code, level, quantity, per_unit, view.cost(code, CostHead.MATERIAL), material))
        for line in lines:
            walk(line.child, code, level + 1, line.quantity, per_unit * line.quantity, covered or whole, path | {code})

    for code, qty, is_base, parent in entries:
        if is_base:
            walk(product, "", 0, qty, qty, False, frozenset())
            continue
        at = parent or product
        through = Decimal(1) if at == product else usage.get(at, Decimal(0))
        walk(code, at, depth.get(at, 0) + 1, qty / through if through else qty, qty, False, frozenset())
    return rows


def _depths(structure: _Structure, product: str) -> dict[str, int]:
    """品目の、製品から一番浅い階層（製品は 0）。"""
    depth = {product: 0}
    queue = [product]
    while queue:
        code = queue.pop(0)
        for line in structure.children.get(code, ()):
            if line.child not in depth:
                depth[line.child] = depth[code] + 1
                queue.append(line.child)
    return depth


def _mul(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None else a * b


def _decimal(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _place_text(parent: str | None, balloon: str | None) -> str:
    if parent is None:
        return ""
    return parent if balloon is None else f"{parent} #{balloon}"


def analysis_book(group: "Group", params: dict[str, str], now: datetime) -> tuple[Version, list[str], bytes]:
    """compare/table と同じ問い合わせ（版・検討名・製品の絞り込み・償却費を無視）で、試算のある製品を範囲にしたブック。

    試算が無い検討案の製品は現行の構成と値で数える（③の分析と同じ）。
    """
    version, names, ignore, _, _, rows = group._compare_rows(params, with_changes=False)
    costs: dict[tuple[str, str], dict[str, str | None]] = {}
    bases: list[str] = []
    for row in rows:
        if row["kind"] == "base":
            bases.append(row["product"])
            costs[(CURRENT, row["product"])] = row["costs"]
        else:
            costs[(row["name"], row["product"])] = row["costs"]
    products = [c for c in bases if any((n, c) in costs for n in names)]
    store = group.store
    policy = store.load_scope_policy()
    scenario_ids = {n: group._scenarios(version, n) for n in names}
    scenarios: dict[tuple[str, str], Scenario] = {
        (n, c): store.load_scenario(scenario_ids[n][c], version) for n in names for c in products if c in scenario_ids[n]}
    annual = {c: None if (v := version.volume(c)) is None else v.annual for c in products}

    structure: list[list[Any]] = []
    product_cost: list[list[Any]] = []
    if products:
        view = _View(Scenario("", products[0], f"{products[0]}#", version, CostHead.MATERIAL, policy),
                     "bulk", CostHead.MATERIAL, False, ignore_amortization=ignore)
        tree = _Structure(version)
        for label in (CURRENT, *names):
            for code in products:
                scenario = scenarios.get((label, code))
                entries = scenario.root_entries() if scenario else [(code, Decimal(1), True, None)]
                for p in places(version, policy, view, tree, code, entries):
                    contribution = _mul(p.material, FACTOR)
                    structure.append([label, code, p.parent, p.part, p.level, p.quantity, p.usage, p.unit, p.material,
                                      contribution, annual[code], _mul(contribution, annual[code])])
                values = costs.get((label, code), costs[(CURRENT, code)])
                for h in Head:
                    value = _decimal(values[h.name])
                    product_cost.append([label, code, h.value, value, _mul(value, annual[code])])

    changes = []
    counts = {n: 0 for n in names}
    for (n, code), scenario in scenarios.items():
        counts[n] += len(scenario.history)
        for record in scenario.history:
            c = record.change
            source = Head[c.target_head].value if c.kind is ChangeKind.SET_HEAD and c.target_head else c.part
            changes.append([n, code, c.kind.value, _place_text(c.parent, c.balloon), source, c.replacement or "",
                            c.quantity])

    items = []
    codes = list(version.items) + [r.code for r in version.cost_rows if r.code not in version.items]
    for code in codes:
        item = version.items.get(code)
        items.append([code, "" if item is None else item.name, "" if item is None else item.specification,
                      "" if item is None else item.kind, "有" if version.children(code) else "無",
                      *(version.cost_value(code, h) for h in MASTER_HEADS)])

    sheets = {
        STRUCTURE: structure,
        PRODUCT_COST: product_cost,
        CHANGES: changes,
        ITEMS: items,
        PRODUCTS: [[c, group._name(version, c), version.dev_number, annual[c]] for c in products],
        TRIALS: [[n, version_label(version), counts[n]] for n in names],
        PREMISES: [
            ["版", version_label(version)],
            ["検討案", "・".join(names)],
            ["製品の絞り込み", (params.get("q") or "").strip() or "なし"],
            ["製品", len(products)],
            ["出した日時", now.strftime("%Y-%m-%d %H:%M")],
            ["調整費の率", ADJUSTMENT_RATE],
            ["間接費の率", INDIRECT_RATE],
            ["材料費から製造原価への係数", FACTOR],
            ["償却費", "無視した（0 として計算）" if ignore else "含めた"],
            ["構成の材料費", "材料費小計に足す分（下位込みの組立品の下は 0）"],
            ["試算の無い製品", "現行の構成と値"],
            ["値の扱い", "参考値"],
        ],
    }
    return version, names, _workbook(sheets)


def _workbook(sheets: dict[str, list[list[Any]]]) -> bytes:
    openpyxl = load_openpyxl()
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet, (table_name, headers) in SHEETS.items():
        ws = wb.create_sheet(sheet)
        ws.append(list(headers))
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in sheets[sheet]:
            ws.append(row)
        # テーブルは見出しだけだと Excel が直しに入るので、行が無ければ空の1行を含める
        last = max(ws.max_row, 2)
        table = Table(displayName=table_name, ref=f"A1:{get_column_letter(len(headers))}{last}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
        ws.add_table(table)
        ws.freeze_panes = "A2"
        for i, header in enumerate(headers):
            ws.column_dimensions[get_column_letter(i + 1)].width = max(10, len(header) * 2 + 2)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
