"""風船番号の場所ごとの置換（1か所を複数の部品に）と、その記録・取り消し・古い DB の読み戻し。品番・数値は架空。"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bomcost import BomLine, CostHead, CostRow, Item, Version
from bomcost.api import Api
from bomcost.store import Store
from test_screens import EmptyDbCase

NAME = "場所の検討"
M = CostHead.MATERIAL


def balloon_version() -> Version:
    """A-100: #3 C-300 ×2・#7 C-300 ×1・#5 S-500 ×2。S-500 は風船番号のある行と無い行が混ざる。"""
    v = Version("2026-11")
    v.add_items([Item("A-100", "製品A", "P", has_children=1), Item("S-500", "組品S", "B", has_children=1)]
                + [Item(c, f"部品{c}", "B") for c in ("C-300", "B-200", "N-999", "N-998")])
    v.add_bom_lines([
        BomLine("A-100", "3", "C-300", 2),
        BomLine("A-100", "7", "C-300", 1),
        BomLine("A-100", "5", "S-500", 2),
        BomLine("S-500", "1", "C-300", 1),
        BomLine("S-500", None, "B-200", 1),
        BomLine("S-500", "2", "B-200", 3),
    ])
    v.add_costs([CostRow("C-300", {M: 500}), CostRow("B-200", {M: 1000}), CostRow("N-999", {M: 100}),
                 CostRow("N-998", {M: 30})])
    return v


# 今の原価: C-300 は 2＋1＋2×1＝5 個（2,500）、B-200 は 2×(1＋3)＝8 個（8,000）
BASE_TOTAL = "10500"


