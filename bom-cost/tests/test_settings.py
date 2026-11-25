"""設定（settings.json）: 読み書き・形の確かめ・率が空の時・版に残る率・設定の画面の API。値は架空。"""

import contextlib
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import example_settings
from bomcost import Store, settings
from bomcost.api import Api
from bomcost.app import main, static_response
from bomcost.formula import Head, OwnValues, breakdown
from bomcost.model import base_code, mbom_to_ebom
from bomcost.study import StudyStore

D = Decimal


class SettingsCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(settings.use, example_settings.EXAMPLE)
        self.dir = Path(tmp.name)
        self.db = self.dir / "t.sqlite"
        self.file = self.dir / "settings.json"


class Parse(SettingsCase):
    def test_blank_values_are_unset(self) -> None:
        self.assertEqual(settings.parse({}), settings.Settings())
        self.assertEqual(settings.parse({"adjustment_percent": "", "indirect_percent": None, "product_kind": " ",
                                         "mold_years": "", "labels": {"a": "", " ": "b"}}), settings.Settings())

    def test_round_trip(self) -> None:
        full = settings.Settings(D("2.5"), D(10), 9, 6, 2, "S", "T", D(4), {"系列": "型式"})
        self.assertEqual(settings.parse(json.loads(json.dumps(full.to_json()))), full)

    def test_wrong_values_are_refused(self) -> None:
        for data, word in (([], "形"), ({"rate": 1}, "rate"), ({"adjustment_percent": "x", "indirect_percent": 1}, "数で"),
                           ({"adjustment_percent": 100, "indirect_percent": 1}, "100 未満"),
                           ({"adjustment_percent": "NaN", "indirect_percent": 1}, "数で"),
                           ({"adjustment_percent": True, "indirect_percent": 1}, "数で"),
                           ({"adjustment_percent": 5}, "両方"), ({"code_total": 9}, "3つ"),
                           ({"code_total": 7, "code_base": 6, "code_branch": 2}, "合計以上"),
                           ({"code_total": 9.5, "code_base": 6, "code_branch": 2}, "整数"),
                           ({"product_kind": "X", "part_kind": "X"}, "別の物"), ({"product_kind": "部品"}, "別の物"),
                           ({"mold_years": 0}, "正の数"), ({"labels": {"系列": 1}}, "読み替え"), ({"labels": ["a"]}, "読み替え")):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, word):
                settings.parse(data)


class LoadAndSave(SettingsCase):
    def test_no_file_is_empty_and_calculation_is_refused(self) -> None:
        self.assertEqual(settings.load(self.db), settings.Settings())
        with self.assertRaises(settings.SettingsMissing):
            breakdown(D(100), OwnValues())
        self.assertNotIsInstance(settings.SettingsMissing(""), ValueError)  # 値の誤りとして未登録に紛れない

    def test_saved_file_is_read_back(self) -> None:
        settings.save(self.db, example_settings.EXAMPLE)
        settings.use(settings.Settings())
        self.assertEqual(settings.load(self.db), example_settings.EXAMPLE)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["settings.json"])

    def test_file_saved_by_notepad_with_bom_is_read(self) -> None:
        self.file.write_text('{"adjustment_percent": 10, "indirect_percent": 20}', encoding="utf-8-sig")
        settings.load(self.db)
        self.assertEqual(breakdown(D(100), OwnValues()).value(Head.MANUFACTURING), D(132))

    def test_broken_file_stops_the_start(self) -> None:
        self.file.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "settings.json"):
            settings.load(self.db)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["--db", str(self.db), "--no-browser"]), 1)
        self.assertIn("settings.json", err.getvalue())

    def test_memory_db_keeps_settings_without_a_file(self) -> None:
        settings.save(None, settings.Settings(product_kind="Z"))
        self.assertEqual(settings.product_kind(), "Z")


class Codes(SettingsCase):
    def test_without_digits_codes_are_kept(self) -> None:
        settings.use(settings.Settings())
        for code in ("AB1234000", "A-1"):
            self.assertEqual((mbom_to_ebom(code), base_code(code)), (code, code))

    def test_other_digits(self) -> None:
        settings.use(settings.Settings(code_total=8, code_base=4, code_branch=3))
        self.assertEqual(mbom_to_ebom("A1230000"), "A123")
        self.assertEqual(mbom_to_ebom("A1230100"), "A123010")
        for code in ("A1230101", "A123010"):
            with self.assertRaises(ValueError):
                mbom_to_ebom(code)


