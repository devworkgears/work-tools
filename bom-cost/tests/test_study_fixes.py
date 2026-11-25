"""設計検討（/v2/）の点検で見つかった不具合の再発止め。データは架空。"""

import json
import shutil
import sqlite3
import subprocess
import unittest
from decimal import Decimal
from pathlib import Path

import example_settings  # noqa: F401
from bomcost.api import Api
from bomcost.model import BomLine, CostHead, CostRow, Item, Version, Volume
from bomcost.store import Store
from bomcost.study import (
    OTHER_PART,
    PAIR_OUT,
    REPLACE,
    REWRITE,
    StudyStore,
    calculate,
    check_changes,
    input_amount,
    replace_overridden,
)

ALL = ("item", "bom_line", "cost", "volume")
WEB = Path(__file__).resolve().parent.parent / "bomcost" / "web" / "v2"


def version(lines: list[BomLine], costs: dict[str, str]) -> Version:
    v = Version("t")
    codes = {c for l in lines for c in (l.parent, l.child)} | set(costs) | {"ADD"}
    for code in sorted(codes):
        v.add_item(Item(code, code))
    v.add_bom_lines(lines)
    for code, material in costs.items():
        v.add_cost(CostRow(code, {CostHead.MATERIAL: material}))
    v.add_volume(Volume("X", 1))
    return v


SIMPLE = [BomLine("X", "1", "OLD", 2), BomLine("X", "2", "S", 1)]
COSTS = {"OLD": "100", "NEW": "120", "S": "10", "OTHER": "50", "ADD": "7", "B": "5"}


class FixCase(unittest.TestCase):
    def open(self, lines: list[BomLine], targets: list[str]) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.store = StudyStore(self.conn)
        self.store.replace_bom(version(lines, COSTS), ALL)
        self.number = self.store.create_study()
        self.store.save_targets(self.number, targets)

    def changes(self) -> dict[tuple, tuple[str, str]]:
        return {c.place: (str(c.delta), c.source) for c in self.store.workspace(self.number).plan_changes("A")}

    def material(self) -> dict:
        return calculate(self.store.workspace(self.number), CostHead.MATERIAL)["rows"][0]


class ReplaceAgain(FixCase):
    def test_a_rerun_keeps_the_untouched_side_of_a_hand_edited_pair(self) -> None:
        self.open(SIMPLE, ["X"])
        self.store.save_replaces(self.number, "A", [("OLD", "NEW")])
        self.store.save_rewrite(self.number, "A", "X", [{"balloon": "1", "part": "NEW", "quantity": "3"},
                                                        {"balloon": "2", "part": "S", "quantity": "1"}])
        self.store.save_replaces(self.number, "A", [("OLD", "NEW")])
        self.assertEqual(self.changes(), {("X", "X", "1", "OLD"): ("-2", REPLACE), ("X", "X", "1", "NEW"): ("3", REWRITE)})
        self.assertEqual(self.material()["plans"]["A"], Decimal(210 - 200 + 360))

    def test_f_same_old_part_twice_is_refused_with_rows(self) -> None:
        self.open(SIMPLE, ["X"])
        with self.assertRaisesRegex(ValueError, "重複しています（1行目・2行目）"):
            self.store.save_replaces(self.number, "A", [("OLD", "NEW"), ("OLD", "OTHER")])
        self.assertEqual(self.changes(), {})

    def test_g_mark_only_for_hand_edits_not_for_products_added_later(self) -> None:
        self.open(SIMPLE + [BomLine("Y", "1", "OLD", 1)], ["X"])
        self.store.save_replaces(self.number, "A", [("OLD", "NEW")])
        self.store.save_targets(self.number, ["X", "Y"])
        self.assertEqual(replace_overridden(self.store.workspace(self.number), "A"), {("OLD", "NEW", None): False})
        self.store.save_rewrite(self.number, "A", "X", [{"balloon": "1", "part": "NEW", "quantity": "1"},
                                                        {"balloon": "2", "part": "S", "quantity": "1"}])
        self.assertEqual(replace_overridden(self.store.workspace(self.number), "A"), {("OLD", "NEW", None): True})


