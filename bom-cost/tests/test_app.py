"""画面の API の入口（ソケットを開かずにハンドラの関数を呼ぶ）・静的ファイル・待ち受けの形。データはすべて架空。"""

import contextlib
import io
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from decimal import Decimal
from pathlib import Path

import example_settings  # noqa: F401
import example_data as ex
from bomcost import CostHead, CostRow, Scenario, Store
from bomcost.scenario import Change, ChangeKind
from bomcost.api import Api, Response
from bomcost import app
from bomcost.app import HOST, bind, main, make_handler, port_file, running_port, static_response


class BadRequest(unittest.TestCase):
    def test_errors_are_4xx_in_japanese(self) -> None:
        with Store(":memory:") as store:
            api = Api(store)
            for method, path, body, status, words in (
                    ("POST", "/api/v2/studies", b"{not json", 400, "JSON として読めない"), ("POST", "/api/v2/studies", b"[]", 400, "オブジェクト"),
                    ("GET", "/api/nothing", b"", 404, "API が無い"), ("DELETE", "/api/v2/study", b"", 405, "使えない"),
                    ("GET", "/api/groups", b"", 404, "API が無い")):
                with self.subTest(path=path, body=body):
                    r = api.handle(method, path, "", body)
                    self.assertEqual(r.status, status)
                    self.assertIn(words, r.json()["error"])
            self.assertEqual(api.handle("GET", "/api/v2/studies").status, 200)  # 誤りの後も使える


class RegisteredValueOfTrialProduct(unittest.TestCase):
    def test_registered_value_of_trial_product_is_not_used(self) -> None:
        version = ex.version_2026_10()
        version.add_cost(CostRow("V-110", {CostHead.MATERIAL: 9999}))
        scenario = Scenario("t", "A-100", "V-110", version, CostHead.MATERIAL, ex.policy())
        scenario.apply(Change(ChangeKind.ADD, "F-600"))
        scenario.apply(Change(ChangeKind.REMOVE, "C-300"))
        self.assertEqual(scenario.calculate().total, Decimal("4650"))
        scenario.apply(Change(ChangeKind.REPLACE, "F-600", replacement="X-610"))
        self.assertEqual(scenario.calculate().total, Decimal("4600"))


class StaticAndServer(unittest.TestCase):
    def test_favicon_is_empty_204(self) -> None:
        response = static_response("/favicon.ico")
        self.assertEqual((response.status, response.body), (204, b""))

    def test_static_files(self) -> None:
        index = static_response("/")
        self.assertEqual(index.status, 200)
        self.assertIn("text/html", index.content_type)
        self.assertIn("設計検討", index.body.decode("utf-8"))
        for name in ("/v2.js", "/grid.js", "/v2.css"):
            self.assertEqual(static_response(name).status, 200)
        # 画面の JS は画面ごとのファイルに分けてある。読み込む物が全部出せて、起動は最後に読む
        scripts = re.findall(r'<script src="([^"]+)"', index.body.decode("utf-8"))
        self.assertEqual(scripts[-1], "boot.js")
        for name in scripts:
            self.assertEqual(static_response("/" + name).status, 200, name)
        for old in ("/v2", "/v2/"):  # 前の版の場所
            self.assertEqual((static_response(old).status, static_response(old).headers["Location"]), (302, "/"))
        for bad in ("/../app.py", "/nothing.js", "/..%2Fapp.py", "/sub/x.js"):
            self.assertEqual(static_response(bad).status, 404, bad)

    def test_no_external_resources(self) -> None:
        web = Path(__file__).resolve().parent.parent / "bomcost" / "web"
        for path in web.iterdir():
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)(src|href)=[\"']?(https?:)?//", path.name)
            self.assertNotIn("@import", text, path.name)

    def test_binds_localhost_and_falls_back_when_port_is_used(self) -> None:
        handler = make_handler(lambda *args: Response(200, b"{}"))
        with socket.socket() as busy:
            busy.bind((HOST, 0))
            busy.listen()
            port = busy.getsockname()[1]
            server = bind(port, handler)
            try:
                self.assertEqual(server.server_address[0], "127.0.0.1")
                self.assertNotEqual(server.server_address[1], port)
            finally:
                server.server_close()


class SecondStart(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "t.sqlite"

    def start(self) -> subprocess.Popen:
        root = Path(__file__).resolve().parent.parent
        p = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "bomcost.app", "--db", str(self.db), "--port", "0", "--no-browser"],
                             cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p

    def wait_port(self) -> int:
        for _ in range(150):
            port = running_port(self.db)
            if port is not None:
                return port
            time.sleep(0.2)
        self.fail("起動しない")

    def test_second_start_opens_the_running_one(self) -> None:
        first = self.start()
        port = self.wait_port()
        with mock.patch.object(app.webbrowser, "open") as opened, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db)]), 0)
        opened.assert_called_once_with(f"http://{HOST}:{port}/")
        first.kill()  # 窓を閉じた時: ファイルは残るが、動いていないと分かる
        first.wait()
        self.assertTrue(port_file(self.db).exists())
        self.assertIsNone(running_port(self.db))
        self.start()
        self.wait_port()

    def test_other_server_on_the_port_is_not_taken_for_ours(self) -> None:
        for body in (b'{"db": "other.sqlite"}', b"[]", b"not json"):
            server = bind(0, make_handler(lambda *args, body=body: Response(200, body)))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port_file(self.db).write_text(str(server.server_address[1]), encoding="ascii")
                self.assertIsNone(running_port(self.db), body)
            finally:
                server.shutdown()
                server.server_close()
        for text in ("", "x", "0", "99999"):
            port_file(self.db).write_text(text, encoding="ascii")
            self.assertIsNone(running_port(self.db), text)

    def test_window_is_minimized_only_after_a_good_start(self) -> None:
        (self.db.parent / "settings.json").write_text("{", encoding="utf-8")
        with mock.patch.object(app, "minimize_console") as minimized, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "--no-browser", "--minimize"]), 1)
        minimized.assert_not_called()
        self.assertFalse(port_file(self.db).exists())

    @unittest.skipUnless(os.name == "nt", "Windows だけ")
    def test_minimize_does_not_raise(self) -> None:
        with mock.patch.object(app.ctypes.windll.user32, "ShowWindow", create=True):
            app.minimize_console()


if __name__ == "__main__":
    unittest.main()
