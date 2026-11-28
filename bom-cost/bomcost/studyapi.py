"""設計検討の API。検討は検討番号（number）と版（version: 空なら仮）で指す。"""

import re
from collections import Counter
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, NamedTuple
from urllib.parse import quote

from . import settings
from .formula import Head, breakdown, registered_own
from .importer import (
    TABLE_FIELDS,
    ColumnMap,
    FieldSpec,
    Table,
    VersionImport,
    guess_columns,
    load_openpyxl,
    save_workbook,
    read_csv,
)
from .inside import Rebuild, expand_parents, input_edits, inside_rows
from .model import CostHead, Version, split_balloon
from .normalize import to_code, to_optional_code
from .rollup import CostCalculator
from .structure import structure
from .study import (
    ADDED,
    ATTRIBUTES,
    DEEP,
    HEADS,
    NO_PLACE,
    NOT_TARGET,
    OWN_HEADS,
    REPLACED,
    Attributes,
    Checked,
    Prices,
    SHORT,
    StudyStore,
    Temp,
    UNREGISTERED,
    WAITING,
    Workspace,
    check_changes,
    calculate,
    calculate_heads,
    followers,
    own_rows,
    release_drift,
    variation_bases,
    variation_shortfalls,
    after_places,
    annual_of,
    input_optional_code,
    kind_of,
    known_code,
    material_effects,
    nfkc,
    problem_count,
    temp_placed,
    where_used,
)
from .upload import preview, read_upload

if TYPE_CHECKING:
    from .api import Api, Response

ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")  # Excel に書けない制御文字
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


def _number_order(text: str) -> tuple[int, int, str]:
    return (0, int(text), "") if text.isascii() and text.isdigit() else (1, 0, text)


def _balloon_order(balloon: str | None) -> tuple[int, tuple[int, int, str], tuple[int, int, str]]:
    """風船番号の並べ方: 風船番号、サブ番号（無いのが先）の順。数字は数の順、ほかは文字の順、空欄は最後（空欄どうしは、呼ぶ側で品番の順にする）。"""
    if balloon is None:
        return (1, (0, 0, ""), (0, 0, ""))
    head, branch = split_balloon(balloon)
    return (0, _number_order(head), (-1, 0, "") if branch is None else _number_order(branch))


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


# 取り込みの画面で選ぶ項目（計算か画面で使う物だけ）。系列・タイプ・図面は品目の属性
BOM_FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    "item": (*(f for f in TABLE_FIELDS["item"] if f.name in ("code", "name", "specification", "lifecycle")),
             *(FieldSpec(name, label, False) for name, label in ATTRIBUTES)),
    # 風船番号の列を使わないと、同じ部品の2か所が1行に畳まれて数量が減る（空欄の行はあってよい）
    "bom_line": tuple(replace(f, required=True) if f.name == "balloon" else f
                      for f in TABLE_FIELDS["bom_line"] if f.name != "level"),
    "cost": tuple(f for f in TABLE_FIELDS["cost"] if f.name in ("mbom_code", "material", "direct", "amortization", "processing")),
    "volume": TABLE_FIELDS["volume"],
}
TABLE_NAMES = {"item": "品目", "bom_line": "構成", "cost": "原価", "volume": "年間台数"}


class BomMap(NamedTuple):
    """取り込む表の種類と列の対応（項目の name → 見出し）。"""

    kind: str
    columns: dict[str, str]


def _missing(kind: str, columns: Mapping[str, str]) -> list[str]:
    return [f.label for f in BOM_FIELDS[kind] if f.required and f.name not in columns]


def table_kind(table: Table) -> BomMap:
    """見出しから表の種類（品目・構成・原価・年間台数）を当て、列の対応を作る。"""
    found: list[tuple[int, BomMap]] = []
    for kind, fields in BOM_FIELDS.items():
        columns, score = guess_columns(kind, table.headers, fields)
        if not _missing(kind, columns):
            found.append((score, BomMap(kind, columns)))
    if not found:
        raise ValueError(f"見出しから表の種類を判別できません（{table.name}）")
    return max(found, key=lambda x: x[0])[1]


def chosen_columns(f: Mapping[str, Any], table: Table) -> BomMap:
    """画面で選んだ表の種類と列の対応。"""
    kind, columns = f.get("table"), f.get("columns")
    if kind not in BOM_FIELDS or not isinstance(columns, dict):
        raise ValueError(f"{table.name} の表の種類と列の対応づけを選んでください")
    chosen = {str(k): str(v).strip() for k, v in columns.items() if v not in (None, "")}
    unknown = set(chosen) - {x.name for x in BOM_FIELDS[kind]}
    if unknown:
        raise ValueError(f"{TABLE_NAMES[kind]}に無い項目です: {'・'.join(sorted(unknown))}")
    for header in chosen.values():
        if header not in table.headers:
            raise ValueError(f"列「{header}」は {table.name} の見出しにありません")
    missing = _missing(kind, chosen)
    if missing:
        raise ValueError(f"{table.name}: {TABLE_NAMES[kind]}の必須の列が対応づいていません: {'・'.join(missing)}")
    return BomMap(kind, chosen)