class SettingsApi(SettingsCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.api = Api(self.store)

    def call(self, method: str, path: str, body: object = None):
        return self.api.handle(method, path, "", b"" if body is None else json.dumps(body).encode("utf-8"))

    def test_put_writes_the_file_and_takes_effect(self) -> None:
        new = {"adjustment_percent": "5", "indirect_percent": "10", "labels": {"系列": "型式"}}
        got = self.call("PUT", "/api/settings", {"settings": new}).json()
        self.assertEqual((got["settings"]["adjustment_percent"], got["exists"], got["file"]), ("5", True, str(self.file)))
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8"))["labels"], {"系列": "型式"})
        self.assertEqual(settings.rates(), settings.Rates(D("0.05"), D("0.1")))
        self.assertEqual(self.call("GET", "/api/v2/labels").json(), {"labels": {"系列": "型式"}})

    def test_check_and_wrong_values_change_nothing(self) -> None:
        self.assertEqual(self.call("PUT", "/api/settings", {"settings": {"product_kind": "Q"}, "check": True}).status, 200)
        self.assertEqual(self.call("PUT", "/api/settings", {"settings": {"unknown": 1}}).status, 400)
        self.assertFalse(self.file.exists())
        self.assertEqual(settings.current(), example_settings.EXAMPLE)

    def test_missing_rates_answer_409_with_a_message(self) -> None:
        self.call("POST", "/api/v2/bom/import", {"sample": True})
        settings.use(settings.Settings())
        r = self.call("POST", "/api/v2/lookup", {"codes": ["PR1001-001"]})
        self.assertEqual(r.status, 409)
        self.assertIn("settings.json", r.json()["error"])

    def counts(self, api: Api | None = None) -> int:
        r = (api or self.api).handle("GET", "/api/v2/bom", "", b"")
        return sum(r.json()["counts"].values())

    def save(self, **changes: object) -> bool:
        r = self.call("PUT", "/api/settings", {"settings": {**settings.current().to_json(), **changes}})
        self.assertEqual(r.status, 200)
        return r.json()["bom_dropped"]

    def test_changed_settings_drop_the_imported_parts(self) -> None:
        self.call("POST", "/api/v2/bom/import", {"sample": True})
        full = self.counts()
        self.assertGreater(full, 0)
        study = StudyStore(self.store.connection)
        study.save_columns(("a", "b"), "volume", {"product": "a", "annual": "b"})
        self.assertFalse(self.save())  # 同じ中身の保存では消さない
        self.assertEqual(self.counts(), full)
        self.assertIsNotNone(study.find_columns(("a", "b"), "volume"))
        self.assertTrue(self.save(labels={"系列": "型式"}))
        self.assertEqual(self.counts(), 0)
        self.assertIsNone(study.find_columns(("a", "b"), "volume"))

    def test_studies_and_versions_survive_the_drop(self) -> None:
        self.call("POST", "/api/v2/bom/import", {"sample": True})
        number = self.call("POST", "/api/v2/studies", {"products": ["PR1001-001"]}).json()["header"]["number"]
        own = {"rows": [{"product": "PR1001-001", "head": "processing", "value": "999"}]}

        def call(method: str, path: str, body: object = None, query: str = "") -> dict:
            r = self.api.handle(method, path, f"number={number}{query}", b"" if body is None else json.dumps(body).encode())
            self.assertEqual(r.status, 200, r.body)
            return r.json()

        def calc(query: str = "") -> list:
            return call("GET", "/api/v2/calc", None, "&head=total" + query)["rows"]

        call("PUT", "/api/v2/own", own)
        before = calc()
        self.assertNotEqual(before[0]["now"], before[0]["plans"]["A"])
        call("POST", "/api/v2/release", {})
        self.assertTrue(self.save(mold_years="4"))
        self.assertEqual(calc("&version=1"), before)
        self.assertNotEqual(calc(), before)
        self.call("POST", "/api/v2/bom/import", {"sample": True})
        self.assertEqual(calc(), before)

    def test_replaced_file_drops_the_parts_at_the_next_start(self) -> None:
        self.save(mold_years="4")
        self.call("POST", "/api/v2/bom/import", {"sample": True})
        full = self.counts()
        settings.load(self.db)
        self.assertEqual(self.counts(Api(self.store)), full)  # 同じ設定で起動し直す
        settings.write(self.file, settings.parse({**settings.current().to_json(), "code_total": 9, "code_base": 6, "code_branch": 3}))
        settings.load(self.db)
        self.assertEqual(self.counts(Api(self.store)), 0)

    def test_db_made_before_this_keeps_its_parts(self) -> None:
        self.call("POST", "/api/v2/bom/import", {"sample": True})
        full = self.counts()
        self.store.connection.execute("DELETE FROM v2_bom_settings")
        self.store.connection.commit()
        self.assertEqual(self.counts(Api(self.store)), full)
        self.assertTrue(self.save(mold_years="4"))
        self.assertEqual(self.counts(), 0)

    def test_release_keeps_the_rates_it_was_taken_with(self) -> None:
        settings.use(settings.Settings(D(10), D(20)))
        self.call("POST", "/api/v2/bom/import", {"sample": True})
        number = self.call_q("POST", "/api/v2/studies", "", {"products": ["PR1001-001"]})["header"]["number"]
        before = self.calc(f"number={number}")
        version = self.call_q("POST", "/api/v2/release", f"number={number}", {})["version"]
        settings.use(settings.Settings(D(5), D(10)))
        draft, kept = self.calc(f"number={number}"), self.calc(f"number={number}&version={version}")
        self.assertEqual(kept["rates"], {"adjustment": "10", "indirect": "20"})
        self.assertEqual(draft["rates"], {"adjustment": "5", "indirect": "10"})
        self.assertEqual(kept["rows"], before["rows"])
        self.assertNotEqual(draft["rows"], before["rows"])

    def call_q(self, method: str, path: str, query: str, body: object = None) -> dict:
        r = self.api.handle(method, path, query, b"" if body is None else json.dumps(body).encode("utf-8"))
        self.assertEqual(r.status, 200, r.body.decode("utf-8"))
        return r.json()

    def calc(self, query: str) -> dict:
        return self.call_q("GET", "/api/v2/calc", query + "&head=total")


class Page(unittest.TestCase):
    def test_page_is_served_and_not_linked(self) -> None:
        self.assertEqual(static_response("/settings").status, 301)
        for name in ("", "settings.js"):
            self.assertEqual(static_response(f"/settings/{name}").status, 200, name)
        for bad in ("/settings/../v2.js", "/settings/v2.js", "/v2/settings/settings.js", "/settings/settings/settings.js"):
            self.assertEqual(static_response(bad).status, 404, bad)
        web = Path(__file__).resolve().parent.parent / "bomcost" / "web"
        for path in web.glob("*.*"):
            self.assertNotIn("/settings", path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