class PairAndIncomplete(FixCase):
    def test_b_and_c_a_lost_minus_side_drops_its_plus_side_and_marks_incomplete(self) -> None:
        self.open(SIMPLE, ["X"])
        self.store.save_replaces(self.number, "A", [("OLD", "NEW")])
        self.store.replace_bom(version([BomLine("X", "1", "OTHER", 2), BomLine("X", "2", "S", 1)], COSTS), ALL)
        got = {c.change.part: (c.problems, c.counted) for c in check_changes(self.store.workspace(self.number))}
        self.assertEqual(got, {"OLD": ((OTHER_PART,), False), "NEW": ((PAIR_OUT,), False)})
        row = self.material()
        self.assertEqual(row["plans"]["A"], row["now"])
        self.assertTrue(row["incomplete"]["A"])

    def test_c_no_mark_when_nothing_is_dropped(self) -> None:
        self.open(SIMPLE, ["X"])
        self.store.save_replaces(self.number, "A", [("OLD", "NEW")])
        self.assertFalse(self.material()["incomplete"]["A"])


class Inputs(FixCase):
    def test_full_width_codes_and_numbers_are_folded(self) -> None:
        self.open(SIMPLE, ["X"])
        self.store.save_quantities(self.number, "A", [{"product": "Ｘ", "balloon": "１", "part": "ＯＬＤ", "quantity": "１"}])
        self.store.save_replaces(self.number, "B", [("Ｓ", "ＮＥＷ")])
        self.assertEqual(self.changes(), {("X", "X", "1", "OLD"): ("-1", "quantity")})
        self.assertIn(("X", "X", "2", "NEW"), {c.place for c in self.store.workspace(self.number).plan_changes("B")})
        self.store.save_targets(self.number, ["Ｘ"])
        self.assertEqual(self.store.workspace(self.number).targets, ["X"])

    def test_n_amounts_with_too_many_digits_are_refused(self) -> None:
        for text in ("1e400000000", "1e10", "0.0000001"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "桁が多すぎます"):
                input_amount(text)
        self.assertEqual(input_amount("１２．５"), Decimal("12.5"))
        self.assertEqual(input_amount("−2"), Decimal(-2))


