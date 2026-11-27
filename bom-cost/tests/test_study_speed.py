"""大きい部品表で待たせないための作り: 部品表の読み出しの控えと、Excel の書き方。データは架空。"""

import io
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

import example_settings  # noqa: F401
from test_study_level import ALL, bom
from bomcost.api import Api
from bomcost.importer import load_openpyxl
from bomcost.model import Item
from bomcost.store import Store
from bomcost.study import StudyStore
from bomcost.studyapi import StudyApi


class KeptBom(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.store = StudyStore(self.conn)
        self.store.replace_bom(bom(), ALL)

    def test_a_copy_is_handed_out(self) -> None:
        # merge_temps は品目と原価を足すので、足した物が次の読み出しに残らない
        got = self.store.load_bom()
        got.add_item(Item("X", "足した品目"))
        self.assertNotIn("X", self.store.load_bom().items)
        self.assertEqual(len(self.store.load_bom().lines), len(bom().lines))

    def test_the_bom_is_read_once_until_the_next_import(self) -> None:
        read: list[str] = []
        self.conn.set_trace_callback(lambda sql: read.append(sql) if sql.startswith("SELECT") and "FROM v2_line" in sql else None)
        self.addCleanup(self.conn.set_trace_callback, None)
        for _ in range(3):
            self.store.load_bom()
            self.store.load_attributes()
        self.assertEqual(len(read), 1)
        self.store.replace_bom(bom(), ALL)
        self.store.load_bom()
        self.assertEqual(len(read), 2)

    def test_changing_the_attributes_does_not_stay(self) -> None:
        self.store.load_attributes()["K"]["model"] = "書き換え"
        self.assertNotEqual(self.store.load_attributes()["K"]["model"], "書き換え")

    def test_another_store_on_the_same_connection_is_seen(self) -> None:
        self.store.load_bom()
        again = bom()
        again.add_item(Item("Z", "取り込み直した品目"))
        StudyStore(self.conn).replace_bom(again, ALL)
        self.assertIn("Z", self.store.load_bom().items)

    def test_another_connection_is_seen(self) -> None:
        # 接続は一時フォルダを消す前に閉じる（Windows は開いているファイルを消せない）
        with tempfile.TemporaryDirectory() as folder, closing(sqlite3.connect(Path(folder) / "t.sqlite")) as a, \
                closing(sqlite3.connect(Path(folder) / "t.sqlite")) as b:
            first = StudyStore(a)
            first.replace_bom(bom(), ALL)
            self.assertNotIn("Z", first.load_bom().items)
            again = bom()
            again.add_item(Item("Z", "別の接続で取り込んだ品目"))
            StudyStore(b).replace_bom(again, ALL)
            self.assertIn("Z", first.load_bom().items)

    def test_dropping_the_bom_from_another_store_is_seen(self) -> None:
        # 設定の画面は別の StudyStore で部品表を消す
        self.store.load_bom()
        with self.conn:
            self.conn.execute("DELETE FROM v2_bom_settings")
            self.conn.execute("INSERT INTO v2_bom_settings VALUES ('{\"old\": 1}')")
        self.assertTrue(StudyStore(self.conn).drop_old_bom())
        self.assertEqual(self.store.load_bom().items, {})


class ExcelRows(unittest.TestCase):
    def test_many_rows_are_written_quickly_and_stay_text(self) -> None:
        # 1行ごとにシートの全セルを数え直すと、行の数の2乗で遅くなる（この大きさで十秒を超える・直した後は1秒ほど）
        store = Store(":memory:")
        self.addCleanup(store.close)
        rows = [[f"=P{i}", i, "名称", *range(10)] for i in range(9000)]
        start = time.perf_counter()
        r = StudyApi(Api(store))._xlsx("t", [("構成", ["品番", "数", "名称", *map(str, range(10))], rows)])
        self.assertLess(time.perf_counter() - start, 5)
        sheet = load_openpyxl().load_workbook(io.BytesIO(r.body))["構成"]
        self.assertEqual((sheet["A9001"].value, sheet["A9001"].data_type), ("=P8999", "s"))


if __name__ == "__main__":
    unittest.main()
