"""画面から送られたファイル（filename と、base64 の content）を表として読む。"""

import base64
import binascii
from typing import Any

from .api import ApiError
from .importer import Table, read_table_bytes, xlsx_sheet_names

# 送る形は base64 なので、要求の本文はこの 4/3 倍になる
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
SAMPLE_ROWS = 5


def _upload_bytes(f: Any) -> tuple[str, bytes]:
    if not isinstance(f, dict):
        raise ApiError(400, "ファイルの指定はオブジェクトで送る")
    name = f.get("filename")
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "ファイル名が空")
    name = name.strip()
    content = f.get("content")
    if not isinstance(content, str) or not content:
        raise ApiError(400, f"{name} の中身が空")
    if len(content) > MAX_UPLOAD_BYTES * 4 // 3 + 4:
        raise ApiError(413, f"{name} が大きすぎる（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB）")
    try:
        return name, base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, f"{name} の中身を base64 として読めない") from None


def _sheet(f: dict[str, Any]) -> str | None:
    value = f.get("sheet")
    if value is not None and not isinstance(value, str):
        raise ApiError(400, "sheet は文字で送る")
    return (value or "").strip() or None


def read_upload(f: Any) -> tuple[Table, str, bytes]:
    """送られた1つのファイルの表・ファイル名・中身。sheet は Excel のシート名。"""
    name, data = _upload_bytes(f)
    try:
        table = read_table_bytes(name, data, _sheet(f))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    except Exception:  # 壊れた .xlsx は zipfile などの例外で落ちるので、まとめて読めないとする
        raise ApiError(400, f"{name} を Excel のファイルとして読めない") from None
    return table, name, data


def preview(f: Any) -> tuple[Table, dict[str, Any]]:
    """送られたファイルの表と、画面に見せる中身（シートの名前・見出し・行数・先頭の行）。"""
    table, name, data = read_upload(f)
    sheets = list(xlsx_sheet_names(data)) if name.lower().endswith((".xlsx", ".xlsm")) else []
    return table, {
        "filename": name,
        "sheets": sheets,
        "sheet": _sheet(f) or (sheets[0] if sheets else None),
        "headers": list(table.headers),
        "row_count": len(table.rows),
        "sample": [["" if v is None else str(v) for v in row] for row in table.rows[:SAMPLE_ROWS]],
    }