class ApiShapes(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.api = Api(self.store)
        StudyStore(self.store.connection).replace_bom(version(SIMPLE, COSTS), ALL)
        self.number = self.api.handle("POST", "/api/v2/studies", "", b"{}").json()["header"]["number"]
        self.q = f"number={self.number}"
        self.api.handle("PUT", "/api/v2/targets", self.q, json.dumps({"products": ["X"]}).encode())

    def tearDown(self) -> None:
        self.store.close()

    def status(self, method: str, path: str, body: object) -> tuple[int, str]:
        r = self.api.handle(method, path, self.q, json.dumps(body).encode())
        return r.status, r.json().get("error", "") if r.status >= 300 else ""

    def test_n_bad_shapes_and_huge_numbers_are_400_and_not_saved(self) -> None:
        for method, path, body in (
            ("POST", "/api/v2/quantity", {"rows": [{"product": "X", "balloon": "2", "part": "S", "quantity": "1e400000000"}]}),
            ("POST", "/api/v2/quantity", {"rows": ["x"]}),
            ("POST", "/api/v2/add-part", {"part": "ADD", "quantity": "1e400000000", "products": ["X"]}),
            ("POST", "/api/v2/add-part", {"part": "ADD", "quantity": "1", "products": "X"}),
            ("POST", "/api/v2/add-part", {"part": "ADD", "quantity": "1", "products": [{"code": "X"}]}),
            ("POST", "/api/v2/add-part", {"part": "ADD", "products": ["X"]}),
            ("PUT", "/api/v2/replace", {"rows": ["a"]}),
            ("PUT", "/api/v2/replace", {"rows": {"old": "a"}}),
            ("POST", "/api/v2/structure", {"add": ["x"]}),
            ("POST", "/api/v2/structure", {"add": [{"product": "X", "balloon": "2", "part": "S", "quantity": "1e400000000"}]}),
            ("POST", "/api/v2/structure", {"inside": [{"parent": "A1", "products": "X", "old": "S", "new": "NEW"}]}),
            ("POST", "/api/v2/structure", {"inside": [{"parent": "無い", "products": ["X"], "old": "S", "new": "NEW"}]}),
            ("PUT", "/api/v2/conditions", {"annual": {"X": "1e400000000"}}),
            ("POST", "/api/v2/lookup", {"codes": "ab"}),
        ):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.status(method, path, body)[0], 400)
        self.assertEqual(self.api.handle("GET", "/api/v2/changes", self.q).json()["rows"], [])
        self.assertEqual(self.api.handle("GET", "/api/v2/export/study", self.q).status, 200)

    def test_f_duplicate_rows_are_named_by_screen_row(self) -> None:
        status, error = self.status("PUT", "/api/v2/replace", {"rows": [
            {"old": "OLD", "new": "NEW"}, {"old": "", "new": ""}, {"old": "ＯＬＤ", "new": "OTHER"}]})
        self.assertEqual(status, 400)
        self.assertIn("重複しています（1行目・3行目）", error)

    def test_lost_place_shows_no_before_count(self) -> None:
        self.api.handle("PUT", "/api/v2/replace", self.q, json.dumps({"rows": [{"old": "OLD", "new": "NEW"}]}).encode())
        StudyStore(self.store.connection).replace_bom(
            version([BomLine("X", "1", "OTHER", 2), BomLine("X", "2", "S", 1)], COSTS), ALL)
        rows = {r["part"]: r for r in self.api.handle("GET", "/api/v2/changes", self.q).json()["rows"]}
        self.assertEqual((rows["OLD"]["before"], rows["OLD"]["delta"]), (None, "-2"))
        self.assertEqual(rows["NEW"]["before"], "0")  # 置換の相手が外れただけの行は、箇所が無いのとは別
        self.assertEqual(self.api.handle("GET", "/api/v2/export/study", self.q).status, 200)

    def test_messages_are_polite(self) -> None:
        cases = (
            ("PUT", "/api/v2/replace", {"rows": [{"old": "OLD", "new": ""}]}, "元品番と新品番を入力してください（1行目）"),
            ("POST", "/api/v2/quantity", {"rows": [{"product": "X", "balloon": "2", "part": "S", "quantity": "abc"}]}, "数量は数で入力してください（abc）"),
        )
        for method, path, body, message in cases:
            with self.subTest(path=path):
                self.assertEqual(self.status(method, path, body), (400, message))

    def test_plus_saves_an_empty_plan_at_once(self) -> None:
        r = self.api.handle("POST", "/api/v2/plans", self.q, b"{}")
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual((r.json()["plan"], r.json()["plans"]), ("B", ["A", "B"]))
        # ほかの画面が読む検討の案の一覧にもすぐ出る
        self.assertEqual(self.api.handle("GET", "/api/v2/study", self.q).json()["plans"], ["A", "B"])
        self.assertEqual(self.api.handle("POST", "/api/v2/plans", self.q, b"{}").json()["plan"], "C")
        self.assertEqual(self.api.handle("GET", "/api/v2/replace", self.q + "&plan=B").json()["rows"], [])

    def test_version_must_be_ascii_digits(self) -> None:
        r = self.api.handle("GET", "/api/v2/study", self.q + "&version=²")
        self.assertEqual(r.status, 400)
        self.assertIn("版は数字で入力してください", r.json()["error"])


