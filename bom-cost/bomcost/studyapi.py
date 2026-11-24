"""設計検討（/v2/）の API。検討は検討番号（number）と版（version: 空なら仮）で指す。"""

import io
import json
import unicodedata
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

from .formula import breakdown, registered_own
from .importer import (
    TABLE_FIELDS,
    ColumnMap,
    MissingColumnsError,
    Table,
    VersionImport,
    guess_columns,
    load_openpyxl,
    read_csv,
)
from .model import CostHead, Version
from .normalize import to_code
from .rollup import CostCalculator
from .study import (
    ATTRIBUTES,
    HEADS,
    OWN_HEADS,
    NO_PLACE,
    OTHER_PART,
    Attributes,
    REPLACE,
    StudyStore,
    Workspace,
    check_changes,
    calculate,
    calculate_heads,
    own_rows,
    after_parts,
    grid_balloon,
    grid_locked,
    input_code,
    input_optional_code,
    nfkc,
    occurrences,
    pair_key,
    parts_of,
    problem_count,
    product_lines,
    replace_groups,
    replace_overridden,
    unregistered_parts,
    where_used,
)

if TYPE_CHECKING:
    from .api import Api, Response

# 設計検討の見本（製品をまたぐ差し替えを試せる大きさ）。パッケージの外に置き、配る zip には入れない
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples_v2"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
LABELS_NAME = "labels.json"


def _balloons(ws: Workspace, plan: str, columns: list[str], part: str) -> dict[str, str]:
    """列の製品ごとの、その品番の風船番号（その製品に無ければ入れない）。"""
    out = {}
    for product in columns:
        balloon = grid_balloon(ws, plan, product, part)
        if balloon is not None:
            out[product] = balloon
    return out

def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _version(params: dict[str, str]) -> int | None:
    text = params.get("version", "").strip()
    if text in ("", "仮"):
        return None
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"版は数字で入力してください（{text}）")
    return int(text)


def _balloon_order(balloon: str | None) -> tuple[int, int, str]:
    """風船番号の並べ方: 数字は数の順、ほかは文字の順、空欄は最後。"""
    if balloon is None:
        return (2, 0, "")
    if balloon.isascii() and balloon.isdigit():
        return (0, int(balloon), "")
    return (1, 0, balloon)


def _list(body: dict[str, Any], key: str) -> list[Any]:
    value = body.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"送る形が違います（{key} は配列）")
    return value


def _objects(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """要素がオブジェクトの配列。形が違えば ValueError（400）。"""
    items = _list(body, key)
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"送る形が違います（{key} の {i}番目）")
    return items


def _number(params: dict[str, str]) -> str:
    number = params.get("number", "").strip()
    if not number:
        raise ValueError("検討番号を入力してください")
    return number


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    return "" if value is None else str(value).strip()


def table_kind(table: Table) -> ColumnMap:
    """見出しから表の種類（品目・構成・原価・年間台数）を当て、列の対応を作る。"""
    best: tuple[int, str, dict[str, str]] | None = None
    for kind in TABLE_FIELDS:
        columns, score = guess_columns(kind, table.headers)
        try:
            ColumnMap(kind, table.headers, columns)
        except MissingColumnsError:
            continue
        if best is None or score > best[0]:
            best = (score, kind, columns)
    if best is None:
        raise ValueError(f"見出しから表の種類を判別できません（{table.name}）")
    return ColumnMap(best[1], table.headers, best[2])


