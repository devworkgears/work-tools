"""画面の API（JSON）の入口。HTTP のハンドラはここの Api.handle を呼ぶだけで、ソケットを開かずにテストできる。

中身は設計検討（studyapi）と設定（settingsapi）。金額と数量は Decimal の文字列で返し、未登録の原価は null で返して 0 とは分ける。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs

from .settings import SettingsMissing
from .store import Store

JSON_TYPE = "application/json; charset=utf-8"


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = JSON_TYPE
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def _json_response(data: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def error_response(status: int, message: str) -> Response:
    return _json_response({"error": message}, status)


Handler = Callable[..., Response]


class Api:
    """画面の API。store は SQLite のファイル（Store）。1つのスレッドから呼ぶこと（sqlite3 の接続の制約）。"""

    def __init__(self, store: Store) -> None:
        # 2つとも api を読むので、ここで読む
        from .settingsapi import settings_routes
        from .studyapi import study_routes

        self.store = store
        routes: list[tuple[str, str, Handler]] = [("GET", r"/api/running", self.running), *study_routes(self), *settings_routes(self)]
        self._routes = [(m, re.compile(p), h) for m, p, h in routes]

    def handle(self, method: str, path: str, query: str = "", body: bytes = b"") -> Response:
        """1つの要求を処理する。入力の誤りは 400 番台と日本語のエラー文で返し、例外は外に出さない。"""
        try:
            matched_path = False
            for route_method, pattern, handler in self._routes:
                if pattern.fullmatch(path) is None:
                    continue
                matched_path = True
                if route_method != method:
                    continue
                params = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}
                if method in ("POST", "PUT"):
                    return handler(params, _parse_body(body))
                return handler(params)
            if matched_path:
                return error_response(405, f"{method} は使えない: {path}")
            return error_response(404, f"API が無い: {path}")
        except ApiError as exc:
            return error_response(exc.status, exc.message)
        except SettingsMissing as exc:
            return error_response(409, str(exc))
        except KeyError as exc:
            return error_response(404, str(exc.args[0]) if exc.args else "見つからない")
        except (ValueError, TypeError) as exc:
            return error_response(400, str(exc))
        except Exception as exc:  # 画面を止めないため、想定外の失敗も JSON で返す
            return error_response(500, f"サーバーの中で失敗した: {type(exc).__name__}: {exc}")

    def running(self, params: dict[str, str]) -> Response:
        """この DB で動いている印（もう一度起動した時に、2つ目を立てないために聞く）。"""
        return _json_response({"db": None if self.store.path is None else str(self.store.path.resolve())})


def _parse_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(400, "送られた内容を JSON として読めない") from None
    if not isinstance(data, dict):
        raise ApiError(400, "送る内容は JSON のオブジェクトにする")
    return data
