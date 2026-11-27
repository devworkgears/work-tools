"""大きい部品表で待たせないための作り: 部品表の読み出しの控えと、Excel の書き方。データは架空。"""

import io
import sqlite3
import tempfile
import time
import unittest
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

    def test_another_store_on_the_same_connection_is_seen(self) -> None:
        self.store.load_bom()
        again = bom()
        again.add_item(Item("Z", "取り込み直した品目"))
        StudyStore(self.conn).replace_bom(again, ALL)
        self.assertIn("Z", self.store.load_bom().items)

    def test_another_connection_is_seen(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "t.sqlite"
            a, b = sqlite3.connect(path), sqlite3.connect(path)
            self.addCleanup(a.close)
            self.addCleanup(b.close)
            first = StudyStore(a)
            first.replace_bom(bom(), ALL)
            self.assertNotIn("Z", first.load_bom().items)
            again = bom()
            again.add_item(Item("Z", "別の接続で取り込んだ品目"))
            StudyStore(b).replace_bom(again, ALL)
            self.assertIn("Z", first.load_bom().items)

    def test_dropping_the_bom_is_seen(self) -> None:
        self.store.load_bom()
        with self.conn:
            self.conn.execute("DELETE FROM v2_bom_settings")
            self.conn.execute("INSERT INTO v2_bom_settings VALUES ('{\"old\": 1}')")
        self.assertTrue(self.store.drop_old_bom())
        self.assertEqual(self.store.load_bom().items, {})


class ExcelRows(unittest.TestCase):
    def test_many_rows_are_written_quickly_and_stay_text(self) -> None:
        # 1行ごとにシートの全セルを数え直すと、行の数の2乗で遅くなる（この大きさで7秒ほど・直した後は1秒かからない）
        store = Store(":memory:")
        self.addCleanup(store.close)
        rows = [[f"=P{i}", i, "名称", *range(10)] for i in range(6000)]
        start = time.perf_counter()
        r = StudyApi(Api(store))._xlsx("t", [("構成", ["品番", "数", "名称", *map(str, range(10))], rows)])
        self.assertLess(time.perf_counter() - start, 5)
        sheet = load_openpyxl().load_workbook(io.BytesIO(r.body))["構成"]
        self.assertEqual((sheet["A6001"].value, sheet["A6001"].data_type), ("=P5999", "s"))


if __name__ == "__main__":
    unittest.main()
