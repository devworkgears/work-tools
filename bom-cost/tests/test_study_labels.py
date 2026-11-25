"""設計検討（/v2/）: 語の読み替え（設定の labels）。データは架空。"""

import base64
import json
import tempfile
import unittest
from pathlib import Path

import example_settings
from bomcost import settings
from bomcost.api import Api
from bomcost.store import Store


class LabelsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(Path(self.dir.name) / "t.sqlite")
        self.addCleanup(self.store.close)
        self.api = Api(self.store)

    def write(self, labels: dict[str, str]) -> None:
        self.addCleanup(settings.use, example_settings.EXAMPLE)
        settings.use(settings.Settings(labels=labels))

    def call(self, method: str, path: str, body: object = None):
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self.api.handle(method, path, "", raw)

    def model_of(self, header: str) -> str:
        csv = f"品番,品名,{header}\nA-100,製品A,ZZ\n".encode("utf-8")
        r = self.call("POST", "/api/v2/bom/import",
                      {"files": [{"filename": "品目.csv", "content": base64.b64encode(csv).decode()}]})
        self.assertEqual(r.status, 200, r.body.decode("utf-8"))
        return self.call("POST", "/api/v2/lookup", {"codes": ["A-100"]}).json()["items"]["A-100"]["model"]

    def test_no_labels_by_default(self) -> None:
        self.assertEqual(self.call("GET", "/api/v2/labels").json(), {"labels": {}})
        self.assertEqual(self.model_of("代表"), "ZZ")
        self.assertEqual(self.model_of("型式"), "")

    def test_labels_rename_screen_words_and_import_headers(self) -> None:
        self.write({"代表": "型式"})
        self.assertEqual(self.call("GET", "/api/v2/labels").json(), {"labels": {"代表": "型式"}})
        self.assertEqual(self.model_of("型式"), "ZZ")
        self.assertEqual(self.model_of("代表"), "ZZ")  # 元の語の見出しも読める


if __name__ == "__main__":
    unittest.main()
