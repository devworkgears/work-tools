"""設計検討（/v2/）の API。検討は検討番号（number）と版（version: 空なら仮）で指す。"""

import unicodedata
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import quote

from . import settings
from .formula import Head, breakdown, registered_own
from .importer import (
    TABLE_FIELDS,
    ColumnMap,
    MissingColumnsError,
    Table,
    VersionImport,
    guess_columns,
    load_openpyxl,
    save_workbook,
    read_csv,
)
from .model import CostHead, Version
from .normalize import to_code
from .rollup import CostCalculator
from .study import (
    ADD,
    ATTRIBUTES,
    DEEP,
    HEADS,
    ListCell,
    NEGATIVE,
    OWN_HEADS,
    NO_PLACE,
    OTHER_PART,
    Attributes,
    SlotKey,
    StudyStore,
    Temp,
    UNREGISTERED,
    WAITING,
    Workspace,
    check_changes,
    calculate,
    calculate_heads,
    own_rows,
    after_parts,
    after_places,
    after_table,
    annual_of,
    deep_parts,
    direct_lines,
    input_code,
    input_optional_code,
    inside_lines,
    material_effects,
    nfkc,
    parts_of,
    problem_count,
    replace_groups,
    replace_overridden,
    temp_lines,
    unregistered_parts,
    where_used,
)

if TYPE_CHECKING:
    from .api import Api, Response

# 設計検討の見本（製品をまたぐ差し替えを試せる大きさ）。パッケージの外に置き、配る zip には入れない
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples_v2"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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


def _slot_rows(now: dict[SlotKey, Decimal], deltas: dict[SlotKey, Decimal]) -> list[dict[str, Any]]:
    """直下の行に増減を当てた行（風船の順）。0 になった行と同じ風船に足した品番があれば、置き換えとして元の行に出す。"""
    rows: list[dict[str, Any]] = []
    added = {k: d for k, d in deltas.items() if k not in now}
    for (balloon, part), qty in now.items():
        after = qty + deltas.get((balloon, part), Decimal(0))
        swap = next((k for k in added if k[0] == balloon), None) if after == 0 and (balloon, part) in deltas else None
        rows.append({"balloon": balloon, "part": part if swap is None else swap[1],
                     "quantity": _s(after if swap is None else added.pop(swap)),
                     "before_part": part, "before_quantity": _s(qty), "changed": (balloon, part) in deltas})
    for (balloon, part), delta in added.items():
        rows.append({"balloon": balloon, "part": part, "quantity": _s(delta), "before_part": None, "before_quantity": None,
                     "changed": True})
    return [r for _, r in sorted(enumerate(rows), key=lambda x: (_balloon_order(x[1]["balloon"]), x[0]))]


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


def _rates_json(ws: Workspace) -> dict[str, str]:
    """計算に使った率（%）。"""
    rates = ws.use_rates()
    return {k: format((v * 100).normalize(), "f") for k, v in (("adjustment", rates.adjustment), ("indirect", rates.indirect))}


