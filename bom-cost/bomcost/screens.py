"""画面の2段目の API: グループ（作る・流用・消す）・取り込みと列の対応づけ・マスタ登録・部品から見る・モデルチェンジの比較。

経路は Api の _routes に足す（screen_routes）。入力の誤りは ApiError（400 番台・日本語）で返す。
金額と数量は Decimal の文字列で返し、計算は計算の層（mold・modelchange・rollup）に任せる。
"""

import base64
import binascii
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection

from .api import (HEADS, MASTER_HEADS, ApiError, Response, _flag, _head, _json_response, _known_codes, _s, _text_field, _View,
                  change_problem, group_label, group_text)
from .importer import (
    TABLE_FIELDS,
    ColumnMap,
    MissingColumnsError,
    Table,
    VersionImport,
    guess_columns,
    read_table_bytes,
    xlsx_sheet_names,
)
from .model import (
    MANUAL_VERSION_ID,
    CostHead,
    CostRow,
    ImportWarning,
    Item,
    Version,
    Volume,
    overlay_manual,
)
from .group import PRODUCT_KIND_CODE, Group, _Structure, _batches, is_product_kind, part_kind
from .masterbook import MasterPlan, apply_plan, book_bytes, plan_book, plan_json, save_book
from .modelchange import compare_structures
from .mold import Allocation, MoldCost, ReuseTarget, allocate_mold_cost, part_users, reuse_impact
from .normalize import to_code, to_decimal, to_optional_code
from .scenario import ChangeRecord, Scenario
from .store import GroupInfo, bom_subset, retarget

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
    manual: Version, drop_items: Collection[str] = (), drop_volumes: Collection[str] = ()
) -> Version:
    """manual の中身を写す。drop_items の品目と原価、drop_volumes の台数は写さない。"""
    v = Version(MANUAL_VERSION_ID)
    v.add_items(i for code, i in manual.items.items() if code not in drop_items)
    v.add_bom_lines(manual.lines)
    for conflict in manual.conflicts:
        v.add_conflict(conflict.key, conflict.quantities)
    v.add_costs(r for r in manual.cost_rows if r.code not in drop_items)
    for product, volume in manual.volumes.items():
        if product not in drop_volumes:
            v.add_volume(volume)
    return v


def _item_name(version: Version, code: str) -> str:
    item = version.items.get(code)
    return "" if item is None else item.name


def _batch_text(records: list[ChangeRecord]) -> str:
    """1回の操作の文（変更の記録の1行と同じ）。"""
    from .group import _batch_json

    return _batch_json(records)["text"]


def _table_diff(old: Version, new: Version, table: str) -> dict[str, int]:
    """表（item・bom_line・cost・volume）の、old から new への 追加・変更・削除 の件数。"""
    if table == "item":
        a, b = old.items, new.items
    elif table == "bom_line":
        a, b = {l.key: l.quantity for l in old.lines}, {l.key: l.quantity for l in new.lines}
    elif table == "cost":
        a = {r.code: (r.mbom_code, dict(r.values)) for r in old.cost_rows}
        b = {r.code: (r.mbom_code, dict(r.values)) for r in new.cost_rows}
    else:
        a = {p: v.annual for p, v in old.volumes.items()}
        b = {p: v.annual for p, v in new.volumes.items()}
    return {"added": sum(1 for k in b if k not in a), "changed": sum(1 for k in b if k in a and a[k] != b[k]),
            "removed": sum(1 for k in a if k not in b)}


def _unused_master(manual: Version, source: Version, merged: Version, tables: Collection[str]) -> list[str]:
    """取り込む表（tables）で、流用元の BOM の値を上書きしていた登録のうち、その品番が新しい BOM に無くなった品番。

    BOM に無い部品の追加（流用元の BOM にも無い品番の登録）は数えない。
    """
    known = _known_codes(merged)
    found: dict[str, None] = {}
    by_table = {
        "item": (manual.items, source.items),
        "bom_line": (manual.parents(), source.parents()),
        "cost": ([r.code for r in manual.cost_rows], [r.code for r in source.cost_rows]),
        "volume": (manual.volumes, source.volumes),
    }
    for table, (registered, before) in by_table.items():
        if table in tables:
            before = set(before)
            found.update((c, None) for c in registered if c in before and c not in known)
    return list(found)


def _original_code(body: dict[str, Any], key: str, name: str) -> str | None:
    """直す元の行（original の key）の品番。送られていなければ None（同じ品番を入れ替える）。"""
    original = body.get("original")
    if not isinstance(original, dict) or not str(original.get(key) or "").strip():
        return None
    return to_code(original[key], name=name)