def _rates_json(ws: Workspace) -> dict[str, str]:
    """計算に使った率（%）。"""
    rates = ws.use_rates()
    return {k: format((v * 100).normalize(), "f") for k, v in (("adjustment", rates.adjustment), ("indirect", rates.indirect))}


def item_attributes(table: Table, columns: Mapping[str, str]) -> Attributes:
    """品目の属性（系列・タイプ・図面）を品番ごとに。対応の無い項目は空文字。"""
    at = {name: table.headers.index(columns[name]) if name in columns else None for name, _ in ATTRIBUTES}
    code_at = table.headers.index(columns["code"])
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


def _blank_balloon_repeats(table: Table, column_map: ColumnMap) -> list[tuple[str, str]]:
    """風船番号が空で、2行以上ある親と子（1行に畳まれる）。"""
    count: Counter[tuple[str, str]] = Counter()
    for _, r in column_map.records(table):
        try:
            if to_optional_code(r.get("balloon")) is None:
                count[(to_code(r["parent"]), to_code(r["child"]))] += 1
        except (TypeError, ValueError):
            continue
    return [pair for pair, n in count.items() if n > 1 and pair[0] != pair[1]]


class BomRead(NamedTuple):
    """取り込む表を読んだ物: 版・入った表の種類・警告の文・品目の属性。"""

    version: Version
    kinds: list[str]
    warnings: list[str]
    attributes: Attributes


