"""製造原価の式（材料費小計から調整費・材料費合計・間接費・製造原価）と、検討案の費目変更。品番・数値は架空。"""

import io
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from bomcost import BomLine, CostHead, CostRow, Item, Version
from bomcost.api import Api
from bomcost.formula import ADJUSTMENT_RATE, INDIRECT_RATE, Head, OwnValues, breakdown
from bomcost.importer import load_openpyxl
from bomcost.rollup import calculate_cost
from bomcost.scenario import Change, ChangeKind, Scenario
from bomcost.store import Store
from test_group import HAS_OPENPYXL, NAME, SampleDbCase

M = CostHead.MATERIAL
D = Decimal


def values(b) -> dict[str, Decimal | None]:
    return {h.name: b.values[h] for h in Head}


class Formula(unittest.TestCase):
    def test_each_head_by_hand(self) -> None:
        b = breakdown(D(1000), OwnValues(D(0), D(100), D(50)))
        self.assertEqual(values(b), {
            "MATERIAL": D(1000), "PROCESSING": D(0), "AMORTIZATION": D(100),
            "ADJUSTMENT": D(33),  # (1000 ＋ 0 ＋ 100) × 0.03
            "MATERIAL_TOTAL": D(1133),
            "INDIRECT": D("124.63"),  # 1133 × 0.11
            "DIRECT": D(50),
            "MANUFACTURING": D("1307.63"),  # 1133 ＋ 124.63 ＋ 50
        })
        self.assertEqual(b.incomplete, frozenset())
        # 端数は丸めない。社内加工費も調整費の元に入る
        c = breakdown(D("10.01"), OwnValues(D(5), D(0), D(0)))
        self.assertEqual(c.value(Head.ADJUSTMENT), D("0.4503"))
        self.assertEqual(c.value(Head.MANUFACTURING), D("15.4603") * D("1.11"))
        self.assertEqual((ADJUSTMENT_RATE, INDIRECT_RATE), (D("0.03"), D("0.11")))
        # 表示の文字は末尾の 0 を落とすが指数表記にしない
        self.assertEqual(format(breakdown(D(1000), OwnValues(D(0), D(0), D(0))).value(Head.MATERIAL_TOTAL), "f"), "1030")

    def test_material_missing_makes_dependents_missing(self) -> None:
        b = breakdown(None, OwnValues(D(0), D(100), D(50)))
        self.assertEqual(values(b), {
            "MATERIAL": None, "PROCESSING": D(0), "AMORTIZATION": D(100), "ADJUSTMENT": None,
            "MATERIAL_TOTAL": None, "INDIRECT": None, "DIRECT": D(50), "MANUFACTURING": None,
        })

    def test_amortization_and_direct_missing_count_as_zero_with_mark(self) -> None:
        b = breakdown(D(1000), OwnValues(D(0), None, None))
        self.assertEqual((b.value(Head.AMORTIZATION), b.value(Head.DIRECT)), (None, None))
        self.assertEqual(b.value(Head.ADJUSTMENT), D(30))
        self.assertEqual(b.value(Head.MANUFACTURING), D("1143.3"))  # 1030 × 1.11
        self.assertEqual({h.name for h in b.incomplete},
                         {"AMORTIZATION", "ADJUSTMENT", "MATERIAL_TOTAL", "INDIRECT", "DIRECT", "MANUFACTURING"})
        only_direct = breakdown(D(1000), OwnValues(D(0), D(0), None))
        self.assertEqual({h.name for h in only_direct.incomplete}, {"DIRECT", "MANUFACTURING"})
        # 償却費を無視すると 0 として通し、印を付けない（欄は空）
        ignored = breakdown(D(1000), OwnValues(D(0), None, D(0)), ignore_amortization=True)
        self.assertEqual((ignored.value(Head.AMORTIZATION), ignored.value(Head.MANUFACTURING)), (None, D("1143.3")))
        self.assertEqual(ignored.incomplete, frozenset())
        ignored_registered = breakdown(D(1000), OwnValues(D(0), D(500), D(0)), ignore_amortization=True)
        self.assertEqual(ignored_registered.value(Head.ADJUSTMENT), D(30))
        # 材料費小計が一部未登録なら、それに依る費目も一部未登録
        partial = breakdown(D(1000), OwnValues(D(0), D(0), D(0)), material_incomplete=True)
        self.assertEqual({h.name for h in partial.incomplete},
                         {"MATERIAL", "ADJUSTMENT", "MATERIAL_TOTAL", "INDIRECT", "MANUFACTURING"})