class Screens:
    """2段目の画面の API の中身。api は Api（保存と版・試算の読み込みを借りる）。"""

    def __init__(self, api: "Api") -> None:
        self.api = api
        self.store = api.store

    def routes(self) -> list[tuple[str, str, Callable[..., Response]]]:
        return [
            ("GET", r"/api/import/tables", self.import_tables),
            ("POST", r"/api/import/preview", self.import_preview),
            ("POST", r"/api/import/check", self.check_import),
            ("POST", r"/api/import", self.import_files),
            ("POST", r"/api/groups", self.create_group),
            ("GET", r"/api/groups/info", self.group_detail),
            ("DELETE", r"/api/groups", self.delete_group),
            ("PUT", r"/api/groups/note", self.put_group_note),
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
            ("POST", r"/api/master/book", self.write_master_book),
            ("POST", r"/api/master/open", self.open_master_book),
            ("POST", r"/api/master/check", self.check_master_book),
            ("POST", r"/api/master/apply", self.apply_master_book),
            ("GET", r"/api/parts/view", self.part_view),
            ("GET", r"/api/modelchange", self.model_change),
        ]

    # 取り込み ----------------------------------------------------------------

    @staticmethod
    def _upload_bytes(f: Any) -> tuple[str, bytes]:
        if not isinstance(f, dict):
            raise ApiError(400, "ファイルの指定はオブジェクトで送る")
        name = _text_field(f, "filename", "ファイル名")
        content = f.get("content")
        if not isinstance(content, str) or not content:
            raise ApiError(400, f"{name} の中身が空")
        if len(content) > MAX_UPLOAD_BYTES * 4 // 3 + 4:
            raise ApiError(413, f"{name} が大きすぎる（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB）")
        try:
            return name, base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(400, f"{name} の中身を base64 として読めない") from None

    def _read_upload(self, f: Any) -> tuple[Table, str, bytes]:
        name, data = self._upload_bytes(f)
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
            guess, score = guess_columns(table_id, headers)
            result.append(
                {
                    "id": table_id,
                    "label": TABLE_LABELS[table_id],
                    "fields": [{"name": f.name, "label": f.label, "required": f.required} for f in fields],
                    "saved": None if saved is None else dict(saved.columns),
                    "guess": guess,  # 保存した対応づけが無い時に仮に当てる見出し（項目の name → 見出し）
                    "guess_score": score,
                }
            )
        return result

    def import_tables(self, params: dict[str, str]) -> Response:
        """取り込める表と、表ごとの項目（必須か）。ファイルを選ぶ前の表の選択肢に使う。"""
        return _json_response([
            {"id": table_id, "label": TABLE_LABELS[table_id],
             "fields": [{"name": f.name, "label": f.label, "required": f.required} for f in fields]}
            for table_id, fields in TABLE_FIELDS.items()
        ])

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
                # 保存した対応づけが無い時に仮に選ぶ表（見出しの当たりがいちばん強い表・当たりが無ければ None）
                "guessed": max(tables, key=lambda t: t["guess_score"])["id"] if any(t["guess_score"] for t in tables) else None,
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

    def _import_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        """取り込みの確認と決定で同じ中身: 読んだ表・警告・流用元との差・当て直した変更・使われないマスタ登録。何も保存しない。"""
        group_id = _optional_text(body, "group_id")
        if not group_id:
            raise ApiError(400, "グループ（group_id）が空")
        try:
            info = self.store.group_info(group_id)
        except KeyError:
            raise ApiError(404, f"グループ {group_id} は無い") from None
        files = body.get("files")
        if not isinstance(files, list) or not files:
            raise ApiError(400, "取り込むファイル（files）が空")
        imported_at = datetime.now().replace(microsecond=0)
        imp = VersionImport(Version(group_id, imported_at=imported_at, note=info.note, dev_number=info.dev_number,
                                    dev_name=info.dev_name))
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
        tables = {m.table for m in maps}
        taken = [t for t in TABLE_FIELDS if t in tables and t in info.bom_tables]
        if taken:
            raise ApiError(400, "・".join(TABLE_LABELS[t] for t in taken)
                           + " はもうこのグループに入っているので取り込めない（［新しく作る］でこの表を外して流用し、そこへ取り込む）")
        warnings = list(imp.warnings)
        for conflict in imp.version.conflicts:
            parent, balloon, child = conflict.key
            quantities = "・".join(_s(q) for q in conflict.quantities)
            warnings.append(ImportWarning(
                "", None,
                f"構成の数量が食い違う: 親 {parent}・風船番号 {balloon or '空欄'}・子 {child}（{quantities}。計算には最初の数量を使う）",
            ))
        current = self.store.load_bom(group_id)
        merged = bom_subset(current, info.bom_tables)
        new = bom_subset(imp.version, tables)
        merged.add_items(new.items.values())
        merged.add_bom_lines(new.lines)
        for conflict in new.conflicts:
            merged.add_conflict(conflict.key, conflict.quantities)
        merged.add_costs(new.cost_rows)
        for volume in new.volumes.values():
            merged.add_volume(volume)
        version = self.store.overlay_master(merged)
        source = self._source_bom(info)
        all_tables = set(info.bom_tables) | tables
        refit: tuple[list[Scenario], list[int], list[dict[str, Any]]] = ([], [], [])
        if "bom_line" in all_tables:
            refit = self._refit([self.store.load_scenario(s.scenario_id) for s in self.store.list_scenarios(group_id)],
                                version)
        return {
            "info": info, "imp": imp, "maps": maps, "used": used, "tables": tables, "warnings": warnings,
            "version": version, "refit": refit,
            "diff": [{"table": t, "label": TABLE_LABELS[t], **_table_diff(source, new, t)}
                     for t in TABLE_FIELDS if t in tables],
            "unused": _unused_master(self.store.load_manual(group_id), source, merged, tables),
        }

    def _source_bom(self, info: GroupInfo) -> Version:
        """流用元のグループの BOM（流用元が無い・消えた時は空）。取り込みの差と使われないマスタ登録はこれと比べる。"""
        if info.copied_from:
            try:
                return self.store.load_bom(info.copied_from)
            except KeyError:
                pass
        return Version(info.group_id)

    def _refit(self, scenarios: list[Scenario], version: Version
               ) -> tuple[list[Scenario], list[int], list[dict[str, Any]]]:
        """試算の変更を version（新しい構成）の上に、記録のときと同じ順で当て直す（まだ保存しない）。

        場所が無い・品番が無い・部品とその中の場所が重なる操作は写さず、dropped に1行ずつ（製品・操作・理由）。
        製品がグループに無い試算は丸ごと外す（removed に ID）。返すのは（当て直した試算, removed, dropped）。
        """
        policy = self.store.load_scope_policy()
        known = _known_codes(version)
        kept: list[Scenario] = []
        removed: list[int] = []
        dropped: list[dict[str, Any]] = []

        def drop(old: Scenario, records: list[Any], reason: str) -> None:
            dropped.append({"product": old.base, "product_name": _item_name(version, old.base),
                            "text": _batch_text(records), "reason": reason})

        for old in scenarios:
            batches = _batches(old.history)
            fresh = Scenario(old.name, old.base, old.product_code, version, old.head, policy, old.note,
                             scenario_id=old.scenario_id)
            reason = None if old.base in known else f"製品 {old.base} がグループに無い"
            if reason is None:
                try:
                    fresh.calculate()
                except ValueError as exc:
                    reason = str(exc)
            if reason is not None:
                for records in batches:
                    drop(old, records, reason)
                if old.scenario_id is not None:
                    removed.append(old.scenario_id)
                continue
            for records in batches:
                changes = [r.change for r in records]
                problem = change_problem(fresh, changes)
                if problem is None:
                    first = records[0]
                    try:
                        fresh.apply_batch(changes, first.recorded_at, first.volume, first.allocation)
                    except ValueError as exc:
                        problem = str(exc)
                if problem is not None:
                    drop(old, records, problem)
            kept.append(fresh)
        return kept, removed, dropped

    def _import_json(self, plan: dict[str, Any]) -> dict[str, Any]:
        v = plan["imp"].version
        kept, removed, dropped = plan["refit"]
        unused = plan["unused"]
        return {
            "group_id": plan["info"].group_id,
            "label": group_label(plan["version"]),
            "copied_from": plan["info"].copied_from,
            "source_name": v.source_name,
            "files": plan["used"],
            "tables": [t for t in TABLE_FIELDS if t in plan["tables"]],
            "counts": {"items": len(v.items), "lines": len(v.lines), "costs": len(v.cost_rows),
                       "volumes": len(v.volumes)},
            "warnings": [_warning_json(w) for w in plan["warnings"]],
            "warning_count": len(plan["warnings"]),
            "diff": plan["diff"],
            "dropped": dropped,
            "kept_changes": sum(len(_batches(s.history)) for s in kept),
            "unused_master": {"count": len(unused), "codes": unused},
        }

    def check_import(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """取り込む前の確認: 表ごとの件数・流用元との差（追加・変更・削除）・写した変更のうち新しい構成で効かない物・
        使われないマスタ登録。何も保存しない。"""
        return _json_response(self._import_json(self._import_plan(body)))

    def import_files(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """何本かのファイルを、その表がまだ入っていないグループの BOM に入れる（決定）。使った列の対応づけを保存する。

        確認（check_import）と同じ中身を作り直して入れる。写した変更のうち新しい構成で効かない物はグループから外す。
        """
        plan = self._import_plan(body)
        kept, removed, _ = plan["refit"]
        imp = plan["imp"]
        self.store.import_bom(plan["info"].group_id, imp.version, plan["tables"], plan["warnings"], kept, removed)
        for column_map in plan["maps"]:
            self.store.save_column_map(column_map)
        return _json_response(self._import_json(plan), 201)

    # グループ ----------------------------------------------------------------

    def _group_info(self, params: dict[str, Any], key: str = "group") -> GroupInfo:
        group_id = _optional_text(params, key)
        if not group_id:
            raise ApiError(400, f"グループ（{key}）が空")
        try:
            return self.store.group_info(group_id)
        except KeyError:
            raise ApiError(404, f"グループ {group_id} は無い") from None

    def create_group(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """新しいグループを作る。from（流用元のグループ）を付けると、copy の tables（BOM の表）・master（マスタ登録・型費・
        流用先・対応表）・changes（②の変更）を写す。写した後は元とつながらない（copied_from に残すだけ）。

        開発番号・名称を省けば流用元の値。写した変更は新しい構成で当て直し、効かない物は写さず dropped に並べる
        （構成の表を外した時は、構成を取り込む時に当て直す）。
        """
        source_id = _optional_text(body, "from")
        source = self._group_info({"group": source_id}) if source_id else None
        copy = body.get("copy") or {}
        if not isinstance(copy, dict):
            raise ApiError(400, "写すもの（copy）は tables・master・changes のオブジェクトにする")
        tables = copy.get("tables") or []
        if not isinstance(tables, list) or any(t not in TABLE_FIELDS for t in tables):
            raise ApiError(400, f"写す表（copy.tables）は {'・'.join(TABLE_FIELDS)} の並びにする")
        if source is None and (tables or copy.get("master") or copy.get("changes")):
            raise ApiError(400, "流用元（from）が無いと写せない")
        dev_number = _optional_text(body, "dev_number") or ("" if source is None else source.dev_number)
        if not dev_number:
            raise ApiError(400, "開発番号が空")
        dev_name = _optional_text(body, "dev_name") or ("" if source is None else source.dev_name)
        note = _optional_text(body, "note")
        group_id = self.store.new_group_id()
        tables = [t for t in TABLE_FIELDS if t in tables and source is not None and t in source.bom_tables]
        dropped: list[dict[str, Any]] = []
        if source is None:
            self.store.create_group(dev_number, dev_name, note, group_id=group_id)
        else:
            bom = bom_subset(self.store.load_bom(source.group_id), tables, group_id)
            bom.note, bom.dev_number, bom.dev_name = note, dev_number, dev_name
            manual = self.store.load_manual(source.group_id) if _flag(copy.get("master")) else None
            version = overlay_manual(bom, manual or Version(group_id))
            scenarios: list[Scenario] = []
            if _flag(copy.get("changes")):
                old = [self.store.load_scenario(s.scenario_id) for s in self.store.list_scenarios(source.group_id)]
                if "bom_line" in tables or "bom_line" not in source.bom_tables:
                    scenarios, _, dropped = self._refit(old, version)
                else:
                    scenarios = [retarget(s, version) for s in old]
            self.store.create_group(
                dev_number, dev_name, note, group_id=group_id, copied_from=source.group_id, bom=bom, bom_tables=tables,
                warnings=self.store.group_warnings(source.group_id) if tables else (), manual=manual,
                molds=self.store.mold_costs(source.group_id).values() if manual is not None else (),
                reuse=self.store.reuse_targets(source.group_id) if manual is not None else (),
                part_maps=self.store.part_map_keys(source.group_id) if manual is not None else (),
                scenarios=scenarios,
            )
        info = self.store.group_info(group_id)
        return _json_response({"group_id": group_id, "label": group_text(info.dev_number, info.dev_name, info.note),
                               "copied_from": info.copied_from, "bom_tables": list(info.bom_tables),
                               "scenario_count": len(self.store.list_scenarios(group_id)), "dropped": dropped}, 201)

    def group_detail(self, params: dict[str, str]) -> Response:
        info = self._group_info(params)
        return _json_response(
            {
                "group_id": info.group_id,
                "label": group_text(info.dev_number, info.dev_name, info.note),
                "dev_number": info.dev_number,
                "dev_name": info.dev_name,
                "note": info.note,
                "created_at": None if info.created_at is None else info.created_at.isoformat(),
                "copied_from": info.copied_from,
                "imported_at": None if info.imported_at is None else info.imported_at.isoformat(),
                "source_name": info.source_name,
                "bom_tables": list(info.bom_tables),
                "warning_count": info.warning_count,
                "warnings": [_warning_json(w) for w in self.store.group_warnings(info.group_id)],
                "products": [s.base for s in self.store.list_scenarios(info.group_id)],
            }
        )

    def delete_group(self, params: dict[str, str]) -> Response:
        """グループと中身（BOM・マスタ登録・変更）を消し、グループの一覧を返す。流用したグループは残る。"""
        self.store.delete_group(self._group_info(params).group_id)
        return self.api.groups({})

    def put_group_note(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """グループの検討内容（メモ）だけを書き換え、グループの一覧を返す。"""
        self.store.set_group_note(self._group_info(params).group_id, _optional_text(body, "note"))
        return self.api.groups({})

    # マスタ登録（グループごと） ------------------------------------------------

    def _group_id(self, params: dict[str, Any], body: dict[str, Any] | None = None) -> str:
        """登録の入るグループ。group（問い合わせ）か group_id（送る内容）で受ける。無いグループは 404。"""
        data = {**params, **(body or {})}
        key = "group_id" if _optional_text(data, "group_id") else "group"
        return self._group_info(data, key).group_id

    def _product_kind(self, group_id: str, version: Version | None = None) -> str:
        """手入力の製品に付ける区分の記号。グループ（マスタ登録を重ねる）で製品とみなす記号、決まらなければ PRODUCT_KIND_CODE。

        製品群はグループごとに記号を選ぶので、グループの記号に合わせないと手入力の製品が製品の一覧から外れる。
        """
        version = version or self.store.load_group(group_id)
        return Group.product_kind(version, _Structure(version)) or PRODUCT_KIND_CODE

    def _trials(self, version: Version) -> list[dict[str, Any]]:
        """グループの試算（②の変更でできた新製品）の一覧。1本1行。"""
        rows = []
        for s in self.store.list_scenarios(version.version_id):
            base = version.items.get(s.base)
            rows.append({"scenario_id": s.scenario_id, "code": s.product_code,
                         "name": "" if base is None else base.name, "base": s.base})
        return rows

    def manual(self, params: dict[str, str]) -> Response:
        """グループのマスタ登録の品目（role で製品・部品に分ける）・年間台数・型費・流用先・対応表と試算（新製品）の一覧。

        in_bom は登録の品番のうち BOM にもある品番（計算はマスタ登録の値を使い、空の費目だけ BOM の値）。
        """
        group_id = self._group_id(params)
        manual = self.store.load_manual(group_id)
        version = self.store.load_group(group_id)
        product_kind = self._product_kind(group_id, version)
        codes = list(manual.items) + [r.code for r in manual.cost_rows if r.code not in manual.items]
        items = []
        for code in codes:
            item = manual.items.get(code)
            kind = "" if item is None else item.kind
            items.append(
                {
                    "code": code,
                    "name": "" if item is None else item.name,
                    "specification": "" if item is None else item.specification,
                    "kind": kind,
                    "role": "product" if kind == product_kind else "part",
                    "costs": {h.name: _s(manual.cost_value(code, h)) for h in dict.fromkeys(HEADS + MASTER_HEADS)},
                }
            )
        bom = _known_codes(self.store.load_bom(group_id))
        return _json_response(
            {
                "group_id": group_id,
                "heads": [{"id": h.name, "label": h.value} for h in MASTER_HEADS],
                "items": items,
                "product_kind": product_kind,
                "trials": self._trials(version),
                "volumes": [{"product": v.product, "annual": _s(v.annual), "kind": v.kind.value}
                            for v in manual.volumes.values()],
                "molds": [{"part": m.part, "amount": _s(m.amount), "years": _s(m.years),
                           "annual_amount": _s(m.annual_amount)} for m in self.store.mold_costs(group_id).values()],
                "reuse": [{"part": t.part, "product": t.product, "quantity": _s(t.quantity_per_unit),
                           "replaced_part": t.replaced_part} for t in self.store.reuse_targets(group_id)],
                "part_maps": self._part_map_rows(group_id),
                "in_bom": [c for c in dict.fromkeys([*codes, *manual.parents(), *manual.volumes]) if c in bom],
                "allocations": [{"id": a.name, "label": a.value} for a in Allocation],
            }
        )

    def put_manual_item(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """試作品の品目と原価を足す・直す（同じ品番は入れ替え）。マスタ登録に入る。

        group（問い合わせ）か group_id（送る内容）のグループの登録に入る。
        role（product・part）を送ると区分はそれで決まる: product は製品とみなす記号、part は kind（製品の記号は不可・
        空なら part_kind）。
        role が無ければ kind をそのまま区分にする。
        original（{"code": 直す元の品番}）があれば、その行を消して code の行に置き換える。
        """
        group_id = self._group_id(params, body)
        code = to_code(_text_field(body, "code", "品番"))
        original = _original_code(body, "code", "直す元の品番") or code
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
        kind = _optional_text(body, "kind")
        role = _optional_text(body, "role")
        if role:
            product_kind = self._product_kind(group_id)
            if role == "product":
                kind = product_kind
            elif role != "part":
                raise ApiError(400, f"role が分からない: {role}（product・part）")
            elif is_product_kind(product_kind, kind):
                raise ApiError(400, f"部品の区分に製品の記号 {kind} は使えない")
            else:
                kind = part_kind(product_kind, kind)
        manual = self.store.load_manual(group_id)
        kept = manual.items.get(original) or manual.items.get(code)
        result = _manual_copy(manual, drop_items={code, original})
        result.add_item(Item(code, name, kind,
                             "" if kept is None else kept.lifecycle,
                             False if kept is None else kept.has_children,
                             specification=_optional_text(body, "specification")))
        if any(v is not None for v in values.values()):
            result.add_cost(CostRow(code, values))
        self.store.save_manual(result, group_id)
        return self.manual({"group": group_id})

    def delete_manual_item(self, params: dict[str, str]) -> Response:
        group_id = self._group_id(params)
        code = to_code(_param(params, "code", "品番"))
        manual = self.store.load_manual(group_id)
        if code not in manual.items and manual.cost_row(code) is None:
            raise ApiError(404, f"手入力の品目 {code} は無い")
        self.store.save_manual(_manual_copy(manual, drop_items={code}), group_id)
        return self.manual({"group": group_id})

    def put_volume(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """新製品の年間台数（計画）を足す・直す。original（{"product": 直す元の品番}）があればその行を置き換える。"""
        group_id = self._group_id(params, body)
        product = to_code(_text_field(body, "product", "製品の品番"))
        original = _original_code(body, "product", "直す元の品番") or product
        annual = _required_number(body.get("annual"), "年間台数")
        if annual < 0:
            raise ApiError(400, "年間台数は 0 以上にする")
        manual = self.store.load_manual(group_id)
        result = _manual_copy(manual, drop_volumes={product, original})
        result.add_volume(Volume(product, annual))
        self.store.save_manual(result, group_id)
        return self.manual({"group": group_id})

    def delete_volume(self, params: dict[str, str]) -> Response:
        group_id = self._group_id(params)
        product = to_code(_param(params, "product", "製品の品番"))
        manual = self.store.load_manual(group_id)
        if product not in manual.volumes:
            raise ApiError(404, f"手入力の年間台数 {product} は無い")
        self.store.save_manual(_manual_copy(manual, drop_volumes={product}), group_id)
        return self.manual({"group": group_id})

    def put_mold(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """型費を足す・直す。original（{"part": 直す元の品番}）があればその行を置き換える。"""
        group_id = self._group_id(params, body)
        part = to_code(_text_field(body, "part", "部品の品番"))
        original = _original_code(body, "part", "直す元の品番")
        amount = _required_number(body.get("amount"), "型費")
        years = _required_number(body.get("years", "2"), "償却年数")
        if amount < 0:
            raise ApiError(400, "型費は 0 以上にする")
        if years <= 0:
            raise ApiError(400, "償却年数は正の数にする")
        if original is not None and original != part:
            self.store.delete_mold_cost(original, group_id)
        self.store.save_mold_cost(MoldCost(part, amount, years), group_id)
        return self.manual({"group": group_id})

    def delete_mold(self, params: dict[str, str]) -> Response:
        group_id = self._group_id(params)
        part = to_code(_param(params, "part", "部品の品番"))
        if part not in self.store.mold_costs(group_id):
            raise ApiError(404, f"型費 {part} は無い")
        self.store.delete_mold_cost(part, group_id)
        return self.manual({"group": group_id})

    def put_reuse(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """流用先を足す・直す。同じ部品と流用先の組は入れ替える。original があればその組を消してから足す。"""
        group_id = self._group_id(params, body)
        part = to_code(_text_field(body, "part", "部品の品番"))
        product = to_code(_text_field(body, "product", "流用先の品番"))
        quantity = _required_number(body.get("quantity"), "使用数")
        if quantity <= 0:
            raise ApiError(400, "使用数は正の数にする")
        replaced = _optional_text(body, "replaced_part") or None
        target = ReuseTarget(part, product, quantity, replaced)
        original = body.get("original")
        if isinstance(original, dict) and original.get("part") and original.get("product"):
            self.store.delete_reuse_target(ReuseTarget(original["part"], original["product"], 1), group_id)
        self.store.delete_reuse_target(target, group_id)
        self.store.add_reuse_target(target, group_id)
        return self.manual({"group": group_id})

    def delete_reuse(self, params: dict[str, str]) -> Response:
        group_id = self._group_id(params)
        target = ReuseTarget(_param(params, "part", "部品の品番"), _param(params, "product", "流用先の品番"), 1)
        if not any(t.product == target.product for t in self.store.reuse_targets(group_id, target.part)):
            raise ApiError(404, f"流用先 {target.part} → {target.product} は無い")
        self.store.delete_reuse_target(target, group_id)
        return self.manual({"group": group_id})

    # 部品の対応表 ------------------------------------------------------------

    def _part_map_rows(self, group_id: str, pair: tuple[str, str] | None = None) -> list[dict[str, Any]]:
        pairs = [pair] if pair is not None else self.store.part_map_pairs(group_id)
        return [
            {"old_product": old, "new_product": new, "old_part": r.old_part, "new_part": r.new_part,
             "old_balloon": r.old_balloon, "new_balloon": r.new_balloon}
            for old, new in pairs
            for r in self.store.part_map_rows(group_id, old, new)
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
        return _json_response(self._part_map_rows(self._group_id(params), pair))

    def add_part_map(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        group_id = self._group_id(params, body)
        key = self._part_map_key(body)
        if key[0] == key[1]:
            raise ApiError(400, "元の製品と新しい製品は別の品番にする")
        if key[2] == key[3]:
            raise ApiError(400, "同じ品番どうしは対応表に入れなくても自動で対応する")
        self.store.add_part_map(group_id, *key)
        return _json_response(self._part_map_rows(group_id), 201)

    def update_part_map(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        group_id = self._group_id(params, body)
        original = body.get("original")
        if not isinstance(original, dict):
            raise ApiError(400, "直す元の行（original）が空")
        old_key = self._part_map_key(original)
        new_key = self._part_map_key(body)
        if new_key[2] == new_key[3]:
            raise ApiError(400, "同じ品番どうしは対応表に入れなくても自動で対応する")
        if self.store.delete_part_map(group_id, *old_key) == 0:
            raise ApiError(404, "直す元の対応表の行が無い")
        self.store.add_part_map(group_id, *new_key)
        return _json_response(self._part_map_rows(group_id))

    def delete_part_map(self, params: dict[str, str]) -> Response:
        group_id = self._group_id(params)
        if self.store.delete_part_map(group_id, *self._part_map_key(dict(params))) == 0:
            raise ApiError(404, "対応表の行が無い")
        return _json_response(self._part_map_rows(group_id))

    # マスタのブック（Excel で開いて打ち込み、まとめて取り込む） ----------------

    def _book_path(self, group_id: str) -> Path:
        """ブックの置き場。グループごとに1つ（ファイル名にグループの ID を入れる）。"""
        folder = self.api.master_dir
        if folder is None:
            if self.store.path is None:
                raise ApiError(400, "DB がファイルでないのでブックの置き場が無い")
            folder = self.store.path.parent
        stem = "bom-cost" if self.store.path is None else self.store.path.stem
        suffix = "-" + re.sub(r'[\\/:*?"<>|\s]', "_", group_id)
        return Path(folder) / f"{stem}-master{suffix}.xlsx"

    def write_master_book(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """グループの今の登録をブックに書き、Windows では既定のアプリで開く。

        開けない環境ではパスを返すだけ。
        """
        group_id = self._group_id(params, body)
        path = self._book_path(group_id)
        data = book_bytes(self.store, self._product_kind(group_id), group_id)
        try:
            save_book(path, data)
        except PermissionError:
            raise ApiError(409, "ブックが Excel で開いたままで書き出せない（閉じてからもう一度）") from None
        except OSError as exc:
            raise ApiError(500, f"ブックを書き出せない: {exc.strerror or exc}") from None
        return self._open_book(path)

    def open_master_book(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """置き場のブックを書き直さずに開く。無ければ 404。"""
        path = self._book_path(self._group_id(params, body))
        if not path.is_file():
            raise ApiError(404, "ブックがまだ無い（先に［Excel で開く］）")
        return self._open_book(path)

    def _open_book(self, path: Path) -> Response:
        opened = False
        if self.api.open_file is not None:
            try:
                self.api.open_file(path)
                opened = True
            except OSError:
                pass
        return _json_response({"path": str(path), "opened": opened})

    def _master_plan(self, params: dict[str, str], body: dict[str, Any]) -> tuple[str, MasterPlan]:
        """body に content があれば送られたブック、無ければ置き場のブックを読む。グループの登録と比べる。"""
        group_id = self._group_id(params, body)
        if body.get("content"):
            name, data = self._upload_bytes(body)
            if not name.lower().endswith((".xlsx", ".xlsm")):
                raise ApiError(400, f"{name} は Excel のブック（.xlsx）でない")
        else:
            path = self._book_path(group_id)
            name = str(path)
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                raise ApiError(404, "ブックがまだ無い（先に［Excel で開く］）") from None
            except PermissionError:
                raise ApiError(409, "ブックを読めない（Excel が開いたまま？）") from None
        try:
            plan = plan_book(self.store, self._product_kind(group_id), data, group_id)
        except Exception:  # 壊れた .xlsx は zipfile などの例外で落ちるので、まとめて読めないとする
            raise ApiError(400, f"{name} を Excel のブックとして読めない") from None
        return name, plan

    def check_master_book(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """ブックと今の登録の差（追加・変更・同じ・削除の件数）と行の誤りを返す。何も保存しない。"""
        name, plan = self._master_plan(params, body)
        return _json_response({"source": name, **plan_json(plan)})

    def apply_master_book(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """ブックを読み直して入れる。行の誤りが1つでもあれば何も入れず 400（errors つき）。"""
        name, plan = self._master_plan(params, body)
        if plan.errors:
            return _json_response({"error": f"行の誤りが {len(plan.errors)} 件あるので取り込まない",
                                   "source": name, **plan_json(plan)}, 400)
        apply_plan(self.store, plan)
        return _json_response({"source": name, **plan_json(plan), "manual": self.manual({"group": plan.group_id}).json()})

    # 部品から見る ------------------------------------------------------------

    def part_view(self, params: dict[str, str]) -> Response:
        """部品を使う製品・流用先と、型費の載り方・原価の今と後（費目5つ）。設計書 6章の図4。

        group（グループ）か scenario（その試算のグループ）で見る。scenario を選ぶと、その試算の製品は「土台＋変更」の仮の構成で
        使用数を数える。
        head は主に見る費目（既定は材料費）。費目5つの値は heads に並べる。材料費のほかは製品を一番上にした式の値で、
        流用先の部品の差は部品の材料費がその費目に効く分。
        """
        part = to_code(_param(params, "part", "部品の品番"))
        allocation = _allocation(params.get("allocation"))
        head = _head(params.get("head"), CostHead.MATERIAL)
        scenario: Scenario | None = None
        if params.get("scenario"):
            scenario = self.api._load_scenario(_scenario_id(params["scenario"]))
        group_id = params.get("group") or (scenario.version.version_id if scenario else "")
        if not group_id:
            raise ApiError(400, "グループ（group）が空")
        version = self.api._load_group(group_id)
        if part not in _known_codes(version):
            raise ApiError(400, f"品番 {part} はグループに無い")
        policy = self.store.load_scope_policy()
        overrides = scenario.overrides() if scenario is not None else None
        reuse = self.store.reuse_targets(version.version_id, part)
        mold = self.store.mold_costs(version.version_id).get(part)
        users, unknown = part_users(version, part, reuse, overrides)
        alloc = None if mold is None else allocate_mold_cost(version, mold, allocation, reuse, overrides)
        # 原価は「製品から」の画面と同じ見方（材料費の足し上げと式）で出す
        if scenario is not None:
            view = _View(Scenario(scenario.name, scenario.base, scenario.product_code, version, CostHead.MATERIAL,
                                  policy, history=scenario.history), "diff", CostHead.MATERIAL, False)
        else:
            view = _View(Scenario("", part, f"{part}#", version, CostHead.MATERIAL, policy),
                         "bulk", CostHead.MATERIAL, False)
        cost = view.cost

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
            name = _item_name(version, scenario.base) if is_trial and item is None else ("" if item is None else item.name)
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
                    "group_id": version.version_id,
                    "group_label": group_label(version),
                    "head": head.name,
                    "head_label": head.value,
                    "allocation": allocation.name,
                    "allocation_label": allocation.value,
                    "volumes": [{"product": u.product, "kind": u.volume_kind.value} for u in users],
                    "scenario_id": None if scenario is None else scenario.scenario_id,
                    "scenario_base": None if scenario is None else scenario.base,
                    "reference": "参考値",
                    "amortization_excluded": True,
                    # 製品の償却費は登録値を式に入れる（③の［償却費を無視］は 0 とするので、登録があれば製造原価が違う）
                    "amortization_basis": "registered",
                },
            }
        )

    # モデルチェンジ ----------------------------------------------------------

    def model_change(self, params: dict[str, str]) -> Response:
        """元の製品と新製品（または試算）の構成を、同じ品番・対応表・片方だけの3段で並べる。設計書 8章。

        old_group・new_group は元・新しい製品のグループ（新しい側を省けば元と同じ）。対応表は新しい側のグループの分を使う。
        """
        scenario: Scenario | None = None
        if params.get("scenario"):
            scenario = self.api._load_scenario(_scenario_id(params["scenario"]))
        old_text = params.get("old") or (scenario.base if scenario is not None else "")
        if not old_text:
            raise ApiError(400, "元の製品の品番（old）が空")
        old_product = to_code(old_text)
        old_group = params.get("old_group") or (scenario.version.version_id if scenario else "")
        if not old_group:
            raise ApiError(400, "元の製品のグループ（old_group）が空")
        old_version = self.api._load_group(old_group)
        if scenario is not None:
            new_product = scenario.product_code
            new_version = scenario.version
            new_overrides = scenario.overrides()
        else:
            new_product = to_code(_param(params, "new", "新しい製品の品番"))
            new_version = self.api._load_group(params.get("new_group") or old_group)
            new_overrides = None
        if old_product == new_product:
            raise ApiError(400, "元の製品と新しい製品は別の品番にする")
        for code, version in ((old_product, old_version), (new_product, new_version)):
            if not (scenario is not None and code == new_product) and not version.children(code):
                raise ApiError(400, f"製品 {code} の構成がグループ {group_label(version)} に無い")
        direct_only = str(params.get("direct_only", "")).lower() in ("1", "true", "yes")
        group_id = new_version.version_id
        mapping = self.store.part_map(group_id, old_product, new_product)
        map_rows = self._part_map_rows(group_id, (old_product, new_product))
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
                "old_group": old_version.version_id,
                "old_group_label": group_label(old_version),
                "new_product": new_product,
                "new_group": new_version.version_id,
                "new_group_label": group_label(new_version),
                "scenario_id": None if scenario is None else scenario.scenario_id,
                "scenario_base": None if scenario is None else scenario.base,
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
                "part_maps": map_rows,
            }
        )


def screen_routes(api: "Api") -> list[tuple[str, str, Callable[..., Response]]]:
    return Screens(api).routes()
