"""画面の2段目の API: 取り込みと列の対応づけ・版の一覧と削除・手入力・部品から見る・モデルチェンジの比較。

経路は Api の _routes に足す（screen_routes）。入力の誤りは ApiError（400 番台・日本語）で返す。
金額と数量は Decimal の文字列で返し、計算は計算の層（mold・modelchange・rollup）に任せる。
"""

import base64
import binascii
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from .api import HEADS, ApiError, Response, _head, _json_response, _known_codes, _s, _text_field
from .importer import (
    TABLE_FIELDS,
    ColumnMap,
    MissingColumnsError,
    Table,
    VersionImport,
    read_table_bytes,
    xlsx_sheet_names,
)
from .model import MANUAL_VERSION_ID, CostHead, CostRow, ImportWarning, Item, Version, Volume
from .modelchange import compare_structures
from .mold import Allocation, MoldCost, ReuseTarget, allocate_mold_cost, part_users, reuse_impact
from .normalize import to_code, to_decimal, to_optional_code
from .rollup import calculate_cost
from .scenario import Scenario

if TYPE_CHECKING:
    from .api import Api

# 1万行・列の多い CSV が入る大きさ。送る形は base64 なので、要求の本文はこの 4/3 倍になる
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
TABLE_LABELS = {
    "item": "部品マスタ（品目）",
    "bom_line": "構成マスタ（構成行）",
    "cost": "原価",
    "volume": "年間台数",
}
SAMPLE_ROWS = 5


