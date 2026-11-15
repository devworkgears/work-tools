"""設計検討（/v2/）: 語の読み替え（DB と同じフォルダの labels.json）。データは架空。"""

import base64
import json
import tempfile
import unittest
from pathlib import Path

from bomcost.api import Api
from bomcost.store import Store


class LabelsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(Path(self.dir.name) / "t.sqlite")
        self.addCleanup(self.store.close)
        self.api = Api(self.store)

    def write(self, text: str) -> None:
        (Path(self.dir.name) / "labels.json").write_text(text, encoding="utf-8")

    def call(self, method: str, path: str, body: object = None):
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self.api.handle(method, path, "", raw)

    def model_of(self, header: str) -> str:
        csv = f"品番,品名,{header}\nA-100,製品A,ZZ\n".encode("utf-8")
        r = self.call("POST", "/api/v2/bom/import",
                      {"files": [{"filename": "品目.csv", "content": base64.b64encode(csv).decode()}]})
        self.assertEqual(r.status, 200, r.body.decode("utf-8"))
        return self.call("POST", "/api/v2/lookup", {"codes": ["A-100"]}).json()["items"]["A-100"]["model"]

    def test_no_file_gives_no_labels(self) -> None:
        self.assertEqual(self.call("GET", "/api/v2/labels").json(), {"labels": {}})
        self.assertEqual(self.model_of("代表"), "ZZ")
        self.assertEqual(self.model_of("型式"), "")

    def test_file_renames_screen_words_and_import_headers(self) -> None:
        self.write('{"代表": "型式"}')
        self.assertEqual(self.call("GET", "/api/v2/labels").json(), {"labels": {"代表": "型式"}})
        self.assertEqual(self.model_of("型式"), "ZZ")
        self.assertEqual(self.model_of("代表"), "ZZ")  # 元の語の見出しも読める

    def test_memory_db_has_no_labels(self) -> None:
        store = Store(":memory:")
        self.addCleanup(store.close)
        self.assertEqual(Api(store).handle("GET", "/api/v2/labels", "", b"").json(), {"labels": {}})

    def test_broken_file_is_refused_with_a_message(self) -> None:
        for text in ("{", "[]", '{"代表": 1}'):
            self.write(text)
            r = self.call("GET", "/api/v2/labels")
            self.assertEqual(r.status, 400, text)
            self.assertIn("labels.json", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