def import_tables(tables: list[Table], maps: list[BomMap | None] | None = None) -> BomRead:
    """maps は表ごとの列の対応（tables と同じ並び）。無い表は見出しから表の種類を当てる。"""
    work = VersionImport(Version("v2", datetime.now().replace(microsecond=0)))
    kinds: list[str] = []
    notes: list[str] = []
    attributes: Attributes = {}
    names = {name for name, _ in ATTRIBUTES}
    for i, table in enumerate(tables):
        kind, columns = (maps[i] if maps else None) or table_kind(table)
        column_map = ColumnMap(kind, table.headers, {k: v for k, v in columns.items() if k not in names})
        work.add_table(table, column_map)
        kinds.append(kind)
        if kind == "item":
            attributes.update(item_attributes(table, columns))
        if kind == "bom_line":
            repeats = _blank_balloon_repeats(table, column_map)
            if repeats:
                some = "・".join(f"{p} の {c}" for p, c in repeats[:5])
                notes.append(f"{table.name}: 風船番号が空で、同じ親と子の行が2行以上ある物が {len(repeats)} 組あり、"
                             f"1行分の数量で取り込みます（{some}{' など' if len(repeats) > 5 else ''}）")
    warnings = [f"{w.file}{'' if w.row is None else f' {w.row}行'}: {w.message}" for w in work.warnings]
    return BomRead(work.version, kinds, [*notes, *warnings], attributes)


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
        """設定が変わっていれば部品表を消す。"""
        return self.store.drop_old_bom()

    def routes(self) -> list[tuple[str, str, Callable[..., "Response"]]]:
        return [
            ("GET", r"/api/v2/labels", self.labels),
            ("GET", r"/api/v2/bom", self.bom_status),
            ("POST", r"/api/v2/bom/preview", self.bom_preview),
            ("POST", r"/api/v2/bom/import", self.bom_import),
            ("POST", r"/api/v2/bom/clear", self.bom_clear),
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
            ("POST", r"/api/v2/inside/replace", self.replace_inside),
            ("POST", r"/api/v2/temp-rename", self.rename_temp),
            ("POST", r"/api/v2/plans", self.add_plan),
            ("GET", r"/api/v2/structure", self.structure),
            ("POST", r"/api/v2/structure", self.edit_structure),
            ("POST", r"/api/v2/structure/needs", self.structure_needs),
            ("GET", r"/api/v2/changes", self.changes),
            ("GET", r"/api/v2/own", self.own),
            ("PUT", r"/api/v2/own", self.put_own),
            ("POST", r"/api/v2/changes/drop-deep", self.drop_deep),
            ("GET", r"/api/v2/temp-items", self.temp_items),
            ("PUT", r"/api/v2/temp-items", self.put_temp_items),
            ("GET", r"/api/v2/conditions", self.conditions),
            ("PUT", r"/api/v2/conditions", self.put_conditions),
            ("GET", r"/api/v2/calc", self.calc),
            ("GET", r"/api/v2/where-used", self.where_used),
            ("POST", r"/api/v2/release", self.release),
            ("GET", r"/api/v2/export/study", self.export_study),
            ("GET", r"/api/v2/export/structure", self.export_structure),
            ("GET", r"/api/v2/export/release-drift", self.export_release_drift),
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

    def bom_clear(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """取り込んだ部品表を全部消す。検討は残す。"""
        self.store.clear_bom()
        return self._json({"counts": self.store.bom_counts()})

    def bom_preview(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """送られたファイルの見出しと先頭の行、表ごとの項目と列の当たり（保存した対応づけがあれば saved）。"""
        table, data = preview(body)
        tables = []
        scores: dict[str, int] = {}
        for kind, fields in BOM_FIELDS.items():
            guess, scores[kind] = guess_columns(kind, table.headers, fields)
            tables.append({"id": kind, "label": TABLE_NAMES[kind], "guess": guess,
                           "fields": [{"name": f.name, "label": f.label, "required": f.required} for f in fields],
                           "saved": self.store.find_columns(table.headers, kind)})
        saved = next((t["id"] for t in tables if t["saved"] is not None), None)
        guessed = max(scores, key=lambda k: scores[k])
        return self._json({**data, "tables": tables, "suggested": saved or (guessed if scores[guessed] else None)})

    def bom_import(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """files（filename・content の base64）か、sample: true で見本の CSV を取り込む。入った表だけ入れ替える。

        files の1件に table と columns（項目 → 見出し）があればその対応で読み、決定の時に対応を保存する。無ければ見出しから
        表の種類を当てる。sheet は Excel のシート名。check: true は読んだ件数・警告・数量の食い違いを返すだけで、何も保存しない。
        """
        tables: list[Table] = []
        maps: list[BomMap | None] = []
        if body.get("sample"):
            if not SAMPLES_DIR.is_dir():
                raise ValueError("見本のデータがありません")
            tables = [read_csv(p) for p in sorted(SAMPLES_DIR.glob("*.csv"))]
        else:
            files = body.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError("取り込むファイルを選んでください")
            for f in files:
                table, _, _ = read_upload(f)
                tables.append(table)
                maps.append(chosen_columns(f, table) if f.get("columns") is not None else None)
        version, kinds, warnings, attributes = import_tables(tables, maps)
        if body.get("check"):
            read = {"item": len(version.items), "bom_line": len(version.lines), "cost": len(version.cost_rows),
                    "volume": len(version.volumes)}
            return self._json({"tables": kinds, "warnings": warnings, "counts": {k: read[k] for k in dict.fromkeys(kinds)},
                               "conflicts": [{"parent": c.key[0], "balloon": c.key[1], "child": c.key[2],
                                              "quantities": [_s(x) for x in c.quantities]} for c in version.conflicts]})
        self.store.replace_bom(version, kinds, attributes)
        for table, chosen in zip(tables, maps):
            if chosen is not None:
                self.store.save_columns(table.headers, chosen.kind, chosen.columns)
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
        codes = [c if c in bom.items else nfkc(c).strip() for c in (str(x).strip() for x in _list(body, "codes") if x is not None) if c]
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
        bom = self.store.load_bom()
        codes = [known_code(p, bom.items, name="対象品番") for p in products if p is not None and str(p).strip()]
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
            "releases": self.store.releases(ws.number),  # 確定した版と日時（② の過去の版）
            "plans": ws.plan_ids(),
            "targets": ws.targets,
            "problems": problem_count(ws),
            "variations": variation_bases(ws),  # バリエーション → 直下に持つ製品
        })

    # ③ 対象品番 -------------------------------------------------------------

    def targets(self, params: dict[str, str]) -> "Response":
        ws = self._ws(params)
        return self._json({"targets": ws.targets, "main_product": ws.main_product, "variations": variation_bases(ws)})

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

    # 全階層の部品（⑧の［一括置換］の全階層: 変えた後の構成の部品を全階層で並べ、選んだ箇所を置き換える） -------------------

    def after_parts(self, params: dict[str, str]) -> "Response":
        ws = self._ws(params)
        items = ws.bom.items
        inside, tops = inside_rows(ws, self._plan(params))
        return self._json({
            # 組立品の中（全階層）。path は直下の組立品から部品の親まで・tops は直下の組立品 → 使う箇所
            "inside": [{"path": r.path, "part": r.part, "name": items[r.part].name if r.part in items else None,
                        "lines": [{"balloon": x.balloon, "quantity": _s(x.quantity), "was": x.was} for x in r.lines]} for r in inside],
            "tops": {code: [{"product": p, "balloon": b} for p, b in places] for code, places in tops.items()},
            "bases": {t.code: t.base for t in ws.temps if t.base is not None and t.code in ws.temp_merged},
            "places": [{"product": p.product, "balloon": p.balloon, "part": p.part, "quantity": _s(p.quantity), "was": p.was,
                        "before": _s(p.before), **({"now": p.nows[0]} if len(p.nows) == 1 else {})} for p in after_places(ws, self._plan(params))]})

    def apply_replace(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """old を new に、対象品番の全製品で置き換える。rows（product・balloon・old・new）があれば、その箇所だけ。"""
        if "rows" in body:
            self.store.apply_places(_number(params), self._plan(params), _objects(body, "rows"))
        else:
            self.store.apply_replace(_number(params), self._plan(params), body.get("old"), body.get("new"))
        return self.after_parts({"number": _number(params), "plan": self._plan(params)})

    def replace_inside(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """組立品の中の置き換え。新しい品番が要る組立品には、numbers か自動の品番を付ける（made）。check: true なら書かずに断るかだけを見る。"""
        number, plan = _number(params), self._plan(params)
        numbers = body.get("numbers")
        result = self._edit_inside(number, plan, _objects(body, "rows"), numbers if isinstance(numbers, dict) else None, body.get("check") is True)
        if body.get("check") is True:
            return self._json(result)
        return self._json({**self.after_parts({"number": number, "plan": plan}).json(), **result})

    def _edit_inside(self, number: str, plan: str, rows: list[dict[str, Any]], numbers: Mapping[str, Any] | None = None,
                     check: bool = False) -> dict[str, Any]:
        """numbers は、自動の品番（needs の suggested。前の形の元品番も読む）→ 付ける品番（画面で聞いた物）。無い物は自動の品番。
        check なら、断る物が無いかを見るだけで書かない。"""
        ws = self.store.workspace(number)
        rebuild = Rebuild(ws, plan, input_edits(ws, rows))
        given = numbers or {}
        made = [{"code": n["code"], "new": str(given.get(n["suggested"]) or given.get(n["code"]) or n["suggested"]).strip(),
                 "name": n["name"], "typed": n["typed"]} for n in rebuild.needs()]
        change = rebuild.change(made)
        if check:
            return {"made": made, "typed": []}
        self.store.save_inside(ws, plan, change.added, change.lines, change.picks)
        # 材料費を打ってある仮品番は、中を替えても金額が変わらない
        return {"made": made, "typed": [t.code for t in ws.temps if t.code in change.lines and t.material is not None]}

    def rename_temp(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """仮品番の品番を替える。使っている所（変更・ほかの仮品番の中）も一緒に替わる。"""
        self.store.rename_temp(_number(params), body.get("code"), body.get("new"))
        return self.after_parts({"number": _number(params), "plan": self._plan(params)})

    # ⑤ 構成一覧・⑧ 構成変更 -------------------------------------------------------

    def structure(self, params: dict[str, str]) -> "Response":
        """図面が同じ対象品番の構成: 製品ごとの直下の箇所・組立品の中の箇所（全階層）・出てくる品番の名称など。

        drawing が無ければ先頭の製品の図面、blank=1 なら図面の無い製品、all=1 なら対象品番の全部。
        kind=product（⑧）ならバリエーションを除く。variation=1 なら対象品番のバリエーションだけを全部出す（⑪バリエーション）。
        """
        ws = self._ws(params)
        plan = self._plan(params)
        variations = variation_bases(ws)
        only_variations = params.get("variation") == "1"
        pool = ([p for p in ws.targets if p in variations] if only_variations
                else [p for p in ws.targets if p not in variations] if params.get("kind") == "product" else ws.targets)
        drawings = {p: ws.attribute(p, "drawing") for p in pool}
        first = next(iter(pool), None)
        drawing = params.get("drawing") or ("" if params.get("blank") == "1" else drawings.get(first or "", ""))
        if params.get("all") == "1" or only_variations:
            drawing = None
        products = [p for p in pool if drawing in (None, drawings[p])]
        found = structure(ws, plan, products)
        short = variation_shortfalls(ws, plan) if variations else {}
        items = ws.bom.items

        def rows(code: str | None, lines: list[Any]) -> list[dict[str, Any]]:
            minus = short.get(code or "", {})
            return [{"balloon": s.balloon, "now": s.now, "part": s.part, "before": _s(s.before), "quantity": _s(s.quantity),
                     **({"lost": True} if s.lost else {}),
                     **({"problem": True} if (code, s.balloon, s.now, s.part) in found.bad or s.part in minus else {}),
                     **({"short": True} if s.part in minus else {})}
                    for s in lines]

        everything = [s for lines in (*found.direct.values(), *found.inside.values()) for s in lines]
        codes = dict.fromkeys([*products, *(c for s in everything for c in (s.now, s.part) if c is not None)])
        return self._json({
            "drawing": drawing, "drawings": [{"drawing": d, "count": n} for d, n in Counter(drawings.values()).items()],
            "products": products, "main_product": ws.main_product,
            "direct": {p: rows(p, lines) for p, lines in found.direct.items()},
            "inside": {code: rows(None, lines) for code, lines in found.inside.items()},
            "bases": {t.code: t.base for t in ws.temps if t.base is not None and t.code in ws.temp_merged},
            "items": {c: {"name": items[c].name, "specification": items[c].specification, "lifecycle": items[c].lifecycle,
                          "temp": c in ws.temp_merged} for c in codes if c in items},
        })

    def edit_structure(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """構成変更の打ち替え。直下の back（現行に戻す）・quantity・replace・add、組立品の中の inside（parent・products・balloon・now・old・new・quantity）の
        順に入れる（後で断られても、前の分は残る）。"""
        number, plan = _number(params), self._plan(params)
        result: dict[str, Any] = {"made": [], "typed": []}
        if back := _objects(body, "back"):
            self.store.back_places(number, plan, back)
        if quantity := _objects(body, "quantity"):
            self.store.save_quantities(number, plan, quantity)
        if places := _objects(body, "replace"):
            self.store.apply_places(number, plan, places)
        if added := _objects(body, "add"):
            self.store.add_places(number, plan, added)
        if inside := _objects(body, "inside"):
            numbers = body.get("numbers")
            result = self._edit_inside(number, plan, expand_parents(self.store.workspace(number), plan, inside),
                                       numbers if isinstance(numbers, dict) else None)
        return self._json(result)

    def structure_needs(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """組立品の中の打ち替え（⑧の窓の inside か、一括置換の全階層の rows＝path の形）で新しく取る品番の候補。
        入れる前に画面で聞くために、何も書かずに返す。fresh が偽の物は、中身の同じ仮品番がもうあり、その品番を使う（聞かない）。"""
        number, plan = _number(params), self._plan(params)
        ws = self.store.workspace(number)
        rows = _objects(body, "rows") if "rows" in body else expand_parents(ws, plan, _objects(body, "inside"))
        rebuild = Rebuild(ws, plan, input_edits(ws, rows))
        temps = {t.code for t in ws.temps}
        return self._json({"needs": [{"code": n["code"], "name": n["name"], "suggested": n["suggested"], "fresh": n["suggested"] not in temps}
                                     for n in rebuild.needs()]})

    def add_plan(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        """［＋］で空の案を足す。足した案の名前（plan）と検討（案の一覧つき）を返す。"""
        plan = self.store.add_plan(_number(params))
        data = self._study_json(self.store.workspace(_number(params))).json()
        return self._json({**data, "plan": plan})

    # ⑥ 変更一覧 -------------------------------------------------------------

    def _change_rows(self, params: dict[str, str]) -> list[dict[str, Any]]:
        ws = self._ws(params)
        plan = self._plan(params)
        product = params.get("product", "").strip()
        rows = []
        for c in check_changes(ws, ws.plan_changes(plan)):
            if not product or c.change.product == product:
                rows.append(_change_row(ws, c))
        # ⑩ で書き換えた製品の費目（品番の欄に費目の名前・親と風船は無し）
        for r in own_rows(ws, plan, outside=True):
            if product and r["product"] != product:
                continue
            for key, v in r["heads"].items():
                if v["after"] is None:
                    continue
                before = v["now"] if v["now"] is not None else (Decimal(0) if key == "processing" else None)
                rows.append({
                    "product": r["product"], "parent": "", "balloon": None, "part": OWN_HEADS[key].value,
                    "name": "製品の費目", "before": _s(v["now"]), "after": _s(v["after"]),
                    "delta": None if before is None else _s(v["after"] - before),
                    "problems": [] if r["product"] in ws.targets else [NOT_TARGET], "source": "own",
                })
        order = {p: i for i, p in enumerate(ws.targets)}
        rows.sort(key=lambda r: (order.get(r["product"], len(order)), r["product"], r["parent"],
                                 r["balloon"] or "", r["part"], r.get("old") or ""))
        return rows

    def changes(self, params: dict[str, str]) -> "Response":
        rows = self._change_rows(params)
        summary = summarize_changes(rows)
        product = params.get("product", "").strip()
        targets = [p for p in self._ws(params).targets if not product or p == product]
        ws = self._ws(params)
        return self._json({"rows": [{k: v for k, v in r.items() if k != "key"} for r in rows],
                           "summary": [_plain(s) for s in summary], "by_product": [_plain(r) for r in changes_by_product(summary, targets)],
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
            "rows": [{"code": t.code, "name": t.name, "specification": t.specification, "material": _s(t.material), "master": t.code not in ws.temp_merged,
                      "base": t.base, "changed": len(temp_changes(ws, t)) if t.base is not None else 0,
                      # 流用元から計算した材料費（材料費を打った行と、分からない時は空）
                      "derived": _s(ws.bom.cost_value(t.code, CostHead.MATERIAL))
                      if t.base is not None and t.material is None and t.code in ws.temp_merged else None}
                     for t in ws.temps],
        })

    def put_temp_items(self, params: dict[str, str], body: dict[str, Any]) -> "Response":
        known = _list(body, "known") if "known" in body else None
        self.store.save_temps(_number(params), _objects(body, "rows"), known)
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
        code = known_code(params.get("code", ""), ws.bom.items)
        inside, outside, deep = where_used(ws.bom, code, ws.targets)
        group = params.get("group", "")
        data: dict[str, Any] = {"code": code, "inside": len(inside),
                                "outside": len(outside), "deep": deep}
        if group in ("inside", "outside"):
            codes = inside if group == "inside" else outside
            data["products"] = [{"code": c, "name": ws.bom.items[c].name if c in ws.bom.items else None}
                                for c in codes]
        return self._json(data)

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
        effects = change_effects(ws, plan, changes)

        def new_name(e: dict[str, Any]) -> Any:
            return settings.word(e["head"]) if e["head"] else e["new_name"]

        sheets: list[tuple[str, list[str], list[list[Any]]]] = [
            ("まとめ", ["項目", "値"], [
                ["検討番号", ws.number], ["版", "仮" if ws.version is None else ws.version],
                ["題目", ws.header.get("title")], ["担当", ws.header.get("owner")],
                ["出力日時", datetime.now().replace(microsecond=0).isoformat(sep=" ")],
                ["対象品番の数", len(ws.targets)], ["不完全", "あり" if incomplete else "なし"],
                *([f"年間の影響（{h.value}）", results[h]["impact"].get(plan)] for h in HEADS.values()),
                *([f"年間の影響の現行比％（{h.value}）", _percent(results[h]["impact"].get(plan), results[h]["base"].get(plan))]
                  for h in HEADS.values()),
                *([f"1台あたりの平均の増減（年間台数で重み付け・{h.value}）", results[h]["weighted"].get(plan)] for h in HEADS.values())]),
            ("金額", ["対象品番", "名称", "費目", "現行", "変更後", "1台あたりの増減", "増減（％）", "年間台数", "年間の影響", "不完全"], money),
            ("変更ごとの影響", ["変更", "元品番", "元の名称", "新品番", "新の名称", "製品の数", "年間の影響", "現行比（％）", "不完全", "要確認"],
             [[e["kind"], e["old"], e["old_name"], e["new"], new_name(e), len(e["products"]), e["impact"],
               _percent(e["impact"], results[Head.MANUFACTURING]["base"].get(plan)), "不完全" if e["incomplete"] else "",
               "・".join(e["problems"])] for e in effects]),
            ("変更ごとの影響（製品別）", ["変更", "元品番", "新品番", "新の名称", "製品", "名称", "1台あたりの増減", "年間台数", "年間の影響"],
             [[e["kind"], e["old"], e["new"], new_name(e), p["product"], p["name"], p["unit"], p["annual"], p["impact"]]
              for e in effects for p in e["products"]]),
            ("構成の変更", ["変更", "元品番", "元の名称", "新品番", "新の名称", "数量", "製品の数", "製品", "要確認"],
             [[s["kind"], s["old"], s["old_name"], s["new"], s["new_name"], s["quantity"], len(s["products"]),
               "・".join(s["products"]), "・".join(s["problems"])] for s in summarize_changes(changes)]),
            ("構成の明細", ["製品", "名称", "変更", "元品番", "元の名称", "新品番", "新の名称", "数量（現行）", "数量（変更後）", "親品番", "風船", "サブ番号",
                       "要確認"],
             [[r["product"], ws.bom.items[r["product"]].name if r["product"] in ws.bom.items else None, r["kind"],
               r["old"], r["old_name"], r["new"], r["new_name"], _num(r["before"]), _num(r["after"]), r["parent"],
               *split_balloon(r["balloon"]), "・".join(r["problems"])]
              for r in changes_by_product(summarize_changes(changes), ws.targets)]),
            ("計算条件", ["種類", "対象", "項目", "値", "備考"], self._condition_rows(ws, plan)),
        ]
        inner = [[t.code, t.name, t.base, s["kind"], s["old"], s["old_name"], s["new"], s["new_name"], *split_balloon(d["balloon"]),
                  _num(d["before"]), _num(d["after"])]
                 for t in ws.temps if t.base is not None and t.code in ws.temp_merged
                 for s in temp_changes(ws, t) for d in s["details"]]
        if inner:
            sheets.insert(-1, ("仮品番の中", ["仮品番", "名称", "仮品番の元品番", "変更", "元品番", "元の名称", "新品番", "新の名称", "風船",
                                          "サブ番号", "数量（現行）", "数量（変更後）"], inner))
        return self._xlsx(f"検討_{ws.number}", sheets)

    def export_structure(self, params: dict[str, str]) -> "Response":
        """対象品番の全部の構成（変えた後・全階層）を1行1箇所で出す。量が多いので、検討の Excel とは別のファイルにする。

        品番は1列にそろえ、数量（変更後）をその箇所のレベルの列（見出しは 1・2・…）に置く。
        """
        ws = self._ws(params)
        found = structure(ws, self._plan(params), list(ws.targets))
        items = ws.bom.items
        nodes: list[dict[str, Any]] = []

        def walk(product: str, parent: str, lines: list[Any], level: int, path: frozenset[str]) -> None:
            for p in lines:
                changed = p.lost or p.now != p.part or p.before != p.quantity
                item = items.get(p.part)
                nodes.append({"product": product, "product_name": items[product].name if product in items else None, "level": level,
                              "parent": parent, "balloon": split_balloon(p.balloon), "part": p.part,
                              "name": item.name if item else None, "spec": item.specification if item else None, "now": p.now,
                              "before": p.before, "after": Decimal(0) if p.lost else p.quantity,
                              "mark": "部品表に無い" if p.lost else "変更" if changed else None})
                if not p.lost and p.part in found.inside and p.part not in path:  # 循環はたどらない
                    walk(product, p.part, found.inside[p.part], level + 1, path | {p.part})

        for product in ws.targets:
            walk(product, product, found.direct.get(product, []), 1, frozenset({product}))
        depth = max((n["level"] for n in nodes), default=1)
        levels = [str(i) for i in range(1, depth + 1)]
        rows = [[n["product"], n["product_name"], n["level"], n["parent"], *n["balloon"], n["part"], n["name"], n["spec"],
                 n["now"], n["before"], *(n["after"] if i == n["level"] else None for i in range(1, depth + 1)), n["mark"]]
                for n in nodes]
        # 品目: 構成に出てくる品番を1行に1つ（出てきた順）。材料費と製造原価は1個あたり（画面の品番の右の列と同じ求め方）
        calc = CostCalculator(ws.bom, CostHead.MATERIAL)
        parts: list[list[Any]] = []
        for code in dict.fromkeys(n["part"] for n in nodes):
            item = items.get(code)
            try:
                material = calc.item_cost(code) if item else None
            except ValueError:
                material = None
            manufacturing = (breakdown(material, registered_own(ws.bom, code), rates=ws.rates).value(CostHead.MANUFACTURING)
                             if material is not None else None)
            parts.append([code, item.name if item else None, item.specification if item else None, material, manufacturing])
        return self._xlsx(f"構成_{ws.number}", [
            ("構成", ["製品", "製品の名称", "レベル", "親品番", "風船", "サブ番号", "品番", "名称", "仕様", "現行の品番",
                    "数量（現行）", *levels, "変更"], rows),
            ("品目", ["品番", "名称", "仕様", "材料費", "製造原価"], parts),
        ])

    def export_release_drift(self, params: dict[str, str]) -> "Response":
        """確定した版の時と今の部品表で、原価・構成・年間台数が違う所だけを Excel に出す。違いが無ければ same を返す。"""
        ws = self._ws(params)
        if ws.version is None:
            raise ValueError("比べる版を選んでください")
        drift = release_drift(ws, self.store.load_bom())
        if not drift:
            return self._json({"same": True})
        then = f"版{ws.version}の時"
        sheets = [
            ("原価の違い", ["品番", "名称", "費目", then, "今", "差"], drift.costs),
            ("構成の違い", ["親品番", "風船", "サブ番号", "子", "名称", f"数量（{then}）", "数量（今）"],
             [[r[0], *split_balloon(r[1]), *r[2:]] for r in drift.lines]),
            ("年間台数の違い", ["製品", "名称", then, "今"], drift.volumes),
        ]
        return self._xlsx(f"部品表の違い_{ws.number}_版{ws.version}", [s for s in sheets if s[2]])  # 違いの無いシートは出さない

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
            note = "" if t.base is None else f"元品番 {t.base}" + ("（材料費は入力した値）" if t.material is not None else " から計算")
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
            for at, values in enumerate((header, *rows), 1):
                values = [ILLEGAL.sub("", v) if isinstance(v, str) else v for v in values]
                sheet.append(values)
                # 「=」で始まる文字を式にしない。sheet[sheet.max_row] は呼ぶたびに全セルを数え直す（行の数の2乗で遅い）ので、番号で指す
                for col, v in enumerate(values, 1):
                    if isinstance(v, str):
                        sheet.cell(row=at, column=col).data_type = "s"
            sheet.freeze_panes = "A2"
        data = save_workbook(wb, {"まとめ": [1], "金額": [3], "計算条件": [1, 3]})
        return Response(200, data, XLSX_TYPE,
                        {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.xlsx"})



def _percent(diff: Decimal | None, now: Decimal | None) -> Decimal | None:
    """今に対する差の割合（％・小数2桁）。今が無いか 0 なら None。"""
    return None if diff is None or not now else (diff / now * 100).quantize(Decimal("0.01"))


def _num(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _change_row(ws: Workspace, c: Checked) -> dict[str, Any]:
    """変えた箇所1つの行。old は現行の品番（足した箇所は無し）、new は変えた後の品番（数量だけの変更と、消した箇所は無し）。"""
    s, ch = c.change.slot, c.change
    new = s.part if c.kind in (REPLACED, ADDED) else None
    return _row(ws, c.kind, ch.product, ch.parent, s.balloon, c.now, new,
                "0" if c.kind == ADDED else None if c.before is None else _s(c.before), _s(s.quantity), list(c.problems), ch.key)


def _row(ws: Workspace, kind: str, product: str, parent: str, balloon: str | None, old: str | None, new: str | None, before: str | None,
         after: str | None, problems: list[str], key: Any) -> dict[str, Any]:
    items = ws.bom.items
    # 「バリエーション」は設定の語に読み替える（印の中の語なので、語の読み替えだけでは替わらない）
    worded = [p.replace("バリエーション", settings.word("バリエーション")) if p == SHORT else p for p in problems]
    return {"kind": kind, "product": product, "parent": parent, "balloon": balloon, "part": new or old,
            "old": old, "old_name": items[old].name if old in items else None, "new": new, "new_name": items[new].name if new in items else None,
            "before": before, "after": after, "problems": worded, "source": "slot", "key": key}


def _plain(s: dict[str, Any]) -> dict[str, Any]:
    """画面に送る形（中で使う鍵を除く）。"""
    out = {k: v for k, v in s.items() if k != "key"}
    if "details" in out:
        out["details"] = [{k: v for k, v in d.items() if k != "key"} for d in out["details"]]
    return out


def summarize_changes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """変えた箇所の行を、した事（置き換え・数量・追加・削除）ごとに1行へまとめる。

    操作の記録は使わず、箇所ごとの現行と変えた後だけから作る。details に製品ごとの箇所を持つ。製品の費目（金額）の行は入れない。
    """
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        if r["source"] == "own":
            continue
        g = groups.setdefault((r["kind"], r["old"], r["new"], tuple(r["problems"])), {
            **{k: r[k] for k in ("kind", "old", "old_name", "new", "new_name", "problems")}, "products": [], "details": []})
        if r["product"] not in g["products"]:
            g["products"].append(r["product"])
        g["details"].append({k: r[k] for k in ("product", "parent", "balloon", "before", "after", "key")})
    for g in groups.values():
        texts: dict[str, set[str]] = {}
        for d in g["details"]:
            same = g["kind"] == REPLACED and d["before"] == d["after"]
            d["quantity"] = d["after"] if same or g["kind"] == ADDED else f"{d['before'] or '—'} → {d['after']}"
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
    follow = followers(ws)  # 元の製品の直下の変更は、それを直下に持つバリエーションにも効く
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
        units: dict[str, Decimal | None] = {}
        unknown = waiting = False
        for d in g["details"]:
            effect = material[d["key"]]
            before = units.setdefault(d["product"], None)
            unknown |= effect.missing or effect.value is None
            waiting |= effect.missing and effect.value is None
            if effect.value is not None:
                units[d["product"]] = (before or Decimal(0)) + effect.value * rates.factor
            if d["key"][1] == d["key"][2]:
                for v, n in follow.get(d["product"], ()):
                    held = units.setdefault(v, None)
                    if effect.value is not None:
                        units[v] = (held or Decimal(0)) + effect.value * n * rates.factor
        add(None, {**g, "problems": [*g["problems"], *([WAITING] if waiting else [])]}, units, unknown)
    prices = Prices(ws.bom)
    for key, head in OWN_HEADS.items():
        units = {}
        for r in own_rows(ws, plan):
            v = r["heads"][key]
            if v["after"] is not None:
                diff = v["after"] - (v["now"] or Decimal(0))
                # 現行の値が出ない製品は、変えた後の値も出ない（結果の年間の影響に入らない）
                units[r["product"]] = None if prices.of(r["product"]).value is None else diff if head is Head.DIRECT else diff * rates.factor
        if units:
            add(head.value, {"kind": "製品の費目"}, units)
    return sorted(out, key=lambda e: (e["impact"] is None, -abs(e["impact"] or 0), -abs(e["unit_max"] or 0)))


def temp_changes(ws: Workspace, temp: Temp) -> list[dict[str, Any]]:
    """流用した仮品番の中の変更を、した事ごとに1行（流用元からの差）。"""
    rows = []
    for p in temp_placed(ws.bom, temp):
        if p.slot is None or (p.now == p.part and p.before == p.quantity):
            continue
        kind = kind_of(p.now, p.part, p.quantity)
        problems = ([NO_PLACE] if p.lost else []) + ([] if p.part in ws.bom.items else [UNREGISTERED])
        rows.append(_row(ws, kind, temp.code, temp.base or "", p.balloon, p.now, p.part if kind in (REPLACED, ADDED) else None,
                         "0" if kind == ADDED else None if p.before is None else _s(p.before), _s(p.quantity), problems, None))
    return [_plain(s) for s in summarize_changes(rows)]


def changes_by_product(summary: list[dict[str, Any]], targets: list[str]) -> list[dict[str, Any]]:
    """した事のまとめを製品ごとに並べ直す（対象品番の順・製品の中は風船番号の順で、無い行は最後に品番の順・
    変更の無い対象品番は「変更なし」・対象品番の外の製品は後ろ）。"""
    names = ("kind", "old", "old_name", "new", "new_name", "problems")
    found: dict[str, list[dict[str, Any]]] = {}
    for s in summary:
        for d in s["details"]:
            found.setdefault(d["product"], []).append({**{k: s[k] for k in names}, **d})
    for rows in found.values():
        rows.sort(key=lambda r: (_balloon_order(r["balloon"]), r["old"] or r["new"]))
    none = {"kind": "変更なし", "old": None, "old_name": None, "new": None, "new_name": None, "problems": [],
            "parent": None, "balloon": None, "before": None, "after": None, "quantity": ""}
    products = [*targets, *(p for p in found if p not in targets)]
    return [r for p in products for r in found.get(p, [{**none, "product": p}])]


def study_routes(api: "Api") -> list[tuple[str, str, Callable[..., "Response"]]]:
    return StudyApi(api).routes()