def _optional_number(value: Any, name: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    return _required_number(value, name)


def _required_number(value: Any, name: str) -> Decimal:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise ApiError(400, f"{name}が空")
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ApiError(400, f"{name}を数として読めない: {value!r}")
    try:
        return to_decimal(value, name=name)
    except (ValueError, TypeError):
        raise ApiError(400, f"{name}を数として読めない: {value!r}") from None


def _param(params: dict[str, str], key: str, name: str) -> str:
    value = (params.get(key) or "").strip()
    if not value:
        raise ApiError(400, f"{name}（{key}）が空")
    return value


def _optional_text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ApiError(400, f"{key} は文字で送る")
    return str(value).strip()


def _allocation(value: Any) -> Allocation:
    if value is None or value == "":
        return Allocation.BY_USAGE
    try:
        return Allocation[str(value)]
    except KeyError:
        raise ApiError(400, f"載せ方が分からない: {value}（{'・'.join(a.name for a in Allocation)}）") from None


def _scenario_id(value: str) -> int:
    if not value.isdigit():
        raise ApiError(400, f"試算の ID が数でない: {value}")
    return int(value)


def _cell(value: Any) -> str:
    return "" if value is None else str(value)


def _warning_json(w: ImportWarning) -> dict[str, Any]:
    return {"file": w.file, "row": w.row, "message": w.message}


def _manual_copy(
    manual: Version, drop_item: str | None = None, drop_volume: str | None = None
) -> Version:
    """manual の中身を写す。drop_item の品目と原価、drop_volume の台数は写さない。"""
    v = Version(MANUAL_VERSION_ID)
    v.add_items(i for code, i in manual.items.items() if code != drop_item)
    v.add_bom_lines(manual.lines)
    for conflict in manual.conflicts:
        v.add_conflict(conflict.key, conflict.quantities)
    v.add_costs(r for r in manual.cost_rows if r.code != drop_item)
    for product, volume in manual.volumes.items():
        if product != drop_volume:
            v.add_volume(volume)
    return v


class Screens:
    """2段目の画面の API の中身。api は Api（保存と版・試算の読み込みを借りる）。"""

    def __init__(self, api: "Api") -> None:
        self.api = api
        self.store = api.store

    def routes(self) -> list[tuple[str, str, Callable[..., Response]]]:
        return [
            ("POST", r"/api/import/preview", self.import_preview),
            ("POST", r"/api/import", self.import_files),
            ("GET", r"/api/versions/info", self.version_info),
            ("DELETE", r"/api/versions", self.delete_version),
            ("GET", r"/api/manual", self.manual),
            ("PUT", r"/api/manual/items", self.put_manual_item),
            ("DELETE", r"/api/manual/items", self.delete_manual_item),
            ("PUT", r"/api/manual/volumes", self.put_volume),
            ("DELETE", r"/api/manual/volumes", self.delete_volume),
            ("PUT", r"/api/molds", self.put_mold),
            ("DELETE", r"/api/molds", self.delete_mold),
            ("PUT", r"/api/reuse", self.put_reuse),
            ("DELETE", r"/api/reuse", self.delete_reuse),
            ("GET", r"/api/partmaps", self.part_maps),
            ("POST", r"/api/partmaps", self.add_part_map),
            ("PUT", r"/api/partmaps", self.update_part_map),
            ("DELETE", r"/api/partmaps", self.delete_part_map),
            ("GET", r"/api/parts/view", self.part_view),
            ("GET", r"/api/modelchange", self.model_change),
        ]

    # 取り込み ----------------------------------------------------------------

    def _read_upload(self, f: Any) -> tuple[Table, str, bytes]:
        if not isinstance(f, dict):
            raise ApiError(400, "ファイルの指定はオブジェクトで送る")
        name = _text_field(f, "filename", "ファイル名")
        content = f.get("content")
        if not isinstance(content, str) or not content:
            raise ApiError(400, f"{name} の中身が空")
        if len(content) > MAX_UPLOAD_BYTES * 4 // 3 + 4:
            raise ApiError(413, f"{name} が大きすぎる（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB）")
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(400, f"{name} の中身を base64 として読めない") from None
        sheet = _optional_text(f, "sheet") or None
        try:
            table = read_table_bytes(name, data, sheet)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        except Exception:  # 壊れた .xlsx は zipfile などの例外で落ちるので、まとめて読めないとする
            raise ApiError(400, f"{name} を Excel のファイルとして読めない") from None
        return table, name, data

    def _tables_meta(self, headers: tuple[str, ...]) -> list[dict[str, Any]]:
        result = []
        for table_id, fields in TABLE_FIELDS.items():
            saved = self.store.find_column_map(headers, table_id)
            result.append(
                {
                    "id": table_id,
                    "label": TABLE_LABELS[table_id],
                    "fields": [{"name": f.name, "label": f.label, "required": f.required} for f in fields],
                    "saved": None if saved is None else dict(saved.columns),
                }
            )
        return result

    def import_preview(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """送られたファイルの見出しと先頭の行、表ごとの保存済みの対応づけ（あれば）を返す。"""
        table, name, data = self._read_upload(body)
        sheets: list[str] = []
        if name.lower().endswith((".xlsx", ".xlsm")):
            sheets = list(xlsx_sheet_names(data))
        tables = self._tables_meta(table.headers)
        return _json_response(
            {
                "filename": name,
                "sheets": sheets,
                "sheet": _optional_text(body, "sheet") or (sheets[0] if sheets else None),
                "headers": list(table.headers),
                "row_count": len(table.rows),
                "sample": [[_cell(v) for v in row] for row in table.rows[:SAMPLE_ROWS]],
                "tables": tables,
                "suggested": next((t["id"] for t in tables if t["saved"] is not None), None),
            }
        )

    def _column_map(self, f: dict[str, Any], table: Table) -> ColumnMap:
        table_id = _optional_text(f, "table")
        columns = f.get("columns")
        if table_id and table_id not in TABLE_FIELDS:
            raise ApiError(400, f"表が分からない: {table_id}（{'・'.join(TABLE_FIELDS)}）")
        if columns is None:
            saved = self.store.find_column_map(table.headers, table_id or None)
            if saved is None:
                raise ApiError(400, f"{table.name} の列の対応づけが無い（同じ見出しのファイルで保存した対応づけも無い）")
            return saved
        if not table_id:
            raise ApiError(400, f"{table.name} の表（table）が空")
        if not isinstance(columns, dict):
            raise ApiError(400, "列の対応づけ（columns）は 項目 → 見出し のオブジェクトで送る")
        chosen = {str(k): str(v).strip() for k, v in columns.items() if v not in (None, "")}
        for header in chosen.values():
            if header not in table.headers:
                raise ApiError(400, f"列「{header}」は {table.name} の見出しに無い")
        try:
            return ColumnMap(table_id, table.headers, chosen)
        except MissingColumnsError as exc:
            raise ApiError(
                400, f"{table.name}: {TABLE_LABELS[table_id]}の必須の列が対応づいていない: {'・'.join(exc.missing)}"
            ) from None

    def import_files(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """何本かのファイルを1つの版として取り込み、使った列の対応づけを保存する。"""
        version_id = _text_field(body, "version_id", "版の ID")
        if version_id == MANUAL_VERSION_ID:
            raise ApiError(400, f"{MANUAL_VERSION_ID} は手入力の版の名前なので使えない")
        if any(v.version_id == version_id for v in self.store.list_versions()):
            raise ApiError(409, f"版 {version_id} はもうある（別の ID にするか、先に版を消す）")
        files = body.get("files")
        if not isinstance(files, list) or not files:
            raise ApiError(400, "取り込むファイル（files）が空")
        imp = VersionImport(Version(version_id, imported_at=datetime.now().replace(microsecond=0),
                                    note=_optional_text(body, "note")))
        maps: list[ColumnMap] = []
        used: list[dict[str, Any]] = []
        for f in files:
            table, name, _ = self._read_upload(f)
            column_map = self._column_map(f, table)
            imp.add_table(table, column_map)
            maps.append(column_map)
            used.append({"filename": name, "table": column_map.table, "rows": len(table.rows),
                         "columns": dict(column_map.columns)})
            if not table.rows:
                imp.warnings.append(ImportWarning(name, None, "読める行が無い"))
        warnings = list(imp.warnings)
        for conflict in imp.version.conflicts:
            parent, balloon, child = conflict.key
            quantities = "・".join(_s(q) for q in conflict.quantities)
            warnings.append(ImportWarning(
                "", None,
                f"構成の数量が食い違う: 親 {parent}・風船番号 {balloon or '空欄'}・子 {child}（{quantities}。計算には最初の数量を使う）",
            ))
        self.store.save_version(imp.version, warnings=warnings)
        for column_map in maps:
            self.store.save_column_map(column_map)
        v = imp.version
        return _json_response(
            {
                "version_id": version_id,
                "source_name": v.source_name,
                "files": used,
                "counts": {"items": len(v.items), "lines": len(v.lines), "costs": len(v.cost_rows),
                           "volumes": len(v.volumes)},
                "warnings": [_warning_json(w) for w in warnings],
                "warning_count": len(warnings),
            },
            201,
        )

    def version_info(self, params: dict[str, str]) -> Response:
        version_id = _param(params, "version", "版")
        info = next((v for v in self.store.list_versions() if v.version_id == version_id), None)
        if info is None:
            raise ApiError(404, f"版 {version_id} は無い")
        return _json_response(
            {
                "version_id": info.version_id,
                "imported_at": None if info.imported_at is None else info.imported_at.isoformat(),
                "source_name": info.source_name,
                "note": info.note,
                "warning_count": info.warning_count,
                "warnings": [_warning_json(w) for w in self.store.version_warnings(version_id)],
                "used_by": [s.name for s in self.store.list_scenarios() if s.version_id == version_id],
            }
        )

    def delete_version(self, params: dict[str, str]) -> Response:
        """試算が使っている版は VersionInUseError（400）で消せない。"""
        self.store.delete_version(_param(params, "version", "版"))
        return self.api.versions({})

    # 手入力 ------------------------------------------------------------------

    def manual(self, params: dict[str, str]) -> Response:
        manual = self.store.load_manual()
        codes = list(manual.items) + [r.code for r in manual.cost_rows if r.code not in manual.items]
        items = []
        for code in codes:
            item = manual.items.get(code)
            items.append(
                {
                    "code": code,
                    "name": "" if item is None else item.name,
                    "kind": "" if item is None else item.kind,
                    "costs": {h.name: _s(manual.cost_value(code, h)) for h in HEADS},
                }
            )
        return _json_response(
            {
                "heads": [{"id": h.name, "label": h.value} for h in HEADS],
                "items": items,
                "volumes": [{"product": v.product, "annual": _s(v.annual), "kind": v.kind.value}
                            for v in manual.volumes.values()],
                "molds": [{"part": m.part, "amount": _s(m.amount), "years": _s(m.years),
                           "annual_amount": _s(m.annual_amount)} for m in self.store.mold_costs().values()],
                "reuse": [{"part": t.part, "product": t.product, "quantity": _s(t.quantity_per_unit),
                           "replaced_part": t.replaced_part} for t in self.store.reuse_targets()],
                "part_maps": self._part_map_rows(),
                "allocations": [{"id": a.name, "label": a.value} for a in Allocation],
            }
        )

    def put_manual_item(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """試作品の品目と原価を足す・直す（同じ品番は入れ替え）。手入力の版 manual に入る。"""
        code = to_code(_text_field(body, "code", "品番"))
        name = _text_field(body, "name", "名称")
        costs = body.get("costs") or {}
        if not isinstance(costs, dict):
            raise ApiError(400, "原価（costs）は 費目 → 金額 のオブジェクトで送る")
        values: dict[CostHead, Decimal | None] = {}
        for key, value in costs.items():
            try:
                head = CostHead[str(key)]
            except KeyError:
                raise ApiError(400, f"費目が分からない: {key}") from None
            values[head] = _optional_number(value, head.value)
        manual = self.store.load_manual()
        kept = manual.items.get(code)
        result = _manual_copy(manual, drop_item=code)
        result.add_item(Item(code, name, _optional_text(body, "kind"),
                             "" if kept is None else kept.lifecycle,
                             False if kept is None else kept.has_children))
        if any(v is not None for v in values.values()):
            result.add_cost(CostRow(code, values))
        self.store.save_manual(result)
        return self.manual({})

    def delete_manual_item(self, params: dict[str, str]) -> Response:
        code = to_code(_param(params, "code", "品番"))
        manual = self.store.load_manual()
        if code not in manual.items and manual.cost_row(code) is None:
            raise ApiError(404, f"手入力の品目 {code} は無い")
        self.store.save_manual(_manual_copy(manual, drop_item=code))
        return self.manual({})

    def put_volume(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """新製品の年間台数（計画）を足す・直す。"""
        product = to_code(_text_field(body, "product", "製品の品番"))
        annual = _required_number(body.get("annual"), "年間台数")
        if annual < 0:
            raise ApiError(400, "年間台数は 0 以上にする")
        manual = self.store.load_manual()
        result = _manual_copy(manual, drop_volume=product)
        result.add_volume(Volume(product, annual))
        self.store.save_manual(result)
        return self.manual({})

    def delete_volume(self, params: dict[str, str]) -> Response:
        product = to_code(_param(params, "product", "製品の品番"))
        manual = self.store.load_manual()
        if product not in manual.volumes:
            raise ApiError(404, f"手入力の年間台数 {product} は無い")
        self.store.save_manual(_manual_copy(manual, drop_volume=product))
        return self.manual({})

    def put_mold(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        part = to_code(_text_field(body, "part", "部品の品番"))
        amount = _required_number(body.get("amount"), "型費")
        years = _required_number(body.get("years", "2"), "償却年数")
        if amount < 0:
            raise ApiError(400, "型費は 0 以上にする")
        if years <= 0:
            raise ApiError(400, "償却年数は正の数にする")
        self.store.save_mold_cost(MoldCost(part, amount, years))
        return self.manual({})

    def delete_mold(self, params: dict[str, str]) -> Response:
        part = to_code(_param(params, "part", "部品の品番"))
        if part not in self.store.mold_costs():
            raise ApiError(404, f"型費 {part} は無い")
        self.store.delete_mold_cost(part)
        return self.manual({})

    def put_reuse(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """流用先を足す・直す。同じ部品と流用先の組は入れ替える。original があればその組を消してから足す。"""
        part = to_code(_text_field(body, "part", "部品の品番"))
        product = to_code(_text_field(body, "product", "流用先の品番"))
        quantity = _required_number(body.get("quantity"), "使用数")
        if quantity <= 0:
            raise ApiError(400, "使用数は正の数にする")
        replaced = _optional_text(body, "replaced_part") or None
        target = ReuseTarget(part, product, quantity, replaced)
        original = body.get("original")
        if isinstance(original, dict) and original.get("part") and original.get("product"):
            self.store.delete_reuse_target(ReuseTarget(original["part"], original["product"], 1))
        self.store.delete_reuse_target(target)
        self.store.add_reuse_target(target)
        return self.manual({})

    def delete_reuse(self, params: dict[str, str]) -> Response:
        target = ReuseTarget(_param(params, "part", "部品の品番"), _param(params, "product", "流用先の品番"), 1)
        if not any(t.product == target.product for t in self.store.reuse_targets(target.part)):
            raise ApiError(404, f"流用先 {target.part} → {target.product} は無い")
        self.store.delete_reuse_target(target)
        return self.manual({})

    # 部品の対応表 ------------------------------------------------------------

    def _part_map_rows(self, pair: tuple[str, str] | None = None) -> list[dict[str, Any]]:
        pairs = [pair] if pair is not None else self.store.part_map_pairs()
        return [
            {"old_product": old, "new_product": new, "old_part": r.old_part, "new_part": r.new_part,
             "old_balloon": r.old_balloon, "new_balloon": r.new_balloon}
            for old, new in pairs
            for r in self.store.part_map_rows(old, new)
        ]

    @staticmethod
    def _part_map_key(data: dict[str, Any]) -> tuple[str, str, str, str, str | None, str | None]:
        return (
            to_code(_text_field(data, "old_product", "元の製品の品番")),
            to_code(_text_field(data, "new_product", "新しい製品の品番")),
            to_code(_text_field(data, "old_part", "元の部品の品番")),
            to_code(_text_field(data, "new_part", "新しい部品の品番")),
            to_optional_code(_optional_text(data, "old_balloon") or None, name="風船番号"),
            to_optional_code(_optional_text(data, "new_balloon") or None, name="風船番号"),
        )

    def part_maps(self, params: dict[str, str]) -> Response:
        old, new = params.get("old_product"), params.get("new_product")
        pair = (to_code(old), to_code(new)) if old and new else None
        return _json_response(self._part_map_rows(pair))

    def add_part_map(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        key = self._part_map_key(body)
        if key[0] == key[1]:
            raise ApiError(400, "元の製品と新しい製品は別の品番にする")
        if key[2] == key[3]:
            raise ApiError(400, "同じ品番どうしは対応表に入れなくても自動で対応する")
        self.store.add_part_map(*key)
        return _json_response(self._part_map_rows(), 201)

    def update_part_map(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        original = body.get("original")
        if not isinstance(original, dict):
            raise ApiError(400, "直す元の行（original）が空")
        old_key = self._part_map_key(original)
        new_key = self._part_map_key(body)
        if new_key[2] == new_key[3]:
            raise ApiError(400, "同じ品番どうしは対応表に入れなくても自動で対応する")
        if self.store.delete_part_map(*old_key) == 0:
            raise ApiError(404, "直す元の対応表の行が無い")
        self.store.add_part_map(*new_key)
        return _json_response(self._part_map_rows())

    def delete_part_map(self, params: dict[str, str]) -> Response:
        if self.store.delete_part_map(*self._part_map_key(dict(params))) == 0:
            raise ApiError(404, "対応表の行が無い")
        return _json_response(self._part_map_rows())

    # 部品から見る ------------------------------------------------------------

    def part_view(self, params: dict[str, str]) -> Response:
        """部品を使う製品・流用先と、型費の載り方・原価の今と後（費目5つ）。設計書 6章の図4。

        scenario を選ぶと、その試算の製品は「土台＋変更」の仮の構成で使用数を数える。
        head は主に見る費目（既定は材料費）。費目5つの値は heads に並べる。
        """
        part = to_code(_param(params, "part", "部品の品番"))
        allocation = _allocation(params.get("allocation"))
        head = _head(params.get("head"), CostHead.MATERIAL)
        scenario: Scenario | None = None
        if params.get("scenario"):
            scenario = self.api._load_scenario(_scenario_id(params["scenario"]))
        version_id = params.get("version") or (scenario.version.version_id if scenario else "")
        if not version_id:
            raise ApiError(400, "版（version）が空")
        version = self.api._load_version(version_id)
        if part not in _known_codes(version):
            raise ApiError(400, f"品番 {part} は版 {version_id} に無い")
        policy = self.store.load_scope_policy()
        overrides = scenario.overrides() if scenario is not None else None
        reuse = self.store.reuse_targets(part)
        mold = self.store.mold_costs().get(part)
        users, unknown = part_users(version, part, reuse, overrides)
        alloc = None if mold is None else allocate_mold_cost(version, mold, allocation, reuse, overrides)
        costs: dict[tuple[str, CostHead], Decimal | None] = {}

        def cost(code: str, h: CostHead) -> Decimal | None:
            if (code, h) not in costs:
                result = calculate_cost(version, code, h, policy, overrides)
                # 足し上げた範囲に登録値が1つも無い品目は 0 でなく未登録にする
                costs[(code, h)] = None if result.missing and result.total == 0 else result.total
            return costs[(code, h)]

        rows = []
        for user in users:
            share = None if alloc is None else alloc.share_for(user.product)
            mold_unit = None if share is None else share.per_unit
            target = next((t for t in reuse if t.product == user.product), None) if user.via_reuse else None
            is_trial = scenario is not None and user.product == scenario.product_code
            # 型費は償却費なので、費目ごとの今と後には足さず償却費の欄に分けて出す
            # （型費の登録が無い部品は償却費の分を 0 とする）
            mold_add = Decimal(0) if alloc is None else mold_unit
            per_head: dict[str, dict[str, str | None]] = {}
            for h in HEADS:
                if target is not None:
                    impact = reuse_impact(version, target, alloc, h, policy)
                    current, part_diff = impact.current_cost, impact.part_difference
                    after = None if current is None or part_diff is None else current + part_diff
                else:
                    # BOM で使う製品の原価はもう部品を含むので、部品の差は無い
                    now = cost(user.product, h)
                    current = None if is_trial else now
                    part_diff = None
                    after = now
                difference = None if current is None or after is None else after - current
                total = None if difference is None or mold_add is None else difference + mold_add
                per_head[h.name] = {"current": _s(current), "part_difference": _s(part_diff),
                                    "after": _s(after), "difference": _s(difference),
                                    "difference_with_amortization": _s(total)}
            item = version.items.get(user.product)
            name = scenario.name if is_trial and item is None else ("" if item is None else item.name)
            rows.append(
                {
                    "product": user.product,
                    "name": name,
                    "via": "流用先" if user.via_reuse else ("試算の構成" if is_trial else "BOM"),
                    "volume_kind": user.volume_kind.value,
                    "annual": _s(user.annual),
                    "quantity_per_unit": _s(user.quantity_per_unit),
                    "annual_count": _s(user.annual_count),
                    "mold_per_unit": _s(mold_unit),
                    "replaced_part": None if target is None else target.replaced_part,
                    **per_head[head.name],
                    "heads": per_head,
                }
            )
        part_item = version.items.get(part)
        return _json_response(
            {
                "part": part,
                "name": "" if part_item is None else part_item.name,
                "part_costs": {h.name: _s(cost(part, h)) for h in HEADS},
                "mold": None if mold is None else {"amount": _s(mold.amount), "years": _s(mold.years),
                                                   "annual_amount": _s(mold.annual_amount)},
                "allocation": allocation.name,
                "allocation_label": allocation.value,
                "allocations": [{"id": a.name, "label": a.value} for a in Allocation],
                "heads": [{"id": h.name, "label": h.value} for h in HEADS],
                "annual_count": _s(sum((u.annual_count for u in users), Decimal(0))),
                "per_part": None if alloc is None else _s(alloc.per_part),
                "rows": rows,
                "unknown_volume": list(unknown),
                "reuse": [{"product": t.product, "quantity": _s(t.quantity_per_unit),
                           "replaced_part": t.replaced_part} for t in reuse],
                "premises": {
                    "version_id": version.version_id,
                    "head": head.name,
                    "head_label": head.value,
                    "allocation": allocation.name,
                    "allocation_label": allocation.value,
                    "volumes": [{"product": u.product, "kind": u.volume_kind.value} for u in users],
                    "scenario_id": None if scenario is None else scenario.scenario_id,
                    "scenario_name": None if scenario is None else scenario.name,
                    "reference": "参考値",
                    "amortization_excluded": True,
                },
            }
        )

    # モデルチェンジ ----------------------------------------------------------

    def model_change(self, params: dict[str, str]) -> Response:
        """元の製品と新製品（または試算）の構成を、同じ品番・対応表・片方だけの3段で並べる。設計書 8章。"""
        scenario: Scenario | None = None
        if params.get("scenario"):
            scenario = self.api._load_scenario(_scenario_id(params["scenario"]))
        old_text = params.get("old") or (scenario.base if scenario is not None else "")
        if not old_text:
            raise ApiError(400, "元の製品の品番（old）が空")
        old_product = to_code(old_text)
        old_version_id = params.get("old_version") or (scenario.version.version_id if scenario else "")
        if not old_version_id:
            raise ApiError(400, "元の製品の版（old_version）が空")
        old_version = self.api._load_version(old_version_id)
        if scenario is not None:
            new_product = scenario.product_code
            new_version = self.api._load_version(params.get("new_version") or scenario.version.version_id)
            new_overrides = scenario.overrides()
        else:
            new_product = to_code(_param(params, "new", "新しい製品の品番"))
            new_version = self.api._load_version(params.get("new_version") or old_version_id)
            new_overrides = None
        if old_product == new_product:
            raise ApiError(400, "元の製品と新しい製品は別の品番にする")
        for code, version in ((old_product, old_version), (new_product, new_version)):
            if not (scenario is not None and code == new_product) and not version.children(code):
                raise ApiError(400, f"製品 {code} の構成が版 {version.version_id} に無い")
        direct_only = str(params.get("direct_only", "")).lower() in ("1", "true", "yes")
        mapping = self.store.part_map(old_product, new_product)
        rows = compare_structures(old_version, old_product, new_version, new_product, mapping, direct_only,
                                  new_overrides=new_overrides)

        def name(code: str | None) -> str:
            if code is None:
                return ""
            item = new_version.items.get(code) or old_version.items.get(code)
            return "" if item is None else item.name

        return _json_response(
            {
                "old_product": old_product,
                "old_version": old_version.version_id,
                "new_product": new_product,
                "new_version": new_version.version_id,
                "scenario_id": None if scenario is None else scenario.scenario_id,
                "scenario_name": None if scenario is None else scenario.name,
                "direct_only": direct_only,
                "rows": [
                    {
                        "kind": r.kind.name,
                        "kind_label": r.kind.value,
                        "old_part": r.old_part,
                        "old_name": name(r.old_part),
                        "new_part": r.new_part,
                        "new_name": name(r.new_part),
                        "old_quantity": _s(r.old_quantity),
                        "new_quantity": _s(r.new_quantity),
                        "difference": _s(r.difference),
                    }
                    for r in rows
                ],
                "part_maps": self._part_map_rows((old_product, new_product)),
            }
        )


def screen_routes(api: "Api") -> list[tuple[str, str, Callable[..., Response]]]:
    return Screens(api).routes()