def small_version() -> Version:
    """P（材料費は未登録で子を足す・償却費 10・直接費 5）→ C ×2。C・D は部品の製造原価も持つ（計算には使わない）。"""
    v = Version("t")
    v.add_items([Item("P", "製品", "P"), Item("C", "部品C", "B"), Item("D", "部品D", "B")])
    v.add_bom_lines([BomLine("P", "1", "C", 2)])
    v.add_costs([
        CostRow("P", {CostHead.AMORTIZATION: 10, CostHead.DIRECT: 5, CostHead.MANUFACTURING: 99999,
                      CostHead.INDIRECT: 7777, CostHead.ADJUSTMENT: 6666}),
        CostRow("C", {M: 100, CostHead.DIRECT: 30, CostHead.MANUFACTURING: 999}),
        CostRow("D", {M: 150, CostHead.DIRECT: 40, CostHead.MANUFACTURING: 5000}),
    ])
    return v


class RollupOnlyMaterial(unittest.TestCase):
    def test_parts_manufacturing_is_not_added_to_product(self) -> None:
        v = small_version()
        result = calculate_cost(v, "P", CostHead.MANUFACTURING)
        # 材料費小計 200（2×100）・償却費 10 → 材料費合計 216.3・間接費 23.793・直接費 5
        self.assertEqual(result.total, D("245.093"))
        self.assertEqual(calculate_cost(v, "P", CostHead.DIRECT).total, D(5))  # 子の 2×30 を足さない
        self.assertEqual(calculate_cost(v, "P", CostHead.INDIRECT).total, D("23.793"))
        # 部品にも同じ式を当てる（その値は親の計算に使わない）: 100 × 1.03 × 1.11 ＋ 30
        self.assertEqual(result.item_costs["C"], D("144.33"))

    def test_swapping_parts_moves_only_material(self) -> None:
        v = small_version()
        s = Scenario("s", "P", "P#", v)
        before = {h: s.calculate(head=h).total for h in (M, CostHead.DIRECT, CostHead.MANUFACTURING)}
        s.apply(Change(ChangeKind.REPLACE, "C", 2, "D"))
        s.apply(Change(ChangeKind.ADD, "D", 1))
        after = {h: s.calculate(head=h).total for h in (M, CostHead.DIRECT, CostHead.MANUFACTURING)}
        self.assertEqual(after[M] - before[M], D(250))  # 2×(150−100) ＋ 150
        self.assertEqual(after[CostHead.DIRECT], before[CostHead.DIRECT])
        self.assertEqual(after[CostHead.MANUFACTURING] - before[CostHead.MANUFACTURING], D(250) * D("1.03") * D("1.11"))

    def test_set_head_changes_product_values(self) -> None:
        v = small_version()
        s = Scenario("s", "P", "P#", v, CostHead.MANUFACTURING)
        lines = s.root_lines()
        record = s.apply(Change(ChangeKind.SET_HEAD, "P", 80, target_head="DIRECT"))
        self.assertEqual(record.difference, D(75))  # 直接費 5 → 80
        s.apply(Change(ChangeKind.SET_HEAD, "P", 30, target_head="PROCESSING"))
        s.apply(Change(ChangeKind.SET_HEAD, "P", 0, target_head="AMORTIZATION"))
        self.assertEqual(s.root_lines(), lines)  # 構成は変えない
        self.assertEqual(s.own_values(), OwnValues(D(30), D(0), D(80)))
        # (200 ＋ 30 ＋ 0) × 1.03 × 1.11 ＋ 80
        self.assertEqual(s.calculate().total, D(230) * D("1.03") * D("1.11") + 80)
        self.assertEqual(s.calculate(head=M).total, D(200))
        s.undo()
        self.assertEqual(s.own_values().amortization, D(10))

    def test_set_head_checks(self) -> None:
        for bad in (dict(kind=ChangeKind.SET_HEAD, part="P", quantity=1),
                    dict(kind=ChangeKind.SET_HEAD, part="P", quantity=1, target_head="INDIRECT"),
                    dict(kind=ChangeKind.SET_HEAD, part="P", quantity=-1, target_head="DIRECT"),
                    dict(kind=ChangeKind.ADD, part="C", quantity=1, target_head="DIRECT")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Change(**bad)


class SampleApi(SampleDbCase):
    """samples/ の A-100: 材料費 200・償却費と直接費は未登録。"""

    def compare(self, query: str = "") -> dict:
        return self.ok("GET", "/api/group/compare", f"version=2026-10&names={quote(NAME)}&ignore_amortization=1" + query)

    def test_compare_eight_heads_in_order(self) -> None:
        self.change("A-100", kind="REPLACE", part="B-200", replacement="X-610", quantity="2")
        r = self.compare("&q=A-100")
        self.assertEqual([(h["id"], h["label"]) for h in r["heads"]], [
            ("MATERIAL", "材料費小計"), ("PROCESSING", "社内加工費"), ("AMORTIZATION", "償却費"), ("ADJUSTMENT", "調整費"),
            ("MATERIAL_TOTAL", "材料費合計"), ("INDIRECT", "間接費"), ("DIRECT", "直接費"), ("MANUFACTURING", "製造原価")])
        base, trial = r["rows"]
        # 200 → 材料費合計 206・間接費 22.66・製造原価 228.66（直接費は未登録で 0 として通す）
        self.assertEqual((base["costs"]["MATERIAL_TOTAL"], base["costs"]["INDIRECT"], base["costs"]["MANUFACTURING"]),
                         ("206", "22.66", "228.66"))
        self.assertEqual(base["costs"]["PROCESSING"], "0")
        self.assertIn("MANUFACTURING", base["incomplete"])
        self.assertNotIn("AMORTIZATION", base["incomplete"])  # 無視した償却費は印を付けない
        # 製造原価の差は 材料費の差 × 1.03 × 1.11
        self.assertEqual(D(trial["differences"]["MANUFACTURING"]), D(-2800) * D("1.03") * D("1.11"))
        self.assertEqual(trial["differences"]["PROCESSING"], "0")
        # ②の製品の行の差も同じ計算
        rows = {p["code"]: p for p in self.products(f"&name={NAME}&head=MANUFACTURING")["products"]}
        self.assertEqual(rows["A-100"]["difference"], trial["differences"]["MANUFACTURING"])

    def test_set_head_from_group_listed_and_undone(self) -> None:
        r = self.change("A-100", kind="SET_HEAD", part="A-100", target_head="DIRECT", quantity="50", head="MANUFACTURING")
        sid = r["scenario_id"]
        self.change("A-100", kind="SET_HEAD", part="A-100", target_head="PROCESSING", quantity="100")
        rows = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}&head=MANUFACTURING")["rows"]
        self.assertEqual([(x["kind"], x["kind_label"], x["target_head"], x["text"]) for x in rows], [
            ("SET_HEAD", "費目変更", "DIRECT", "費目変更 直接費 → 50"),
            ("SET_HEAD", "費目変更", "PROCESSING", "費目変更 社内加工費 → 100")])
        self.assertEqual(D(rows[0]["difference"]), D(50))
        self.assertEqual(D(rows[1]["difference"]), D(100) * D("1.03") * D("1.11"))
        heads = self.ok("GET", "/api/group/heads", f"version=2026-10&name={NAME}&product=A-100")
        self.assertEqual(heads["editable"], ["PROCESSING", "AMORTIZATION", "DIRECT"])
        self.assertEqual((heads["current"]["DIRECT"], heads["after"]["DIRECT"]), (None, "50"))
        self.assertEqual((heads["current"]["PROCESSING"], heads["after"]["PROCESSING"]), ("0", "100"))
        self.assertEqual(heads["after"]["MATERIAL"], "200")
        # 取り消しは変更の一覧の1行ずつ
        self.ok("POST", "/api/group/changes/undo", body={"scenario_id": sid, "sequence": 1})
        heads = self.ok("GET", "/api/group/heads", f"version=2026-10&name={NAME}&product=A-100")
        self.assertEqual((heads["after"]["DIRECT"], heads["after"]["PROCESSING"]), (None, "100"))
        # 書き換えられない費目・製品でない品番は通さない
        bad = self.call("POST", "/api/group/changes", body={"version_id": "2026-10", "name": NAME, "product": "A-100",
                                                            "kind": "SET_HEAD", "target_head": "INDIRECT", "quantity": "1"})
        self.assert_error(bad, 400, "target_head")
        bad = self.call("POST", "/api/group/changes", body={"version_id": "2026-10", "name": NAME, "product": "A-100",
                                                            "kind": "SET_HEAD", "part": "B-200", "target_head": "DIRECT",
                                                            "quantity": "1"})
        self.assert_error(bad, 400, "A-100")

    def set_heads(self, items: list) -> object:
        return self.call("POST", "/api/group/heads/bulk",
                         body={"version_id": "2026-10", "name": NAME, "head": "MANUFACTURING", "items": items})

    def test_set_heads_in_bulk_per_product_batch(self) -> None:
        self.change("A-100", kind="SET_HEAD", part="A-100", target_head="DIRECT", quantity="10")
        r = self.set_heads([{"product": "A-100", "values": {"PROCESSING": "30", "AMORTIZATION": "", "DIRECT": "50"}},
                            {"product": "V-110", "values": {"AMORTIZATION": "5"}},
                            {"product": "K-200", "values": {"DIRECT": " "}}])  # 打っていない製品は入れない
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual((r.json()["count"], r.json()["products"]), (3, ["A-100", "V-110"]))
        rows = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}&head=MANUFACTURING")["rows"]
        self.assertEqual([(x["product"], x["text"], x["batch"]) for x in rows], [
            ("A-100", "費目変更 直接費 → 10", None),
            ("A-100", "費目変更 社内加工費 → 30・直接費 → 50", 2),
            ("V-110", "費目変更 償却費 → 5", None)])
        self.assertEqual([(h["id"], h["value"]) for h in rows[1]["heads"]], [("PROCESSING", "30"), ("DIRECT", "50")])
        # 1回の操作の差は、まとめて入れた費目の差の合計
        self.assertEqual(D(rows[1]["difference"]), D(30) * D("1.03") * D("1.11") + 40)
        table = self.ok("GET", "/api/group/heads/all", f"version=2026-10&name={NAME}")
        self.assertEqual([h["id"] for h in table["heads"]], ["PROCESSING", "AMORTIZATION", "DIRECT"])
        after = {x["product"]: x["after"] for x in table["rows"]}
        self.assertEqual(after["A-100"], {"PROCESSING": "30", "AMORTIZATION": None, "DIRECT": "50"})
        self.assertEqual(after["V-110"]["AMORTIZATION"], "5")
        self.assertNotIn("K-200", after)
        # 取り消しは製品ごとの1回の操作で、まとめて入れた費目が一緒に抜ける
        self.ok("POST", "/api/group/changes/undo", body={"scenario_id": rows[1]["scenario_id"], "sequence": 2})
        a = self.ok("GET", "/api/group/heads", f"version=2026-10&name={NAME}&product=A-100")["after"]
        self.assertEqual((a["PROCESSING"], a["DIRECT"]), ("0", "10"))

    def test_set_heads_rejects_all_on_one_bad_value(self) -> None:
        before = self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")["rows"]
        for items, message in (
                ([{"product": "A-100", "values": {"DIRECT": "50"}}, {"product": "V-110", "values": {"DIRECT": "-1"}}], "0 以上"),
                ([{"product": "A-100", "values": {"DIRECT": "50"}}, {"product": "V-110", "values": {"DIRECT": "abc"}}], "値"),
                ([{"product": "A-100", "values": {"INDIRECT": "1"}}], "target_head"),
                ([{"product": "A-100", "values": {"DIRECT": "1"}}, {"product": "A-100", "values": {"DIRECT": "2"}}], "2回"),
                ([{"product": "A-100", "values": {"DIRECT": ""}}], "変える値が無い")):
            with self.subTest(items=items):
                self.assert_error(self.set_heads(items), 400, message)
        self.assertEqual(self.ok("GET", "/api/group/changes", f"version=2026-10&name={NAME}")["rows"], before)
        self.assertEqual(self.ok("GET", "/api/group/trials", "version=2026-10"), [])

    def test_lookup_calculated_next_to_imported(self) -> None:
        item = self.ok("GET", "/api/lookup", "version=2026-10&q=A-100")["item"]
        calc = {c["head"]: (c["value"], c["incomplete"]) for c in item["calculated"]}
        self.assertEqual(list(calc), [h.name for h in Head])
        self.assertEqual(calc["MATERIAL"], ("200", False))
        self.assertEqual(calc["MANUFACTURING"], ("228.66", True))  # 償却費・直接費が未登録で 0 として通した
        # 取込値は6費目のまま
        self.assertEqual(len(item["costs"]), 6)
        candidates = {c["code"]: c for c in self.ok("GET", "/api/lookup", "version=2026-10&q=部品")["candidates"]}
        self.assertEqual(candidates["B-200"]["manufacturing"], "1829.28")  # 1600 × 1.03 × 1.11

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl が無い")
    def test_excel_heads(self) -> None:
        self.change("A-100", kind="REPLACE", part="B-200", replacement="X-610", quantity="2")
        response = self.call("GET", "/api/group/compare/table/xlsx",
                             f"version=2026-10&names={quote(json.dumps([NAME]))}&ignore_amortization=1")
        self.assertEqual(response.status, 200, response.body)
        ws = load_openpyxl().load_workbook(io.BytesIO(response.body))["比較"]
        self.assertEqual([c.value for c in ws[1]][4:], ["材料費小計", "社内加工費", "償却費", "調整費", "材料費合計",
                                                         "間接費", "直接費", "製造原価"])