class BalloonCase(EmptyDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.save_version(balloon_version())

    def change(self, **change: object) -> dict:
        return self.ok("POST", "/api/group/changes", body={"version_id": "2026-11", "name": NAME, "product": "A-100",
                                                           "head": "MATERIAL", **change})

    def tree(self, query: str = "") -> dict:
        return self.ok("GET", "/api/group/tree", f"version=2026-11&product=A-100&name={NAME}&by_balloon=1" + query)

    def total(self) -> str:
        return self.ok("GET", "/api/group/products", f"version=2026-11&name={NAME}&q=A-100")["products"][0]["after"]

    def changes(self) -> dict:
        return self.ok("GET", "/api/group/changes", f"version=2026-11&name={NAME}")


class ReplaceAtBalloon(BalloonCase):
    def test_tree_rows_by_balloon(self) -> None:
        rows = [(c["balloon"], c["code"], c["quantity"]) for c in self.tree()["children"]]
        self.assertEqual(rows, [("3", "C-300", "2"), ("7", "C-300", "1"), ("5", "S-500", "2")])
        merged = self.ok("GET", "/api/group/tree", "version=2026-11&product=A-100")["children"]
        self.assertEqual([(c["balloon"], c["code"], c["quantity"]) for c in merged],
                         [(None, "C-300", "3"), (None, "S-500", "2")])  # 付けなければ今のまま

    def test_one_place_to_several_parts(self) -> None:
        r = self.change(kind="REPLACE", part="C-300", parent="A-100", balloon="7",
                        replacements=[{"code": "N-999", "quantity": "1"}, {"code": "N-998", "quantity": "2"}])
        # #7 の C-300 ×1 を抜き、N-999 ×1・N-998 ×2 を入れる: 10,500 − 500 ＋ 100 ＋ 60。#3 の C-300 は残る
        self.assertEqual(r["summary"]["total"], "10160")
        history = r["summary"]["history"]
        self.assertEqual([(h["kind"], h["part"], h["replacement"], h["quantity"], h["parent"], h["balloon"], h["batch"])
                          for h in history],
                         [("REPLACE", "C-300", "N-999", "1", "A-100", "7", 1),
                          ("REPLACE", "C-300", "N-998", "2", "A-100", "7", 1)])
        c = self.changes()
        self.assertEqual([(x["text"], x["cost_before"], x["cost_after"], x["difference"], x["balloon"]) for x in c["rows"]],
                         [("#7 C-300 → N-999 ×1・N-998 ×2", BASE_TOTAL, "10160", "-340", "7")])
        self.assertEqual((c["count"], c["difference"]), (1, "-340"))
        # 同じ場所はもう置換できない。#3 は別の場所なので置換できる
        self.assert_error(self.call("POST", "/api/group/changes", body={
            "version_id": "2026-11", "name": NAME, "product": "A-100", "kind": "REPLACE", "part": "C-300",
            "parent": "A-100", "balloon": "7", "replacement": "N-999"}), 400, "もう置換した")
        self.assertEqual(self.change(kind="REPLACE", part="C-300", parent="A-100", balloon="3",
                                     replacement="N-999", quantity="2")["summary"]["total"], "9360")  # −1,000＋200
        self.assertEqual([x["text"] for x in self.changes()["rows"]],
                         ["#7 C-300 → N-999 ×1・N-998 ×2", "#3 C-300 → N-999 ×2"])

        # 取り消しは1回の置換のまとまりを丸ごと戻す
        sid = r["scenario_id"]
        self.assertEqual(self.ok("POST", f"/api/scenarios/{sid}/undo", body={})["total"], "10160")
        undone = self.ok("POST", f"/api/scenarios/{sid}/undo", body={})
        self.assertEqual((undone["total"], undone["history"]), (BASE_TOTAL, []))

    def test_place_in_sub_assembly_uses_parent_usage(self) -> None:
        # S-500 は製品1台に 2 個。#1 の C-300 ×1 を N-998 ×3 に: −500×1×2 ＋ 30×3×2
        r = self.change(kind="REPLACE", part="C-300", parent="S-500", balloon="1", replacement="N-998", quantity="3")
        self.assertEqual(r["summary"]["total"], "9680")

    def test_mixed_rows_with_and_without_balloon(self) -> None:
        rows = [(c["balloon"], c["code"], c["quantity"]) for c in self.tree("&code=S-500")["children"]]
        self.assertEqual(rows, [("1", "C-300", "1"), (None, "B-200", "1"), ("2", "B-200", "3")])
        # 風船番号の無い行の B-200 ×1 だけを N-999 ×2・N-998 ×1 に（S-500 は 2 個）: −1,000×2 ＋ 100×2×2 ＋ 30×1×2
        r = self.change(kind="REPLACE", part="B-200", parent="S-500",
                        replacements=[{"code": "N-999", "quantity": "2"}, {"code": "N-998", "quantity": "1"}])
        self.assertEqual(r["summary"]["total"], "8960")
        self.assertEqual({(h["parent"], h["balloon"]) for h in r["summary"]["history"]}, {("S-500", None)})
        self.assertEqual([x["text"] for x in self.changes()["rows"]], ["B-200 → N-999 ×2・N-998 ×1"])
        # #2 の B-200 は残っていて、別に置換できる
        self.assertEqual(self.change(kind="REPLACE", part="B-200", parent="S-500", balloon="2",
                                     replacement="N-999", quantity="3")["summary"]["total"], "3560")  # −6,000＋600

    def test_without_place_is_replace_by_code(self) -> None:
        r = self.change(kind="REPLACE", part="C-300", replacement="N-999", quantity="5")
        self.assertEqual(r["summary"]["total"], "8500")  # 5×(100−500)
        self.assertEqual((r["summary"]["history"][0]["parent"], r["summary"]["history"][0]["batch"]), (None, None))
        self.assertEqual(self.changes()["rows"][0]["text"], "C-300 → N-999（×5）")

    def test_input_errors(self) -> None:
        def bad(words: str, **change: object) -> None:
            self.assert_error(self.call("POST", "/api/group/changes", body={
                "version_id": "2026-11", "name": NAME, "product": "A-100", **change}), 400, words)

        bad("構成に無い", kind="REPLACE", part="C-300", parent="A-100", balloon="9", replacement="N-999")
        bad("構成に無い", kind="REPLACE", part="C-300", parent="A-100", replacement="N-999")  # 風船番号の無い行は無い
        bad("親品番", kind="REPLACE", part="C-300", replacements=[{"code": "N-999"}, {"code": "N-998"}])
        bad("土台", kind="REPLACE", part="C-300", parent="N-999", replacement="N-998")
        bad("置換だけ", kind="ADD", part="C-300", parent="A-100", balloon="7")
        bad("親品番", kind="REPLACE", part="C-300", balloon="7", replacement="N-999")
        bad("代わりの品番", kind="REPLACE", part="C-300", parent="A-100", balloon="7", replacements=[{"code": "ZZ"}])
        bad("replacements", kind="REPLACE", part="C-300", parent="A-100", balloon="7", replacements=[])
        self.assertEqual(self.store.list_scenarios(), [])


class OldDatabaseOpens(unittest.TestCase):
    def test_db_without_new_columns_opens_with_same_costs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite"
            with Store(path) as store:
                store.save_version(balloon_version())
                api = Api(store)
                for change in ({"kind": "REPLACE", "part": "C-300", "replacement": "N-999", "quantity": "3"},
                               {"kind": "ADD", "part": "N-998", "quantity": "2"}):
                    api.handle("POST", "/api/group/changes", "", _body(change))
                before = api.handle("GET", "/api/group/changes", f"version=2026-11&name={NAME}").json()
            _drop_new_columns(path)
            conn = sqlite3.connect(str(path))
            self.assertNotIn("batch", {r[1] for r in conn.execute("PRAGMA table_info(change)")})
            conn.close()
            with Store(path) as store:
                api = Api(store)
                after = api.handle("GET", "/api/group/changes", f"version=2026-11&name={NAME}").json()
                products = api.handle("GET", "/api/group/products", f"version=2026-11&name={NAME}&q=A-100").json()
                # 開くと列が足され、今ある行は空のまま
                cols = {r[1] for r in store._conn.execute("PRAGMA table_info(change)")}
                nulls = store._conn.execute("SELECT parent, balloon, batch FROM change").fetchall()
        self.assertEqual(after, before)
        self.assertEqual(products["products"][0]["after"], "9360")  # 10,500 ＋ 3×(100−500) ＋ 2×30
        self.assertTrue({"parent", "balloon", "batch"} <= cols)
        self.assertEqual(nulls, [(None, None, None)] * 2)


def _body(change: dict) -> bytes:
    return json.dumps({"version_id": "2026-11", "name": NAME, "product": "A-100", "head": "MATERIAL", **change},
                      ensure_ascii=False).encode("utf-8")


def _drop_new_columns(path: Path) -> None:
    """change の表を、列を足す前の形に作り直す。"""
    conn = sqlite3.connect(str(path))
    with conn:
        conn.executescript(
            """
            CREATE TABLE change_old (
                scenario_id INTEGER NOT NULL REFERENCES scenario(scenario_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL, recorded_at TEXT NOT NULL, kind TEXT NOT NULL, part TEXT NOT NULL,
                replacement TEXT, quantity TEXT NOT NULL, cost_before TEXT, cost_after TEXT,
                version_id TEXT NOT NULL REFERENCES version(version_id), head TEXT NOT NULL, volume TEXT,
                allocation TEXT, PRIMARY KEY (scenario_id, sequence));
            INSERT INTO change_old SELECT scenario_id, sequence, recorded_at, kind, part, replacement, quantity,
                cost_before, cost_after, version_id, head, volume, allocation FROM change;
            DROP TABLE change;
            ALTER TABLE change_old RENAME TO change;
            """
        )
    conn.close()