HARNESS = r"""
const out = {};
out.clean = [clean("ＰＲ１００１－００１"), clean(" −２ "), clean("abc")];
// ④ と同じ形: 品番・名称（書き換えない）・品番
function model3() {
  const rows = [];
  return {
    rows,
    nRows: () => rows.length + 1,
    nCols: () => 4,
    cell(r, c) { return { text: "", editable: c === 1 || c === 3 }; },
    set(r, c, text) {
      if (r >= rows.length) { if (!text) return; rows.push({}); }
      rows[r][c] = text;
    },
    grow() { rows.push({}); return true; },
  };
}
function paste(model, text, sel) {
  const fake = { model, sel, changed() {} };
  Grid.prototype.onPaste.call(fake, { preventDefault() {}, clipboardData: { getData: () => text } });
  return model.rows;
}
out.skip = paste(model3(), "OLD\tNEW\nO2\tN2\n", { r: 0, c: 1 });
out.blank = paste(model3(), "A\tB\n\nC\tD", { r: 0, c: 1 });
// keydown を経ずに textarea へ入った文字（日本語入力の確定・insertText）で編集を始める
const started = [];
const fake = { sink: { value: "ＰＲ１００２－００１" }, editing: null, startEdit(keep, text) { started.push(text); } };
Grid.prototype.takeSink.call(fake);
fake.editing = {};
fake.sink.value = "x";
Grid.prototype.takeSink.call(fake);
out.sink = { started, left: fake.sink.value };
// ④ と同じ形の格子で、Tab・左右は書けない「名称」を飛ばし、名称で打った文字は右の書ける列へ入る
function navGrid(sel) {
  const g = Object.create(Grid.prototype);
  g.model = { nRows: () => 2, nCols: () => 5, cell: (r, c) => ({ editable: c === 1 || c === 3 }) };
  g.sel = sel;
  g.select = (r, c) => { g.sel = { r, c }; };
  g.td = () => null;
  return g;
}
const nav = navGrid({ r: 0, c: 1 });
nav.move(0, 1, true);
const tab = nav.sel;
nav.move(0, 1, true);  // 右に書けるセルが無ければ1つ動く
const edge = nav.sel;
const typed = navGrid({ r: 1, c: 2 });
let editedAt = null;
typed.td = (r, c) => { editedAt = [r, c]; throw new Error("stop"); };
try { typed.startEdit(false, "K"); } catch (_) {}
out.nav = { tab, edge, typed: typed.sel, editedAt };
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node が無い")
class GridPaste(unittest.TestCase):
    def test_i_j_paste_skips_read_only_columns_and_keeps_blank_lines(self) -> None:
        source = (WEB / "grid.js").read_text(encoding="utf-8")
        run = subprocess.run(["node", "-e", source + HARNESS], capture_output=True, text=True, check=True)
        got = json.loads(run.stdout)
        self.assertEqual(got["clean"], ["PR1001-001", "-2", "abc"])
        self.assertEqual(got["skip"], [{"1": "OLD", "3": "NEW"}, {"1": "O2", "3": "N2"}])
        # 空の行は空のまま1行に数え、後ろの行が落ちない
        self.assertEqual(got["blank"], [{"1": "A", "3": "B"}, {"1": ""}, {"1": "C", "3": "D"}])

    def test_moves_skip_read_only_columns_and_typing_there_goes_right(self) -> None:
        source = (WEB / "grid.js").read_text(encoding="utf-8")
        run = subprocess.run(["node", "-e", source + HARNESS], capture_output=True, text=True, check=True)
        got = json.loads(run.stdout)["nav"]
        self.assertEqual(got["tab"], {"r": 0, "c": 3})
        self.assertEqual(got["edge"], {"r": 0, "c": 4})
        self.assertEqual((got["typed"], got["editedAt"]), ({"r": 1, "c": 3}, [1, 3]))

    def test_text_entered_without_keydown_starts_editing(self) -> None:
        source = (WEB / "grid.js").read_text(encoding="utf-8")
        run = subprocess.run(["node", "-e", source + HARNESS], capture_output=True, text=True, check=True)
        got = json.loads(run.stdout)["sink"]
        # 編集中でなければ入った文字のまま編集を始め（半角へは書き換えの時にそろえる）、textarea は空に戻す
        self.assertEqual(got, {"started": ["ＰＲ１００２－００１"], "left": ""})


BUSY_HARNESS = r"""
const span = {};
const box = { id: "busy", hidden: true, querySelector: () => span };
const body = { children: [{ id: "app", inert: false }, box] };
globalThis.document = { body, activeElement: null, getElementById: () => box };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = {};
(async () => {
  await work("すぐ", async () => sleep(20));
  await sleep(400);
  out.short = box.hidden; // すぐ終われば帯は出ない
  let during;
  const long = work("計算しています", async () => { const end = busy.start(""); await sleep(450); during = [box.hidden, span.textContent, body.children[0].inert]; end(); });
  await long;
  out.during = during;
  out.after = [box.hidden, body.children[0].inert, busy.texts.length];
  // 失敗しても、押せる画面に戻る
  out.error = await work("失敗", async () => { throw new Error("x"); }).catch((e) => e.message);
  // 中で続けて動かした時は、外側が終わるまで押せない
  await work("外", async () => { await work("中", async () => {}); out.nested = body.children[0].inert; });
  out.end = [body.children[0].inert, busy.texts.length, busy.timer];
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node が無い")
class Busy(unittest.TestCase):
    """処理中の表示と、時間のかかる操作の二重押しの止め。"""

    def test_bar_shows_only_for_long_work_and_the_screen_is_released(self) -> None:
        source = (WEB / "v2.js").read_text(encoding="utf-8")
        part = source[source.index("const busy = {"):source.index("// 通信はどれも処理中に数える")]
        run = subprocess.run(["node", "-e", part + BUSY_HARNESS], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout), {
            "short": True, "during": [False, "計算しています…", True], "after": [True, False, 0],
            "error": "x", "nested": True, "end": [False, 0, None]})


if __name__ == "__main__":
    unittest.main()
