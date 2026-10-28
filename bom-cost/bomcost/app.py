"""手元で立てる画面のサーバー。127.0.0.1 だけで待ち、画面（bomcost/web/）と API（JSON）を出す。

使い方:
    python -m bomcost.app --db bom-cost.sqlite [--port N] [--no-browser]
    python -m bomcost.app --demo --db demo.sqlite   # 架空の例を入れた DB を作って終わる
"""

import argparse
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .api import Api, Response, error_response
from .demo import build_demo
from .store import Store

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WEB_DIR = Path(__file__).resolve().parent / "web"
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

        def _serve(self, method: str) -> None:
            parts = urlsplit(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                response = error_response(400, "Content-Length が数でない")
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


def serve(db: Path, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    # sqlite3 の接続は作ったスレッドでしか使えないので、保存と計算は1本のスレッドに寄せる
    worker = ThreadPoolExecutor(max_workers=1)
    store = worker.submit(Store, db).result()
    api = Api(store)

    def call(method: str, path: str, query: str, body: bytes) -> Response:
        return worker.submit(api.handle, method, path, query, body).result()

    server = bind(port, make_handler(call))
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"BOM 原価モック: {url}  （DB: {db}・止めるには Ctrl+C）", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        worker.submit(store.close).result()
        worker.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bomcost.app", description="BOM 原価モックの画面を立てる")
    parser.add_argument("--db", required=True, type=Path, help="SQLite のファイル（無ければ空で作る）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"待つポート（既定 {DEFAULT_PORT}）")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    parser.add_argument("--demo", action="store_true", help="架空の例を入れた DB を --db に作って終わる")
    args = parser.parse_args(argv)
    if args.demo:
        try:
            scenario_id = build_demo(args.db)
        except FileExistsError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"架空の例の DB を作った: {args.db}（試算 ID {scenario_id}）")
        print(f"立てるには: python -m bomcost.app --db {args.db}")
        return 0
    serve(args.db, args.port, not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
