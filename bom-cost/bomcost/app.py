"""手元で立てる画面のサーバー。127.0.0.1 だけで待ち、画面（bomcost/web/）と API（JSON）を出す。

使い方:
    python -m bomcost.app --db bom-cost.sqlite [--port N] [--no-browser] [--minimize]
"""

import argparse
import ctypes
import http.client
import json
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import settings
from .api import Api, Response, error_response
from .store import Store
from .upload import MAX_UPLOAD_BYTES

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WEB_DIR = Path(__file__).resolve().parent / "web"
# ファイルは base64 で送るので、取り込みの上限の 4/3 倍＋JSON の分。4本まとめて送る取り込みも入る
MAX_BODY_BYTES = (MAX_UPLOAD_BYTES * 4 // 3) * 4 + 1024 * 1024
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

Call = Callable[[str, str, str, bytes], Response]


class _Server(ThreadingHTTPServer):
    # Windows の SO_REUSEADDR は使用中のポートにも bind できてしまい、空きの判定ができないので切る
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def static_response(path: str, web_dir: Path = WEB_DIR) -> Response:
    """web_dir の直下のファイルだけを返す（下の階層・外のパスは出さない）。"""
    if path == "/favicon.ico":  # ブラウザが勝手に要求する。404 だとコンソールにエラーが出る
        return Response(204, b"", "image/x-icon")
    # 前の版の画面の場所。覚えたまま残らないように、302 で送る
    if path in ("/v2", "/v2/"):
        return Response(302, b"", "text/plain; charset=utf-8", {"Location": "/"})
    # 設定（/settings/）の画面は別のフォルダに置く（中の相対パスが引けるよう、末尾の / へ寄せる）
    if web_dir.name != "settings":
        if path == "/settings":
            return Response(301, b"", "text/plain; charset=utf-8", {"Location": "/settings/"})
        if path.startswith("/settings/"):
            return static_response(path[len("/settings"):], web_dir / "settings")
    name = "index.html" if path in ("", "/") else path.lstrip("/")
    target = web_dir / name
    if "/" in name or "\\" in name or target.suffix not in STATIC_TYPES or not target.is_file():
        return error_response(404, f"ファイルが無い: {path}")
    return Response(200, target.read_bytes(), STATIC_TYPES[target.suffix], {"Cache-Control": "no-store"})


def make_handler(call: Call) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # 投機的に開いたまま何も送らない接続で待ち続けないため
        timeout = 30

        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._serve("POST")

        def do_PUT(self) -> None:
            self._serve("PUT")

        def do_DELETE(self) -> None:
            self._serve("DELETE")

        def _own_origin(self) -> bool:
            # 他のサイトからの送信（CSRF）と、名前を 127.0.0.1 に向け直す読み取り（DNS rebinding）を拒む
            hosts = {f"{name}:{self.server.server_address[1]}" for name in (HOST, "localhost")}
            origin = self.headers.get("Origin")
            return self.headers.get("Host") in hosts and (origin is None or origin in {f"http://{h}" for h in hosts})

        def _serve(self, method: str) -> None:
            parts = urlsplit(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not self._own_origin():
                self.close_connection = True
                response = error_response(403, "このページ以外からは使えない")
            elif length < 0:
                response = error_response(400, "Content-Length が数でない")
            elif length > MAX_BODY_BYTES:
                # 読まずに返すので、残りを受けないよう接続を閉じる
                self.close_connection = True
                response = error_response(413, f"送る内容が大きすぎる（上限 {MAX_BODY_BYTES // (1024 * 1024)} MB）")
            else:
                body = self.rfile.read(length) if length else b""
                if parts.path.startswith("/api/"):
                    response = call(method, parts.path, parts.query, body)
                elif method == "GET":
                    response = static_response(parts.path)
                else:
                    response = error_response(405, f"{method} は使えない: {parts.path}")
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def bind(port: int, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    """127.0.0.1:port で待つ。使われていたら OS に空きのポートを選ばせる。"""
    try:
        return _Server((HOST, port), handler)
    except OSError:
        return _Server((HOST, 0), handler)


def port_file(db: Path) -> Path:
    """動いているサーバーのポートを書くファイル（DB の隣）。"""
    return db.with_name(db.name + ".port")


def running_port(db: Path) -> int | None:
    """この DB で動いているサーバーのポート。無ければ None（窓を閉じて止めるとファイルが残るので、聞いて確かめる）。"""
    try:
        port = int(port_file(db).read_text(encoding="ascii"))
        if not 0 < port < 65536:
            return None
        conn = http.client.HTTPConnection(HOST, port, timeout=5)
        try:
            conn.request("GET", "/api/running")
            data = json.loads(conn.getresponse().read())
        finally:
            conn.close()
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return port if isinstance(data, dict) and data.get("db") == str(db.resolve()) else None


def minimize_console() -> None:
    """Windows の黒い窓を最小化する。できなければ何もしない。"""
    if os.name != "nt":
        return
    try:
        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 6)  # SW_MINIMIZE
    except (AttributeError, OSError):
        pass


def open_page(port: int) -> None:
    webbrowser.open(f"http://{HOST}:{port}/")


def serve(db: Path, port: int = DEFAULT_PORT, open_browser: bool = True, minimize: bool = False) -> None:
    running = running_port(db)
    if running is not None:
        print(f"もう動いています: http://{HOST}:{running}/", flush=True)
        if open_browser:
            open_page(running)
        return
    settings.load(db)
    # sqlite3 の接続は作ったスレッドでしか使えないので、保存と計算は1本のスレッドに寄せる
    worker = ThreadPoolExecutor(max_workers=1)
    store = worker.submit(Store, db).result()
    api = Api(store)

    def call(method: str, path: str, query: str, body: bytes) -> Response:
        return worker.submit(api.handle, method, path, query, body).result()

    server = bind(port, make_handler(call))
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"BOM 原価モック: {url}  （DB: {db}・止めるには Ctrl+C）", flush=True)
    try:
        port_file(db).write_text(str(server.server_address[1]), encoding="ascii")
    except OSError:
        pass  # 書けなくても使える（二重の起動を止められないだけ）
    if minimize:
        minimize_console()
    if open_browser:
        open_page(server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        port_file(db).unlink(missing_ok=True)
        worker.submit(store.close).result()
        worker.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bomcost.app", description="BOM 原価モックの画面を立てる")
    parser.add_argument("--db", required=True, type=Path, help="SQLite のファイル（無ければ空で作る）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"待つポート（既定 {DEFAULT_PORT}）")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    parser.add_argument("--minimize", action="store_true", help="起動したら黒い窓を最小化する（Windows）")
    args = parser.parse_args(argv)
    try:
        serve(args.db, args.port, not args.no_browser, args.minimize)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