def item_attributes(table: Table, column_map: ColumnMap, labels: Mapping[str, str] | None = None) -> Attributes:
    """部品マスタの任意の列（系列・タイプ・図面）を品番ごとに。無い列は空文字。

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


def import_tables(tables: list[Table], labels: Mapping[str, str] | None = None,
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
            self.drop_old_bom()  # settings.json を置き換えて起動し直した時
        return self._store

    def drop_old_bom(self) -> bool:
        """設定が変わっていれば部品表を消す。列の対応づけも、前の語で当てた物が残らないように消す。"""
        dropped = self.store.drop_old_bom()
        if dropped:
            self.api.store.clear_column_maps()
        return dropped

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
            ("POST", r"/api/v2/quantity", self.put_quantity),
            ("POST", r"/api/v2/add-part", self.add_part),
            ("GET", r"/api/v2/after-list", self.after_list),
            ("POST", r"/api/v2/plans", self.add_plan),
            ("GET", r"/api/v2/rewrite", self.rewrite),
            ("PUT", r"/api/v2/rewrite", self.put_rewrite),
            ("GET", r"/api/v2/changes", self.changes),
            ("GET", r"/api/v2/own", self.own),
            ("PUT", r"/api/v2/own", self.put_own),
            ("GET", r"/api/v2/inside", self.inside),
            ("POST", r"/api/v2/changes/drop-deep", self.drop_deep),
            ("GET", r"/api/v2/temp-inside", self.temp_inside),
            ("PUT", r"/api/v2/temp-inside", self.put_temp_inside),
            ("GET", r"/api/v2/temp-items", self.temp_items),
            ("PUT", r"/api/v2/temp-items", self.put_temp_items),
            ("GET", r"/api/v2/conditions", self.conditions),
            ("PUT", r"/api/v2/conditions", self.put_conditions),
            ("GET", r"/api/v2/calc", self.calc),
            ("GET", r"/api/v2/where-used", self.where_used),
            ("GET", r"/api/v2/parts", self.parts),
            ("POST", r"/api/v2/release", self.release),
            ("GET", r"/api/v2/export/study", self.export_study),
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
        return self._json({"labels": dict(settings.labels())})

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
        version, kinds, warnings, attributes = import_tables(tables, settings.labels(), maps)
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
        """品番の右の列（名称・系列・タイプ・図面・年間台数・材料費・製造原価）。無い品番は registered: false。

        number があればその検討の部品表（版の写し・仮品番を混ぜた物）で引き、仮品番は temp: true。
        """
        temp: set[str] = set()
        if params.get("number"):
            ws = self._ws(params)
            bom, attributes, temp, rates = ws.bom, ws.attributes, ws.temp_merged, ws.rates
        else:
            bom, attributes, rates = self.store.load_bom(), self.store.load_attributes(), None
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
                "manufacturing": _s(breakdown(material, registered_own(bom, code), rates=rates).value(CostHead.MANUFACTURING)
                                    if material is not None else None),
            }
        return self._json({"items": result})

    def products(self, params: dict[str, str]) -> "Response":
        """品番検索: 製品（どこの子でもない親か、年間台数のある品目）のうち、品番・名称・系列に語を含み、タイプが合う物。

        product_specs は製品にあるタイプの一覧（条件の選択肢）。
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

    # ⑪ 部品変更（変えた後の構成の部品を並べ、品番を打ち替えると全製品で置き換える） -------------------

    def after_parts(self, params: dict[str, str]) -> "Response":
        ws = self._ws(params)
        items = ws.bom.items
        found = after_parts(ws, self._plan(params))
        return self._json({"parts": [
            {"code": p.code, "name": items[p.code].name if p.code in items else None, "used": len(p.users),
             "before": p.before, "products": p.users, "was": p.was} for p in found],
            "deep": [{"code": c, "name": items[c].name if c in items else None, "used": n}
                     for c, n in deep_parts(ws, (p.code for p in found))],
            "places": [{"product": p.product, "balloon": p.balloon, "part": p.part, "quantity": _s(p.quantity), "was": p.was,
                        "before": _s(p.before)} for p in after_places(ws, self._plan(params))]})

    def apply_replace(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """old を new に、対象品番の全製品で置き換える。rows（product・balloon・old・new）があれば、その箇所だけ。"""
        if "rows" in body:
            self.store.apply_places(_number(params), self._plan(params), _objects(body, "rows"))
        else:
            self.store.apply_replace(_number(params), self._plan(params), body.get("old"), body.get("new"))
        return self.after_parts({"number": _number(params), "plan": self._plan(params)})

    def revert_replace(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        place = (body.get("product"), body.get("balloon")) if "product" in body else None
        self.store.revert_replace(_number(params), self._plan(params), body.get("part"), place)
        return self.after_parts({"number": _number(params), "plan": self._plan(params)})

    def put_quantity(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """箇所（product・balloon・part）ごとの、変えた後の数量。"""
        self.store.save_quantities(_number(params), self._plan(params), _objects(body, "rows"))
        return self.after_parts({"number": _number(params), "plan": self._plan(params)})

    def add_part(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """部品を、選んだ製品の直下にまとめて足す。"""
        self.store.add_part(_number(params), self._plan(params), body.get("part"), body.get("quantity"), _list(body, "products"))
        return self.after_parts({"number": _number(params), "plan": self._plan(params)})

    # ⑤ 構成一覧 ---------------------------------------------------------------

    def after_list(self, params: dict[str, str]) -> "Response":
        """行＝対象品番の直下の部品・セル＝製品ごとの変えた後の数量と風船番号。読むだけ。"""
        ws = self._ws(params)
        items = ws.bom.items
        # 主対象（無ければ先頭の製品）の風船番号の順。そこに無い部品は、対象品番の順に最初に使う製品の風船番号の順で後ろ
        lead = list(dict.fromkeys([*([ws.main_product] if ws.main_product else []), *ws.targets]))

        def order(row: tuple[str, dict[str, ListCell]]) -> tuple[Any, ...]:
            at = next(i for i, p in enumerate(lead) if p in row[1])
            return (at, min(_balloon_order(b) for b in row[1][lead[at]].balloons), row[0])

        return self._json({"targets": ws.targets, "main_product": ws.main_product, "rows": [
            {"part": part, "name": items[part].name if part in items else None,
             "cells": {p: {"balloon": "・".join(b or "－" for b in sorted(c.balloons, key=_balloon_order)),
                           "quantity": _s(c.quantity), "now": _s(c.now), "changed": c.changed, "problem": c.problem}
                       for p, c in cells.items()}}
            for part, cells in sorted(after_table(ws, self._plan(params)).items(), key=order)]})

    def add_plan(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """［＋］で空の案を足す。足した案の名前（plan）と検討（案の一覧つき）を返す。"""
        plan = self.store.add_plan(_number(params))
        data = self._study_json(self.store.workspace(_number(params))).json()
        return self._json({**data, "plan": plan})

    # ⑧ 構成変更 -------------------------------------------------------------

    def rewrite(self, params: dict[str, str]) -> "Response":
        """1製品の直下に案の変更を当てた行。置き換えた箇所は元の行の位置に新しい品番を出す。assembly は中を見られる組立品。"""
        ws = self._ws(params)
        plan = self._plan(params)
        product = input_code(params.get("product") or (ws.targets[0] if ws.targets else ""), name="対象品番")
        deltas = {(c.balloon, c.part): c.delta for c in ws.plan_changes(plan) if c.product == product and c.parent == product}
        rows = [{**r, "parent": product, "assembly": bool(inside_lines(ws, r["part"]))}
                for r in _slot_rows(direct_lines(ws.bom, product), deltas)]
        return self._json({"product": product, "targets": ws.targets, "rows": rows})

    def put_rewrite(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        product = input_code(body.get("product") or "", name="対象品番")
        self.store.save_rewrite(_number(params), self._plan(params), product, _objects(body, "rows"))
        return self.rewrite({"number": _number(params), "plan": self._plan(params), "product": product})

    def inside(self, params: dict[str, str]) -> "Response":
        """組立品の中（直下）を見る。保存しない。"""
        ws = self._ws(params)
        code = input_code(params.get("code", ""))
        items = ws.bom.items
        rows = [{"balloon": b, "part": part, "name": items[part].name if part in items else None, "quantity": _s(qty),
                 "assembly": bool(inside_lines(ws, part))} for (b, part), qty in inside_lines(ws, code).items()]
        rows.sort(key=lambda r: _balloon_order(r["balloon"]))
        return self._json({"code": code, "name": items[code].name if code in items else None, "rows": rows})

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
        rows = self._change_rows(params)
        summary = summarize_changes(rows)
        product = params.get("product", "").strip()
        targets = [p for p in self._ws(params).targets if not product or p == product]
        ws = self._ws(params)
        return self._json({"rows": rows, "summary": summary, "by_product": changes_by_product(summary, targets),
                           "deep": sum(1 for r in rows if DEEP in r["problems"]),
                           "temps": {t.code: {"base": t.base, "name": t.name, "changes": temp_changes(ws, t)}
                                     for t in ws.temps if t.base is not None and t.code in ws.temp_merged}})

    def drop_deep(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        self.store.drop_deep_changes(_number(params), self._plan(params))
        return self.changes({"number": _number(params), "plan": self._plan(params)})

    # ⑩ 費目変更 -----------------------------------------------------------

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
            "rows": [{"code": t.code, "name": t.name, "material": _s(t.material), "master": t.code not in ws.temp_merged,
                      "base": t.base, "changed": len(temp_changes(ws, t)) if t.base is not None else 0,
                      # 流用元から計算した材料費（材料費を打った行と、分からない時は空）
                      "derived": _s(ws.bom.cost_value(t.code, CostHead.MATERIAL))
                      if t.base is not None and t.material is None and t.code in ws.temp_merged else None}
                     for t in ws.temps],
            "unregistered": unregistered_parts(ws),
        })

    def put_temp_items(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        self.store.save_temps(_number(params), _objects(body, "rows"))
        return self.temp_items({"number": _number(params)})

    def temp_inside(self, params: dict[str, str]) -> "Response":
        """流用した仮品番の直下（流用元に中の変更を当てた行）。"""
        ws = self._ws(params)
        code = input_code(params.get("code", ""))
        temp = next((t for t in ws.temps if t.code == code and t.base is not None), None)
        if temp is None:
            raise ValueError(f"{code} は流用した仮品番ではありません")
        deltas = {(b, part): d for b, part, d in temp.lines}
        return self._json({"code": code, "base": temp.base, "rows": _slot_rows(direct_lines(ws.bom, temp.base or ""), deltas)})

    def put_temp_inside(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        self.store.save_temp_lines(_number(params), body.get("code"), _objects(body, "rows"))
        return self.temp_inside({"number": _number(params), "code": str(body.get("code"))})

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
            "base": {k: _s(v) for k, v in result["base"].items()},
        }

    def _calc(self, params: dict[str, str]) -> dict[str, Any]:
        """head は費目の名前（HEADS）か all（8費目を heads に並べ、変更ごとの影響 effects も返す）。"""
        key = params.get("head", "total")
        now = datetime.now().replace(microsecond=0).isoformat()
        if key == "all":
            ws = self._ws(params)
            results = calculate_heads(ws)
            plan = self._plan(params)
            return {"computed_at": now, "head": "all", "plans": ws.plan_ids(), "rates": _rates_json(ws),
                    "effects": [{**e, "unit_min": _s(e["unit_min"]), "unit_max": _s(e["unit_max"]), "impact": _s(e["impact"]),
                                 "products": [{**p, "unit": _s(p["unit"]), "annual": _s(p["annual"]), "impact": _s(p["impact"])}
                                              for p in e["products"]]}
                                for e in change_effects(ws, plan, self._change_rows({**params, "product": ""}))],
                    "heads": [{"key": k, "label": h.value, **self._head_json(results[h])} for k, h in HEADS.items()]}
        head = HEADS.get(key)
        if head is None:
            raise ValueError(f"費目の指定が違います（{key}）")
        ws = self._ws(params)
        return {"computed_at": now, "head": key, "rates": _rates_json(ws), **self._head_json(calculate(ws, head))}

    def calc(self, params: dict[str, str]) -> "Response":
        return self._json(self._calc(params))

    def where_used(self, params: dict[str, str]) -> "Response":
        """件数を先に返し、group（inside・outside）を付けた時だけその品番の一覧を返す。保存しない。"""
        ws = self._ws(params)
        code = input_code(params.get("code", ""))
        inside, outside, deep = where_used(ws.bom, code, ws.targets)
        group = params.get("group", "")
        data: dict[str, Any] = {"code": code, "inside": len(inside),
                                "outside": len(outside), "deep": deep}
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

    def export_study(self, params: dict[str, str]) -> "Response":
        """検討1つを1つの Excel に出す（構成の変更と金額の変更を別のシートに・1行1件）。"""
        ws = self._ws(params)
        plan = self._plan(params)
        changes = self._change_rows({**params, "product": ""})
        results = calculate_heads(ws)
        money, incomplete = [], False
        for head in HEADS.values():
            for r in results[head]["rows"]:
                now, after = r["now"], r["plans"].get(plan)
                diff = None if now is None or after is None else after - now
                flag = bool(r["now_incomplete"] or r["incomplete"].get(plan))
                incomplete |= flag
                money.append([r["product"], r["name"], head.value, now, after, diff, _percent(diff, now), r["annual"],
                              None if diff is None or r["annual"] is None else diff * r["annual"], "不完全" if flag else ""])
        order = {p: i for i, p in enumerate(ws.targets)}
        money.sort(key=lambda r: order[r[0]])  # 対象品番の順（同じ品番の中は費目の順のまま）
        sheets: list[tuple[str, list[str], list[list[Any]]]] = [
            ("まとめ", ["項目", "値"], [
                ["検討番号", ws.number], ["版", "仮" if ws.version is None else ws.version],
                ["題目", ws.header.get("title")], ["担当", ws.header.get("owner")],
                ["出力日時", datetime.now().replace(microsecond=0).isoformat(sep=" ")],
                ["対象品番の数", len(ws.targets)], ["不完全", "あり" if incomplete else "なし"],
                *([f"年間の影響（{h.value}）", results[h]["impact"].get(plan)] for h in HEADS.values()),
                *([f"年間の影響の現行比％（{h.value}）", _percent(results[h]["impact"].get(plan), results[h]["base"].get(plan))]
                  for h in HEADS.values()),
                *([f"台数の加重平均（1台あたり・{h.value}）", results[h]["weighted"].get(plan)] for h in HEADS.values()),
                *([f"説明: {name}", text] for name, text in SHEET_NOTES)]),
            ("変更ごとの影響", ["変更", "元品番", "元の名称", "新品番", "新の名称", "製品の数", "1台あたりの差（最小）", "1台あたりの差（最大）",
                         "年間の影響", "現行比（％）", "不完全", "要確認"],
             [[e["kind"], e["old"], e["old_name"], e["new"], settings.word(e["head"]) if e["head"] else e["new_name"],
               len(e["products"]), e["unit_min"], e["unit_max"], e["impact"],
               _percent(e["impact"], results[Head.MANUFACTURING]["base"].get(plan)), "不完全" if e["incomplete"] else "",
               "・".join(e["problems"])] for e in change_effects(ws, plan, changes)]),
            ("構成の変更", ["変更", "元品番", "元の名称", "新品番", "新の名称", "数量", "製品の数", "製品", "要確認"],
             [[s["kind"], s["old"], s["old_name"], s["new"], s["new_name"], s["quantity"], len(s["products"]),
               "・".join(s["products"]), "・".join(s["problems"])] for s in summarize_changes(changes)]),
            ("構成の明細", ["製品", "名称", "変更", "元品番", "元の名称", "新品番", "新の名称", "変更前", "変更後", "親", "風船", "要確認"],
             [[r["product"], ws.bom.items[r["product"]].name if r["product"] in ws.bom.items else None, r["kind"],
               r["old"], r["old_name"], r["new"], r["new_name"], _num(r["before"]), _num(r["after"]), r["parent"],
               r["balloon"], "・".join(r["problems"])]
              for r in changes_by_product(summarize_changes(changes), ws.targets)]),
            ("金額", ["対象品番", "名称", "費目", "今", "変更後", "差", "差（％）", "年間台数", "年間の影響", "不完全"], money),
            ("計算条件", ["種類", "対象", "項目", "値", "備考"], self._condition_rows(ws, plan)),
        ]
        inner = [[t.code, t.name, t.base, s["kind"], s["old"], s["old_name"], s["new"], s["new_name"], d["balloon"],
                  _num(d["before"]), _num(d["after"])]
                 for t in ws.temps if t.base is not None and t.code in ws.temp_merged
                 for s in temp_changes(ws, t) for d in s["details"]]
        if inner:
            sheets.insert(4, ("仮品番の中", ["仮品番", "名称", "流用元", "変更", "元品番", "元の名称", "新品番", "新の名称", "風船",
                                          "変更前", "変更後"], inner))
        return self._xlsx(f"検討_{ws.number}", sheets)

    @staticmethod
    def _condition_rows(ws: Workspace, plan: str) -> list[list[Any]]:
        rates = ws.use_rates()
        note = "版を取った時の率" if ws.rates else None
        rows: list[list[Any]] = [["単価の時点", None, None, None, "時点は持っていない（取り込んだ原価の表の値で計算する）"],
                                 ["率", None, "諸経費", rates.adjustment, note], ["率", None, "間接費", rates.indirect, note]]
        for p in ws.targets:
            volume = ws.bom.volume(p)
            given = ws.conditions.get(p)
            value = given if given is not None else None if volume is None else volume.annual
            rows.append(["年間台数", p, None, value, "検討で入力" if given is not None else "部品表" if value is not None else "未登録"])
        for t in ws.temps:
            note = "" if t.base is None else f"流用元 {t.base}" + ("（材料費は入力した値）" if t.material is not None else " から計算")
            master = t.code not in ws.temp_merged
            rows.append(["仮品番", t.code, t.name, t.material if master else ws.bom.cost_value(t.code, CostHead.MATERIAL),
                         "品目の台帳の値を使う" if master else note])
        for r in own_rows(ws, plan):
            rows += [["製品の費目", r["product"], OWN_HEADS[k].value, v["after"],
                      "台帳に値なし" if v["now"] is None else f"台帳の値 {v['now']}"]
                     for k, v in r["heads"].items() if v["after"] is not None]
        return rows

    def _xlsx(self, name: str, sheets: list[tuple[str, list[str], list[list[Any]]]]) -> "Response":
        from .api import Response

        wb = load_openpyxl().Workbook()
        wb.remove(wb.active)
        for title, header, rows in sheets:
            sheet = wb.create_sheet(title)
            for values in (header, *rows):
                sheet.append(values)
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"  # 「=」で始まる文字を式にしない
            sheet.freeze_panes = "A2"
        data = save_workbook(wb, {"まとめ": [1], "金額": [3], "計算条件": [1, 3]})
        return Response(200, data, XLSX_TYPE,
                        {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.xlsx"})


# Excel の各シートの列の意味（渡した先が列を取り違えないように、まとめのシートに書く）
SHEET_NOTES = (
    ("変更ごとの影響", "した事ごとに、製造原価への効き方。差は1台あたりで製品ごとに違えば最小と最大、年間の影響＝差×年間台数の合計。"
                  "現行比は対象品番の今の製造原価×年間台数の合計に対する割合。足すとまとめの年間の影響（製造原価）になる"),
    ("構成の変更", "した事ごとに1行。置き換え＝同じ製品・親・風船番号で元品番が 0 になり新品番が増えた物。数量は1箇所あたり"),
    ("構成の明細", "1行＝製品×した事×箇所（親・風船）。変更の無い対象品番も「変更なし」で出す。数量は親1単位あたりで、"
                   "置き換えは変更前＝元品番の数量・変更後＝新品番の数量"),
    ("金額", "1行＝対象品番×費目。1台あたり。差＝変更後−今、年間の影響＝差×年間台数。費目には内訳と合計が混ざるので足し合わせない"),
    ("不完全", "未登録の原価や計算に入らない変更があり、金額が足りない行"),
    ("単価待ち", "単価の無い品番を使う変更。置き換えは、単価が分かるまで現行のまま（差なし）で計算する"),
    ("単位", "金額は取り込んだ原価の表と同じ単位、数量は部品表と同じ単位"),
)


def _percent(diff: Decimal | None, now: Decimal | None) -> Decimal | None:
    """今に対する差の割合（％・小数2桁）。今が無いか 0 なら None。"""
    return None if diff is None or not now else (diff / now * 100).quantize(Decimal("0.01"))


def _num(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def summarize_changes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """変更の行（製品×箇所×品番）を、した事（置き換え・数量・追加・削除）ごとに1行へまとめる。

    操作の記録は使わず、今の部品表との差分だけから作る。同じ製品・親・風船番号の中で、0 になった品番が1つ・
    増えた品番が1つの時だけ置き換えとみなす。details に製品ごとの箇所を持つ。製品の費目（金額）の行は入れない。
    """
    slots: dict[tuple[str, str, str | None], list[dict[str, Any]]] = {}
    for r in rows:
        if r["source"] != "own":
            slots.setdefault((r["product"], r["parent"], r["balloon"]), []).append(r)
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}

    def add(kind: str, old: dict[str, Any] | None, new: dict[str, Any] | None, before: str | None, after: str) -> None:
        one = old or new
        assert one is not None
        problems = list(dict.fromkeys(p for r in (old, new) if r for p in r["problems"]))
        key = (kind, old and old["part"], new and new["part"], tuple(problems))
        g = groups.setdefault(key, {
            "kind": kind, "old": old and old["part"], "old_name": old and old["name"], "new": new and new["part"],
            "new_name": new and new["name"], "problems": problems, "products": [], "details": []})
        if one["product"] not in g["products"]:
            g["products"].append(one["product"])
        g["details"].append({"product": one["product"], "parent": one["parent"], "balloon": one["balloon"],
                             "before": before, "after": after})

    for members in slots.values():
        down = [r for r in members if Decimal(r["delta"]) < 0]
        up = [r for r in members if Decimal(r["delta"]) > 0]
        # 足した部品は、同じ風船番号（無い時も）で消えた品番の置き換えとは見ない
        swap = [r for r in up if r["source"] != ADD]
        if len(down) == 1 and len(swap) == 1 and down[0]["before"] is not None and Decimal(down[0]["after"]) == 0:
            add("置き換え", down[0], swap[0], down[0]["before"], swap[0]["delta"])
            down, up = [], [r for r in up if r["source"] == ADD]
        for r in down:
            gone = r["before"] is not None and Decimal(r["after"]) == 0
            add("削除" if gone else "数量", r, None, r["before"], r["after"])
        for r in up:
            if r["added"]:
                add("追加", None, r, "0", r["after"])
            else:
                add("数量", r, None, r["before"], r["after"])
    for g in groups.values():
        texts: dict[str, set[str]] = {}
        for d in g["details"]:
            same = g["kind"] == "置き換え" and d["before"] == d["after"]
            d["quantity"] = d["after"] if same or g["kind"] == "追加" else f"{d['before'] or '—'} → {d['after']}"
            texts.setdefault(d["quantity"], set()).add(d["product"])
        g["quantity"] = next(iter(texts)) if len(texts) == 1 else "・".join(f"{t}（{len(p)}製品）" for t, p in texts.items())
    return list(groups.values())


def change_effects(ws: Workspace, plan: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """した事（構成の変更のまとめと、⑩ の費目ごと）ごとの、製造原価への効き方（年間の影響の大きい順）。

    式は材料費小計・加工費・償却費・直接費の足し算に率を掛けた物なので、した事ごとに分けて足すと全体の差になる。
    products は製品ごとの1台あたりの差。計算に入らない・単価や年間台数の分からない分は足さず、incomplete の印を付ける
    （単価の無い品番への置き換えは現行のままなので、差は出さずに「単価待ち」を付ける）。
    """
    rates = ws.use_rates()
    material = material_effects(ws, plan)
    out: list[dict[str, Any]] = []

    def add(head: str | None, group: dict[str, Any], units: dict[str, Decimal | None], unknown: bool = False) -> None:
        products = []
        for product, unit in units.items():
            annual = annual_of(ws, product)
            products.append({"product": product, "name": ws.bom.items[product].name if product in ws.bom.items else None,
                             "unit": unit, "annual": annual,
                             "impact": None if unit is None or annual is None else unit * annual})
        known = [p["unit"] for p in products if p["unit"] is not None]
        impacts = [p["impact"] for p in products if p["impact"] is not None]
        out.append({**{k: group.get(k) for k in ("kind", "old", "old_name", "new", "new_name")}, "head": head,
                    "problems": group.get("problems", []), "products": products,
                    "unit_min": min(known, default=None), "unit_max": max(known, default=None),
                    "impact": sum(impacts, Decimal(0)) if impacts else None, "incomplete": unknown or len(impacts) < len(products)})

    for g in summarize_changes(rows):
        parts = [g["old"], g["new"]] if g["kind"] == "置き換え" else [g["old"] or g["new"]]
        units: dict[str, Decimal | None] = {}
        unknown = waiting = False
        for d in g["details"]:
            for part in parts:
                effect = material[(d["product"], d["parent"], d["balloon"], part)]
                before = units.setdefault(d["product"], None)
                waiting |= effect.waiting
                if effect.value is None:
                    unknown = True
                else:
                    units[d["product"]] = (before or Decimal(0)) + effect.value * rates.factor
        add(None, {**g, "problems": [*g["problems"], *([WAITING] if waiting else [])]}, units, unknown)
    for key, head in OWN_HEADS.items():
        units = {}
        for r in own_rows(ws, plan):
            v = r["heads"][key]
            if v["after"] is not None:
                diff = v["after"] - (v["now"] or Decimal(0))
                units[r["product"]] = diff if head is Head.DIRECT else diff * rates.factor
        if units:
            add(head.value, {"kind": "製品の費目"}, units)
    return sorted(out, key=lambda e: (e["impact"] is None, -abs(e["impact"] or 0), -abs(e["unit_max"] or 0)))


def temp_changes(ws: Workspace, temp: Temp) -> list[dict[str, Any]]:
    """流用した仮品番の中の変更を、した事ごとに1行（流用元からの差）。"""
    now, after = temp_lines(ws.bom, temp)
    items = ws.bom.items
    return summarize_changes([{
        "product": temp.code, "parent": temp.base, "balloon": balloon, "part": part,
        "name": items[part].name if part in items else None, "before": _s(now.get((balloon, part), Decimal(0))),
        "after": _s(after[(balloon, part)]), "delta": _s(delta), "added": (balloon, part) not in now,
        "problems": ([] if part in items else [UNREGISTERED]) + ([NEGATIVE] if after[(balloon, part)] < 0 else []),
        "source": "temp",
    } for balloon, part, delta in temp.lines])


def changes_by_product(summary: list[dict[str, Any]], targets: list[str]) -> list[dict[str, Any]]:
    """した事のまとめを製品ごとに並べ直す（対象品番の順・変更の無い対象品番は「変更なし」・対象品番の外の製品は後ろ）。"""
    names = ("kind", "old", "old_name", "new", "new_name", "problems")
    found: dict[str, list[dict[str, Any]]] = {}
    for s in summary:
        for d in s["details"]:
            found.setdefault(d["product"], []).append({**{k: s[k] for k in names}, **d})
    none = {"kind": "変更なし", "old": None, "old_name": None, "new": None, "new_name": None, "problems": [],
            "parent": None, "balloon": None, "before": None, "after": None, "quantity": ""}
    products = [*targets, *(p for p in found if p not in targets)]
    return [r for p in products for r in found.get(p, [{**none, "product": p}])]


def study_routes(api: "Api") -> list[tuple[str, str, Callable[..., "Response"]]]:
    return StudyApi(api).routes()