class OldDb(unittest.TestCase):
    def test_target_head_column_added_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite"
            with Store(path) as store:
                store.save_version(small_version())
                s = Scenario("s", "P", "P#", store.load_version("t"))
                s.apply(Change(ChangeKind.ADD, "D", 1))
                sid = store.save_scenario(s)
            _drop_target_head(path)
            with Store(path) as store:
                cols = {r[1] for r in store._conn.execute("PRAGMA table_info(change)")}
                self.assertIn("target_head", cols)
                self.assertEqual(store._conn.execute("SELECT target_head FROM change").fetchall(), [(None,)])
                api = Api(store)
                r = api.handle("POST", f"/api/scenarios/{sid}/changes", "",
                               json.dumps({"kind": "SET_HEAD", "target_head": "DIRECT", "quantity": "8"}).encode())
                self.assertEqual(r.status, 200, r.body.decode("utf-8"))
            with Store(path) as store:
                back = store.load_scenario(sid)
                self.assertEqual([(c.kind, c.target_head, c.quantity) for c in back.changes],
                                 [(ChangeKind.ADD, None, D(1)), (ChangeKind.SET_HEAD, "DIRECT", D(8))])
                self.assertEqual(back.own_values().direct, D(8))


def _drop_target_head(path: Path) -> None:
    """change の表を target_head を足す前の形に作り直す。"""
    conn = sqlite3.connect(str(path))
    with conn:
        conn.executescript(
            """
            CREATE TABLE change_old (
                scenario_id INTEGER NOT NULL REFERENCES scenario(scenario_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL, recorded_at TEXT NOT NULL, kind TEXT NOT NULL, part TEXT NOT NULL,
                replacement TEXT, quantity TEXT NOT NULL, cost_before TEXT, cost_after TEXT,
                version_id TEXT NOT NULL REFERENCES version(version_id), head TEXT NOT NULL, volume TEXT,
                allocation TEXT, parent TEXT, balloon TEXT, batch INTEGER, PRIMARY KEY (scenario_id, sequence));
            INSERT INTO change_old SELECT scenario_id, sequence, recorded_at, kind, part, replacement, quantity,
                cost_before, cost_after, version_id, head, volume, allocation, parent, balloon, batch FROM change;
            DROP TABLE change;
            ALTER TABLE change_old RENAME TO change;
            """
        )
    conn.close()


if __name__ == "__main__":
    unittest.main()