def _fold(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def load_labels(db_path: Path | None) -> dict[str, str]:
    """語の読み替え（画面と取り込みの見出しの語 → 使う語）。DB と同じフォルダの labels.json に書く。無ければ空。"""
    path = None if db_path is None else db_path.parent / LABELS_NAME
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{LABELS_NAME} を読めません（{e}）") from None
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ValueError(f"{LABELS_NAME} の形が違います（語 → 使う語 の対応を書く）")
    return data


def item_attributes(table: Table, column_map: ColumnMap, labels: dict[str, str] | None = None) -> Attributes:
    """部品マスタの任意の列（代表・規格・組立図）を品番ごとに。無い列は空文字。

    labels に読み替えがある見出しは、読み替えた語の列を先に探し、無ければ元の語の列を使う。
    """
    headers = [_fold(h) for h in table.headers]
    code_at = table.headers.index(column_map.columns["code"])
    at: dict[str, int | None] = {}
    for name, label in ATTRIBUTES:
        wanted = [_fold(x) for x in ((labels or {}).get(label), label) if x]
        at[name] = next((headers.index(w) for w in wanted if w in headers), None)
    result: Attributes = {}
    for row in table.rows:
        cell = row[code_at] if code_at < len(row) else None
        if cell is None or str(cell).strip() == "":
            continue
        try:
            code = to_code(cell)
        except (TypeError, ValueError):
            continue
        result[code] = {name: "" if i is None or i >= len(row) or row[i] is None else str(row[i]).strip()
                        for name, i in at.items()}
    return result


def import_tables(tables: list[Table], labels: dict[str, str] | None = None,
                  maps: list[ColumnMap | None] | None = None) -> tuple[Version, list[str], list[str], Attributes]:
    """表を取り込んだ版・入った表の種類・警告の文・品目の属性。

    maps は表ごとの列の対応（tables と同じ並び）。無い表は見出しから表の種類を当てる。
    """
    work = VersionImport(Version("v2", datetime.now().replace(microsecond=0)))
    kinds: list[str] = []
    attributes: Attributes = {}
    for i, table in enumerate(tables):
        column_map = (maps[i] if maps else None) or table_kind(table)
        work.add_table(table, column_map)
        kinds.append(column_map.table)
        if column_map.table == "item":
            attributes.update(item_attributes(table, column_map, labels))
    warnings = [f"{w.file}{'' if w.row is None else f' {w.row}行'}: {w.message}" for w in work.warnings]
    return work.version, kinds, warnings, attributes


class StudyApi:
    def __init__(self, api: "Api") -> None:
        self.api = api
        self._store: StudyStore | None = None

    @property
    def store(self) -> StudyStore:
        # 表は最初の要求で作る。Api は DB の接続と別のスレッドで作られ、sqlite3 の接続は作ったスレッドでしか使えない
        if self._store is None:
            self._store = StudyStore(self.api.store.connection)
        return self._store

    def routes(self) -> list[tuple[str, str, Callable[..., "Response"]]]:
        return [
            ("GET", r"/api/v2/labels", self.labels),
            ("GET", r"/api/v2/bom", self.bom_status),
            ("POST", r"/api/v2/bom/import", self.bom_import),
            ("POST", r"/api/v2/lookup", self.lookup),
            ("GET", r"/api/v2/products", self.products),
            ("GET", r"/api/v2/studies", self.studies),
            ("POST", r"/api/v2/studies", self.create_study),
            ("GET", r"/api/v2/study", self.study),
            ("PUT", r"/api/v2/study", self.put_study),
            ("GET", r"/api/v2/targets", self.targets),
            ("PUT", r"/api/v2/targets", self.put_targets),
            ("PUT", r"/api/v2/main-product", self.put_main),
            ("GET", r"/api/v2/after-parts", self.after_parts),
            ("POST", r"/api/v2/replace/apply", self.apply_replace),
            ("POST", r"/api/v2/replace/revert", self.revert_replace),
            ("GET", r"/api/v2/replace", self.replace),
            ("GET", r"/api/v2/replace/groups", self.replace_groups),
            ("PUT", r"/api/v2/replace", self.put_replace),
            ("GET", r"/api/v2/grid", self.grid),
            ("PUT", r"/api/v2/grid", self.put_grid),
            ("POST", r"/api/v2/grid/locked", self.locked_cells),
            ("POST", r"/api/v2/plans", self.add_plan),
            ("GET", r"/api/v2/rewrite", self.rewrite),
            ("PUT", r"/api/v2/rewrite", self.put_rewrite),
            ("GET", r"/api/v2/changes", self.changes),
            ("GET", r"/api/v2/own", self.own),
            ("PUT", r"/api/v2/own", self.put_own),
            ("GET", r"/api/v2/temp-items", self.temp_items),
            ("PUT", r"/api/v2/temp-items", self.put_temp_items),
            ("GET", r"/api/v2/conditions", self.conditions),
            ("PUT", r"/api/v2/conditions", self.put_conditions),
            ("GET", r"/api/v2/calc", self.calc),
            ("GET", r"/api/v2/where-used", self.where_used),
            ("GET", r"/api/v2/parts", self.parts),
            ("POST", r"/api/v2/release", self.release),
            ("GET", r"/api/v2/export/changes", self.export_changes),
            ("GET", r"/api/v2/export/result", self.export_result),
        ]

    def _json(self, data: Any) -> "Response":
        from .api import _json_response

        return _json_response(data)

    def _ws(self, params: dict[str, str]) -> Workspace:
        return self.store.workspace(_number(params), _version(params))

    @staticmethod
    def _plan(params: dict[str, Any]) -> str:
        return str(params.get("plan") or "A").strip()

    # 部品表 -----------------------------------------------------------------

    def labels(self, params: dict[str, str]) -> "Response":
        return self._json({"labels": load_labels(self.api.store.path)})

    def bom_status(self, params: dict[str, str]) -> "Response":
        return self._json({"counts": self.store.bom_counts(), "samples": SAMPLES_DIR.is_dir()})

    def bom_import(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """files（filename・content の base64）か、sample: true で見本の CSV を取り込む。入った表だけ入れ替える。

        files の1件に table と columns（項目 → 見出し）があればその対応で読み、決定の時に対応を保存する。無ければ見出しから
        表の種類を当てる。sheet は Excel のシート名。check: true は読んだ件数と警告を返すだけで、何も保存しない。
        """
        from .screens import Screens

        tables: list[Table] = []
        maps: list[ColumnMap | None] = []
        if body.get("sample"):
            if not SAMPLES_DIR.is_dir():
                raise ValueError("見本のデータがありません")
            tables = [read_csv(p) for p in sorted(SAMPLES_DIR.glob("*.csv"))]
        else:
            files = body.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError("取り込むファイルを選んでください")
            screens = Screens(self.api)
            for f in files:
                table, _, _ = screens._read_upload(f)
                tables.append(table)
                maps.append(screens._column_map(f, table) if f.get("columns") is not None else None)
        version, kinds, warnings, attributes = import_tables(tables, load_labels(self.api.store.path), maps)
        if body.get("check"):
            read = {"item": len(version.items), "bom_line": len(version.lines), "cost": len(version.cost_rows),
                    "volume": len(version.volumes)}
            return self._json({"tables": kinds, "warnings": warnings, "counts": {k: read[k] for k in dict.fromkeys(kinds)}})
        self.store.replace_bom(version, kinds, attributes)
        for column_map in maps:
            if column_map is not None:
                self.api.store.save_column_map(column_map)
        return self._json({"tables": kinds, "warnings": warnings, "counts": self.store.bom_counts()})

    def lookup(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """品番の右の列（名称・代表・規格・組立図・年間台数・材料費・製造原価）。無い品番は registered: false。

        number があればその検討の部品表（版の写し・仮品番を混ぜた物）で引き、仮品番は temp: true。
        """
        temp: set[str] = set()
        if params.get("number"):
            ws = self._ws(params)
            bom, attributes, temp = ws.bom, ws.attributes, ws.temp_merged
        else:
            bom, attributes = self.store.load_bom(), self.store.load_attributes()
        codes = [nfkc(str(c)).strip() for c in _list(body, "codes") if c is not None and str(c).strip()]
        calc = CostCalculator(bom, CostHead.MATERIAL)
        result = {}
        for code in dict.fromkeys(codes):
            item = bom.items.get(code)
            if item is None:
                result[code] = {"registered": False}
                continue
            try:
                material = calc.item_cost(code)
            except ValueError:
                material = None
            volume = bom.volume(code)
            result[code] = {
                "registered": True,
                "temp": code in temp,
                "name": item.name,
                "specification": item.specification,
                "kind": item.kind,
                **{name: attributes.get(code, {}).get(name, "") for name, _ in ATTRIBUTES},
                "annual": None if volume is None else _s(volume.annual),
                "material": _s(material),
                "manufacturing": _s(breakdown(material, registered_own(bom, code)).value(CostHead.MANUFACTURING)
                                    if material is not None else None),
            }
        return self._json({"items": result})

    def products(self, params: dict[str, str]) -> "Response":
        """品番検索: 製品（どこの子でもない親か、年間台数のある品目）のうち、品番・名称・代表に語を含み、規格が合う物。

        product_specs は製品にある規格の一覧（条件の選択肢）。
        """
        bom = self.store.load_bom()
        attributes = self.store.load_attributes()
        words = {key: params.get(key, "").strip().casefold() for key in ("code", "name", "model")}
        spec = params.get("product_spec", "").strip()
        children = {line.child for line in bom.lines}
        products = {p for p in bom.parents() if p not in children} | set(bom.volumes)
        rows = []
        specs: dict[str, None] = {}
        for item in bom.items.values():
            if item.code not in products:
                continue
            attrs = {name: attributes.get(item.code, {}).get(name, "") for name, _ in ATTRIBUTES}
            if attrs["product_spec"]:
                specs[attrs["product_spec"]] = None
            texts = {"code": item.code, "name": item.name, "model": attrs["model"]}
            if any(word and word not in texts[key].casefold() for key, word in words.items()):
                continue
            if spec and attrs["product_spec"] != spec:
                continue
            volume = bom.volume(item.code)
            rows.append({"code": item.code, "name": item.name, **attrs,
                         "annual": None if volume is None else _s(volume.annual)})
        return self._json({"products": rows[:500], "product_specs": list(specs)})

    # 検討 -------------------------------------------------------------------

    def studies(self, params: dict[str, str]) -> "Response":
        return self._json({"studies": self.store.list_studies()})

    def create_study(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """products があれば対象品番も入れて作る。品番の形が違えば検討を作らずに止める。"""
        products = body.get("products") or []
        if not isinstance(products, list):
            raise ValueError("送る形が違います（products は配列）")
        codes = [input_code(p, name="対象品番") for p in products if p is not None and str(p).strip()]
        input_optional_code(body.get("main_product"), name="主対象")
        number = self.store.create_study(_text(body, "title"), _text(body, "owner"), _text(body, "approver"),
                                         _text(body, "dev_number"))
        if codes:
            self.store.save_targets(number, codes, body.get("main_product"))
        return self._study_json(self.store.workspace(number))

    def study(self, params: dict[str, str]) -> "Response":
        return self._study_json(self._ws(params))

    def put_study(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        number = _number(params)
        self.store.update_header(number, _text(body, "title"), _text(body, "owner"), _text(body, "approver"),
                                 _text(body, "dev_number"))
        return self._study_json(self.store.workspace(number))

    def _study_json(self, ws: Workspace) -> "Response":
        return self._json({
            "header": ws.header,
            "version": ws.version,
            "versions": self.store.versions(ws.number),
            "plans": ws.plan_ids(),
            "targets": ws.targets,
            "problems": problem_count(ws),
        })

    # ③ 対象品番 -------------------------------------------------------------

    def targets(self, params: dict[str, str]) -> "Response":
        ws = self._ws(params)
        return self._json({"targets": ws.targets, "main_product": ws.main_product})

    def put_targets(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        products = body.get("products") or []
        if not isinstance(products, list):
            raise ValueError("送る形が違います（products は配列）")
        codes = [str(p) for p in products]
        if "main_product" in body:
            self.store.save_targets(_number(params), codes, body.get("main_product"))
        else:
            self.store.save_targets(_number(params), codes)
        return self.targets({"number": _number(params)})

    def put_main(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        self.store.save_main(_number(params), body.get("main_product"))
        return self.targets({"number": _number(params)})

    # ④ 置換 -----------------------------------------------------------------

    def replace(self, params: dict[str, str]) -> "Response":
        ws = self._ws(params)
        plan = self._plan(params)
        marks = replace_overridden(ws, plan)
        groups: dict[str, dict[str, list[str]]] = {}
        rows = []
        for o, n, b in ws.replaces.get(plan, []):
            if o not in groups:
                groups[o] = dict(replace_groups(ws.bom, ws.targets, o))
            # count＝この行が効く製品の数（風船番号の無い全部の行は、元品番を置き換えられる製品の数）
            members = groups[o].get(b, []) if b is not None else sorted({p for ps in groups[o].values() for p in ps})
            rows.append({"old": o, "new": n, "balloon": b, "count": len(members), "products": members,
                         "overridden": marks[(o, n, b)]})
        return self._json({"rows": rows})

    def replace_groups(self, params: dict[str, str]) -> "Response":
        """元品番を置き換えられる箇所の、風船番号ごとのまとまり（風船番号・使う製品とその数）。保存しない。"""
        ws = self._ws(params)
        code = input_code(params.get("code", ""), name="元品番")
        return self._json({"code": code, "groups": [{"balloon": b, "count": len(ps), "products": ps}
                                                    for b, ps in replace_groups(ws.bom, ws.targets, code)]})

    def put_replace(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        pairs = []
        seen: dict[tuple[str, str | None], int] = {}
        for i, r in enumerate(_objects(body, "rows"), start=1):
            old, new = (nfkc(str(r.get(k) or "")).strip() for k in ("old", "new"))
            if not old and not new:
                continue
            if not old or not new:
                raise ValueError(f"元品番と新品番を入力してください（{i}行目）")
            balloon = r.get("balloon")
            # 行の番号は画面の行（空の行も数える）で示す
            key = (old, None if balloon is None else str(balloon))
            if key in seen:
                raise ValueError(f"元品番 {old} が重複しています（{seen[key]}行目・{i}行目）")
            seen[key] = i
            pairs.append((old, new, key[1]))
        self.store.save_replaces(_number(params), self._plan(params), pairs)
        return self.replace({"number": _number(params), "plan": self._plan(params)})

    # ⑪ 置換（変えた後の構成の部品を並べ、品番を打ち替えると全製品で置き換える） -------------------

    def after_parts(self, params: dict[str, str]) -> "Response":
        ws = self._ws(params)
        items = ws.bom.items
        return self._json({"parts": [
            {"code": code, "name": items[code].name if code in items else None, "used": len(users), "products": users,
             "was": was} for code, users, was in after_parts(ws, self._plan(params))]})

    def apply_replace(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        self.store.apply_replace(_number(params), self._plan(params), body.get("old"), body.get("new"))
        return self.after_parts({"number": _number(params), "plan": self._plan(params)})

    def revert_replace(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        self.store.revert_replace(_number(params), self._plan(params), body.get("part"))
        return self.after_parts({"number": _number(params), "plan": self._plan(params)})

    # ⑤ グリッド画面 ---------------------------------------------------------------

    def grid(self, params: dict[str, str]) -> "Response":
        """列＝親品番（打った物＋変更のある製品）・行＝品番・セル＝その製品でのその品番の変えた後の数量（無い所は出さない）。

        balloons は行ごとに、その製品でのその品番の風船番号（風船の行に出す）。main_product は並びの基準。
        """
        ws = self._ws(params)
        plan = self._plan(params)
        info = next((p for p in ws.plans if p.plan == plan), None)
        changes = ws.plan_changes(plan)
        columns = list(info.grid_columns) if info else []
        rows = list(info.grid_rows) if info else []
        for c in changes:
            if c.product not in columns:
                columns.append(c.product)
            if c.part not in rows:
                rows.append(c.part)
        replace_index = {pair_key(o, n, b): i + 1 for i, (o, n, b) in enumerate(ws.replaces.get(plan, []))}
        now: dict[str, dict[str, Decimal]] = {}
        cells: dict[str, dict[str, Decimal]] = {}
        origin: dict[str, int | None] = {}
        problem: dict[str, dict[str, bool]] = {}
        locked = {r: {p: True for p in columns if grid_locked(ws, plan, p, r)} for r in rows}
        checked = {c.change.place: c for c in check_changes(ws, changes)}
        for r in rows:
            for p in columns:
                found = occurrences(ws.bom, p, r)
                if found:
                    now.setdefault(r, {})[p] = sum((line.quantity for line in found), Decimal(0))
                    cells.setdefault(r, {})[p] = now[r][p]
        for c in changes:
            cells.setdefault(c.part, {})
            cells[c.part][c.product] = cells[c.part].get(c.product, Decimal(0)) + c.delta
            if c.source == REPLACE and c.origin in replace_index:
                origin.setdefault(c.part, replace_index[c.origin])
            if checked[c.place].problems:
                problem.setdefault(c.part, {})[c.product] = True
        return self._json({
            "columns": columns,
            "rows": [{"part": r, "origin": origin.get(r),
                      "cells": {p: _s(v) for p, v in cells.get(r, {}).items()},
                      "now": {p: _s(v) for p, v in now.get(r, {}).items()},
                      "problems": problem.get(r, {}), "locked": locked[r],
                      "balloons": _balloons(ws, plan, columns, r)} for r in rows],
            "targets": ws.targets,
            "main_product": ws.main_product,
        })

    def add_plan(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """［＋］で空の案を足す。足した案の名前（plan）と検討（案の一覧つき）を返す。"""
        plan = self.store.add_plan(_number(params))
        data = self._study_json(self.store.workspace(_number(params))).json()
        return self._json({**data, "plan": plan})

    def locked_cells(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """打ちかけの見出し（columns）・行（rows）のうち、グリッド画面で書けないセル（品番 → 親品番 → true）。保存しない。"""
        ws = self._ws(params)
        plan = self._plan(params)
        columns = [input_code(c, name="親品番") for c in _list(body, "columns") if c is not None and str(c).strip()]
        rows = [input_code(r, name="品番") for r in _list(body, "rows") if r is not None and str(r).strip()]
        locked = {r: {p: True for p in columns if grid_locked(ws, plan, p, r)} for r in rows}
        return self._json({"locked": {r: v for r, v in locked.items() if v},
                           "balloons": {r: _balloons(ws, plan, columns, r) for r in rows}})

    def put_grid(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        self.store.save_grid(_number(params), self._plan(params), _list(body, "columns"), _list(body, "rows"),
                           _objects(body, "cells"))
        return self.grid({"number": _number(params), "plan": self._plan(params)})

    # ⑧ 書き換え -------------------------------------------------------------

    def rewrite(self, params: dict[str, str]) -> "Response":
        """1製品の全構成に案の変更を当てた行。置き換えた箇所は元の行の位置に新しい品番を出す。"""
        ws = self._ws(params)
        plan = self._plan(params)
        product = input_code(params.get("product") or (ws.targets[0] if ws.targets else ""), name="対象品番")
        changes = {c.place[1:]: c for c in ws.plan_changes(plan) if c.product == product}
        lines = product_lines(ws.bom, product)
        rows: list[dict[str, Any]] = []
        used: set[tuple[str, str | None, str]] = set()
        added = [k for k, c in changes.items() if not any(l.key == k for l in lines)]
        for line in lines:
            c = changes.get(line.key)
            qty = line.quantity + (c.delta if c else 0)
            used.add(line.key)
            if qty == 0 and c is not None:
                swap = next((k for k in added if k[0] == line.parent and k[1] == line.balloon and k not in used), None)
                if swap is not None:
                    used.add(swap)
                    rows.append({"parent": line.parent, "balloon": line.balloon, "part": swap[2],
                                 "quantity": _s(changes[swap].delta), "before_part": line.child,
                                 "before_quantity": _s(line.quantity), "changed": True})
                    continue
            rows.append({"parent": line.parent, "balloon": line.balloon, "part": line.child, "quantity": _s(qty),
                         "before_part": line.child, "before_quantity": _s(line.quantity), "changed": c is not None})
        for k in added:
            if k in used:
                continue
            rows.append({"parent": k[0], "balloon": k[1], "part": k[2], "quantity": _s(changes[k].delta),
                         "before_part": None, "before_quantity": None, "changed": True})
        # 親ごとにまとめる。親の並びは構成の木で出てくる順（製品→その子の組立品→孫…）、同じ親の中は風船の順
        rank = {p: i for i, p in enumerate(dict.fromkeys([product, *(l.parent for l in lines)]))}
        rows = [r for _, r in sorted(enumerate(rows), key=lambda x: (
            rank.get(x[1]["parent"], len(rank)), x[1]["parent"] if x[1]["parent"] not in rank else "",
            _balloon_order(x[1]["balloon"]), x[0]))]
        return self._json({"product": product, "targets": ws.targets, "rows": rows})

    def put_rewrite(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        product = input_code(body.get("product") or "", name="対象品番")
        self.store.save_rewrite(_number(params), self._plan(params), product, _objects(body, "rows"))
        return self.rewrite({"number": _number(params), "plan": self._plan(params), "product": product})

    # ⑥ 変更一覧 -------------------------------------------------------------

    def _change_rows(self, params: dict[str, str]) -> list[dict[str, Any]]:
        ws = self._ws(params)
        plan = self._plan(params)
        product = params.get("product", "").strip()
        rows = []
        for c in check_changes(ws, ws.plan_changes(plan)):
            ch = c.change
            if product and ch.product != product:
                continue
            item = ws.bom.items.get(ch.part)
            rows.append({
                "product": ch.product, "parent": ch.parent, "balloon": ch.balloon, "part": ch.part,
                # 今の部品表に箇所が無い（元の品番が違う・箇所が無い）時は、変更前の数が無いので None（画面は —）
                "name": None if item is None else item.name,
                "before": None if NO_PLACE in c.problems or OTHER_PART in c.problems else _s(c.before),
                "after": _s(c.after),
                "delta": _s(ch.delta), "added": c.added, "problems": list(c.problems), "source": ch.source,
            })
        # ⑩ で書き換えた製品の費目（品番の欄に費目の名前・親と風船は無し）
        for r in own_rows(ws, plan):
            if product and r["product"] != product:
                continue
            for key, v in r["heads"].items():
                if v["after"] is None:
                    continue
                before = v["now"] if v["now"] is not None else (Decimal(0) if key == "processing" else None)
                rows.append({
                    "product": r["product"], "parent": "", "balloon": None, "part": OWN_HEADS[key].value,
                    "name": "製品の費目", "before": _s(v["now"]), "after": _s(v["after"]),
                    "delta": None if before is None else _s(v["after"] - before), "added": False, "problems": [],
                    "source": "own",
                })
        order = {p: i for i, p in enumerate(ws.targets)}
        rows.sort(key=lambda r: (order.get(r["product"], len(order)), r["product"], r["parent"],
                                 r["balloon"] or "", r["part"]))
        return rows

    def changes(self, params: dict[str, str]) -> "Response":
        return self._json({"rows": self._change_rows(params)})

    # ⑩ 製品の費目 -----------------------------------------------------------

    def own(self, params: dict[str, str]) -> "Response":
        """対象品番ごとの、書き換える費目（加工費・型費・直接費）の台帳の値と書き換えた値。"""
        ws = self._ws(params)
        return self._json({
            "heads": [{"key": k, "label": h.value} for k, h in OWN_HEADS.items()],
            "rows": [{**r, "heads": {k: {"now": _s(v["now"]), "after": _s(v["after"])} for k, v in r["heads"].items()}}
                     for r in own_rows(ws, self._plan(params))],
        })

    def put_own(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        self.store.save_owns(_number(params), self._plan(params), _objects(body, "rows"))
        return self.own({"number": _number(params), "plan": self._plan(params)})

    # ⑨ 仮品番 ---------------------------------------------------------------

    def temp_items(self, params: dict[str, str]) -> "Response":
        """仮品番の行（master: 品目の台帳に同じ品番があり、台帳の値を使う）と、変更に出る未登録の品番。"""
        ws = self._ws(params)
        return self._json({
            "rows": [{"code": c, "name": nm, "material": _s(m), "master": c not in ws.temp_merged} for c, nm, m in ws.temps],
            "unregistered": unregistered_parts(ws),
        })

    def put_temp_items(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        self.store.save_temps(_number(params), _objects(body, "rows"))
        return self.temp_items({"number": _number(params)})

    # ⑦ 結果・計算条件 -------------------------------------------------------

    def conditions(self, params: dict[str, str]) -> "Response":
        ws = self._ws(params)
        rows = []
        for product in ws.targets:
            volume = ws.bom.volume(product)
            rows.append({"product": product, "bom_annual": None if volume is None else _s(volume.annual),
                         "annual": _s(ws.conditions.get(product))})
        return self._json({"rows": rows})

    def put_conditions(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        annual = body.get("annual") or {}
        if not isinstance(annual, dict):
            raise ValueError("送る形が違います（annual は品番→年間台数）")
        self.store.save_conditions(_number(params), annual)
        return self.conditions({"number": _number(params)})

    @staticmethod
    def _head_json(result: dict[str, Any]) -> dict[str, Any]:
        return {
            "plans": result["plans"],
            "rows": [{**r, "annual": _s(r["annual"]), "now": _s(r["now"]),
                      "plans": {k: _s(v) for k, v in r["plans"].items()}} for r in result["rows"]],
            "impact": {k: _s(v) for k, v in result["impact"].items()},
            "impact_incomplete": result["impact_incomplete"],
            "weighted": {k: _s(v) for k, v in result["weighted"].items()},
        }

    def _calc(self, params: dict[str, str]) -> dict[str, Any]:
        """head は費目の名前（HEADS）か all（8費目を heads に並べて返す）。"""
        key = params.get("head", "total")
        now = datetime.now().replace(microsecond=0).isoformat()
        if key == "all":
            ws = self._ws(params)
            results = calculate_heads(ws)
            return {"computed_at": now, "head": "all", "plans": ws.plan_ids(),
                    "heads": [{"key": k, "label": h.value, **self._head_json(results[h])} for k, h in HEADS.items()]}
        head = HEADS.get(key)
        if head is None:
            raise ValueError(f"費目の指定が違います（{key}）")
        return {"computed_at": now, "head": key, **self._head_json(calculate(self._ws(params), head))}

    def calc(self, params: dict[str, str]) -> "Response":
        return self._json(self._calc(params))

    def where_used(self, params: dict[str, str]) -> "Response":
        """件数を先に返し、group（inside・outside）を付けた時だけその品番の一覧を返す。保存しない。"""
        ws = self._ws(params)
        code = input_code(params.get("code", ""))
        inside, outside = where_used(ws.bom, code, ws.targets)
        group = params.get("group", "")
        data: dict[str, Any] = {"code": code, "inside": len(inside),
                                "outside": len(outside)}
        if group in ("inside", "outside"):
            codes = inside if group == "inside" else outside
            data["products"] = [{"code": c, "name": ws.bom.items[c].name if c in ws.bom.items else None}
                                for c in codes]
        return self._json(data)

    def parts(self, params: dict[str, str]) -> "Response":
        """印を付けた製品（products をカンマで区切る・対象品番の中だけ）の部品と、何製品で使うか。保存しない。"""
        ws = self._ws(params)
        chosen = [c.strip() for c in params.get("products", "").split(",") if c.strip()]
        products = [t for t in ws.targets if t in set(chosen)]
        items = ws.bom.items
        return self._json({"of": len(products), "parts": [
            {"code": c, "name": items[c].name if c in items else None, "used": n} for c, n in parts_of(ws.bom, products)]})

    def release(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        version = self.store.take_release(_number(params))
        return self._study_json(self.store.workspace(_number(params), version))

    # Excel -----------------------------------------------------------------

    def _xlsx(self, name: str, header: list[str], rows: list[list[Any]], numeric: set[int]) -> "Response":
        from .api import Response

        openpyxl = load_openpyxl()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = name
        ws.append(header)
        for row in rows:
            ws.append([Decimal(v) if i in numeric and v is not None else v for i, v in enumerate(row)])
        ws.freeze_panes = "A2"
        out = io.BytesIO()
        wb.save(out)
        return Response(200, out.getvalue(), XLSX_TYPE,
                        {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.xlsx"})

    def export_changes(self, params: dict[str, str]) -> "Response":
        rows = self._change_rows(params)
        return self._xlsx("変更一覧", ["製品", "親", "風船", "品番", "名称", "変更前", "変更後", "差", "要確認"],
                          [[r["product"], r["parent"], r["balloon"], r["part"], r["name"], r["before"], r["after"],
                            r["delta"], "・".join(r["problems"])] for r in rows], {5, 6, 7})

    def export_result(self, params: dict[str, str]) -> "Response":
        data = self._calc(params)
        plans = data["plans"]
        if data["head"] == "all":
            # 1行＝対象品番×費目（費目の並びは式の順）。最後に費目ごとの年間の影響と台数の加重平均
            rows = [[r["product"], h["label"], r["annual"], r["now"], *(r["plans"][p] for p in plans)]
                    for i in range(len(data["heads"][0]["rows"]) if data["heads"] else 0)
                    for h in data["heads"] for r in [h["rows"][i]]]
            rows += [["年間の影響", h["label"], None, None, *(h["impact"][p] for p in plans)] for h in data["heads"]]
            rows += [["台数の加重平均（1台あたり）", h["label"], None, None, *(h["weighted"][p] for p in plans)]
                     for h in data["heads"]]
            return self._xlsx("結果", ["対象品番", "費目", "年間台数", "今", *(f"案{p}" for p in plans)], rows,
                              set(range(2, 4 + len(plans))))
        rows = [[r["product"], r["annual"], r["now"], *(r["plans"][p] for p in plans)] for r in data["rows"]]
        rows.append(["年間の影響", None, None, *(data["impact"][p] for p in plans)])
        rows.append(["台数の加重平均（1台あたり）", None, None, *(data["weighted"][p] for p in plans)])
        return self._xlsx("結果", ["対象品番", "年間台数", "今", *(f"案{p}" for p in plans)], rows,
                          set(range(1, 3 + len(plans))))


def study_routes(api: "Api") -> list[tuple[str, str, Callable[..., "Response"]]]:
    return StudyApi(api).routes()
