"""取り込みから③までの通しで見つかった不具合の再発止め。samples/ を取り込んだ DB で、V-110 だけの検討案を通しと同じ手順で変える。"""

import json
import unittest
from decimal import Decimal
from urllib.parse import quote

from bomcost.app import static_response
from bomcost.importer import guess_columns
from test_group import NAME, SampleDbCase
from test_screens import SAMPLES, b64

V = "2026-10"


def js(name: str) -> str:
    return static_response(f"/{name}").body.decode("utf-8")


class WalkCase(SampleDbCase):
    """通しの②の手順: 費目変更（社内加工費）→ B-200 を F-600 へ品番ごとに置換 → 費目変更（直接費）→ F-600 ×2 を追加
    → C-300 を品番ごとに削除（V-110 の使用数 3 − 1 ＝ 2）→ V-110 直下の #2 F-600 を B-200 ×1 に置換。"""

    def walk(self) -> None:
        heads = lambda **v: self.ok("POST", "/api/group/heads/bulk", body={
            "version_id": V, "name": NAME, "head": "MANUFACTURING", "items": [{"product": "V-110", "values": v}]})
        heads(PROCESSING="100")
        self.ok("POST", "/api/group/changes/by-code", body={"version_id": V, "name": NAME, "head": "MANUFACTURING",
                                                            "part": "B-200", "replacement": "F-600", "q": "V-110"})
        heads(DIRECT="280")
        self.change("V-110", head="MANUFACTURING", kind="ADD", part="F-600", quantity="2")
        self.change("V-110", head="MANUFACTURING", kind="REMOVE", part="C-300", quantity="2")
        self.change("V-110", head="MANUFACTURING", kind="REPLACE", part="F-600", parent="V-110", balloon="2",
                    replacement="B-200", quantity="1")

    def analysis(self, cost: str, parts: str = "") -> dict:
        return self.ok("GET", "/api/group/compare/analysis", f"version={V}&names={quote(json.dumps([NAME]))}"
                       f"&products=V-110&ignore_amortization=1&cost={cost}" + (f"&parts={parts}" if parts else ""))


class AnalysisAfterRows(WalkCase):
    """1・5: ③分析の変更後は、新しく入った部品の行も出て行の和＝計。品番ごとに消した部品は変更後 0（行が無い）で、計は変えない。"""

    def setUp(self) -> None:
        super().setUp()
        self.walk()

    @staticmethod
    def row_sum(series: dict) -> Decimal:
        return sum((Decimal(r["amount"]) for r in series["rows"] if r["amount"] is not None), Decimal(0))

    def test_new_parts_are_rows_and_rows_add_up(self) -> None:
        for cost in ("material", "manufacturing"):
            top = self.analysis(cost)
            drill = self.analysis(cost, "A-100")
            for a in (top, drill):
                for s in a["series"]:
                    self.assertEqual(self.row_sum(s), Decimal(s["total"]), (cost, a["parts"], s["name"]))
            # 置換で入った B-200 は最上位の変更後に、A-100 の中で B-200 の代わりに入った F-600 ×2 は一段下りた所に
            current, after = top["series"]
            self.assertNotIn("B-200", {r["key"] for r in current["rows"]})
            self.assertIn("B-200", {r["key"] for r in after["rows"]})
            rows = {r["key"]: r for r in drill["series"][1]["rows"]}
            self.assertEqual(rows["F-600"]["quantity"], "2")

    def test_removed_part_is_zero_after_and_totals_stay(self) -> None:
        table = self.ok("GET", "/api/group/compare/table", f"version={V}&names={quote(json.dumps([NAME]))}"
                        "&ignore_amortization=1")
        after = table["products"][0]["trials"][0]["after"]
        for cost, head in (("material", "MATERIAL"), ("manufacturing", "MANUFACTURING")):
            top = self.analysis(cost)
            drill = self.analysis(cost, "A-100")
            # 消した C-300 は最上位（直下の −1）でも A-100 の中（3 個）でも変更後に残らない
            self.assertNotIn("C-300", {r["key"] for r in top["series"][1]["rows"]})
            self.assertNotIn("C-300", {r["key"] for r in drill["series"][1]["rows"]})
            self.assertTrue(all(Decimal(r["quantity"]) >= 0 for r in top["series"][1]["rows"] if r["kind"] == "child"))
            # 計は③の表の変更後のまま
            self.assertEqual(Decimal(top["series"][1]["total"]), Decimal(after[head]))
            c, = top["contributions"]
            self.assertEqual([r["after"] for r in c["rows"] if r["key"] == "C-300"], ["0"])

    def test_partial_remove_is_shared_by_place(self) -> None:
        # 使用数 2（A-100 の中 3・直下 −1）のうち 1 だけ消すと、場所ごとの割合で半分ずつ減る（計は 1 個分）
        self.ok("DELETE", "/api/group/trials", f"version={V}&name={quote(NAME)}")
        self.change("V-110", kind="REMOVE", part="C-300", quantity="1")
        current, after = self.analysis("material")["series"]
        self.assertEqual(Decimal(after["total"]) - Decimal(current["total"]), Decimal(-500))
        rows = {r["key"]: r for r in after["rows"]}
        self.assertEqual((Decimal(rows["C-300"]["quantity"]), Decimal(rows["C-300"]["amount"])),
                         (Decimal("-0.5"), Decimal(-250)))

    def test_screen_adds_after_only_rows_and_skips_negative_bars(self) -> None:
        compare = js("compare.js")
        size = compare[compare.index("function analysisSize"):compare.index("function analysisDiff")]
        self.assertIn("!keys.has(r.key)", size)
        share = compare[compare.index("function breakdownShare"):compare.index("const analysisHeads")]
        self.assertIn("Number(row.share) > 0", share)


class AmortizationPremise(SampleDbCase):
    """2・3: 償却費の登録がある製品は、型費配賦（登録値を使う）と③（無視すると 0）で製造原価が違う。前提の行で分かる。
    ③の未登録の咎めは、選んだ検討案の試算がある製品だけ。"""

    def setUp(self) -> None:
        super().setUp()
        self.ok("PUT", "/api/manual/items", body={"code": "V-110", "name": "新しい製品",
                                                   "costs": {"AMORTIZATION": "50", "DIRECT": "300"}})
        self.ok("PUT", "/api/manual/volumes", body={"product": "V-110", "annual": "10000"})  # 型費配賦は台数のある製品を並べる
        self.change("V-110", kind="ADD", part="F-600", quantity="1")

    def compare(self, query: str = "") -> dict:
        return self.ok("GET", "/api/group/compare/table", f"version={V}&names={quote(json.dumps([NAME]))}" + query)

    def test_same_premise_same_value_and_premise_shown(self) -> None:
        view = self.ok("GET", "/api/parts/view", f"part=F-600&version={V}&head=MANUFACTURING")
        mold = {r["product"]: r for r in view["rows"]}["V-110"]["current"]
        # 未登録の咎めは V-110 だけなので、無視しなくても③は出る。償却費の登録値を使えば同じ値
        current = self.compare()["products"][0]["current"]["MANUFACTURING"]
        self.assertEqual(Decimal(mold), Decimal(current))
        ignored = self.compare("&ignore_amortization=1")["products"][0]["current"]["MANUFACTURING"]
        self.assertEqual(Decimal(mold) - Decimal(ignored), 50 * Decimal("1.03") * Decimal("1.11"))
        self.assertEqual(view["premises"]["amortization_basis"], "registered")
        self.assertIn("償却費は登録値", js("screens.js"))

    def test_missing_amortization_names_only_chosen_products(self) -> None:
        self.ok("PUT", "/api/manual/items", body={"code": "V-110", "name": "新しい製品", "costs": {"DIRECT": "300"}})
        for path in ("/api/group/compare", "/api/group/compare/table"):
            r = self.call("GET", path, f"version={V}&names={quote(json.dumps([NAME]))}")
            self.assert_error(r, 400, "V-110")
            message = r.json()["error"]
            self.assertNotIn("K-200", message)
            self.assertNotIn("A-100", message)
            self.assertIn("償却費を無視", message)


class PlaceChangedRows(SampleDbCase):
    """4: 場所を員数変更した製品×部品は、一覧で変更の種類が分かり、まとめての置換のメッセージも員数変更になる。"""

    def test_quantity_changed_place_is_marked_and_message_says_so(self) -> None:
        self.change("A-100", kind="SET_QUANTITY", part="C-300", parent="A-100", balloon="2", quantity="5")
        rows = self.parts(f"&part=C-300&name={NAME}&trial=1")["rows"]
        self.assertEqual([(r["product"], r["place_changes"]) for r in rows],
                         [("A-100", [{"kind": "SET_QUANTITY", "label": "員数変更"}])])
        r = self.call("POST", "/api/group/changes/bulk", body={"version_id": V, "name": NAME, "replacement": "F-600",
                                                               "items": [{"product": "A-100", "part": "C-300"}]})
        self.assert_error(r, 400, "もう員数変更した")
        self.assertNotIn("置換した", r.json()["error"])
        trial = js("trial.js")
        self.assertIn("disabled: placeChanged(r)", trial)


class ScreenWording(unittest.TestCase):
    """6・7・9・10・11: 画面の文言と既定の版・簡易照会・主ボタン。"""

    def test_change_log_puts_head_values_in_their_own_cell(self) -> None:
        trial = js("trial.js")
        log = trial[trial.index("function changeLog"):trial.index("async function drawTrial")]
        self.assertIn('c.kind === "SET_HEAD" ? el("td", { colspan: 3 }, headValues(c)', log)
        self.assertIn('c.kind === "SET_HEAD" ? "" : fmt(c.quantity)', log)

    def test_imported_version_becomes_default_unless_picked(self) -> None:
        screens = js("screens.js")
        run = screens[screens.index("async function runImport"):screens.index("function warningList")]
        self.assertIn("followImported(result.version_id)", run)
        follow = screens[screens.index("function followImported"):]
        self.assertIn("if (!select.dataset.picked) versionOptions(select, { value: versionId })", follow)
        for id_ in ("group-version", "change-old-version", "look-version"):
            self.assertIn(f'"{id_}"', screens[screens.index("const FOLLOW_VERSION_SELECTS"):])
        self.assertIn("screenState.importedVersion", screens[screens.index("function manualDefaultDev"):])

    def test_lookup_opens_only_on_code_click(self) -> None:
        trial = js("trial.js")
        code_input = trial[trial.index("const codeInput"):trial.index("const qtyInput")]
        self.assertNotIn("openTrialLookup", code_input)
        self.assertEqual(trial.count("openTrialLookup("), 2)  # 定義と、品番のボタン（trialCode）だけ

    def test_messages_for_people(self) -> None:
        trial, group, screens, compare = js("trial.js"), js("group.js"), js("screens.js"), js("compare.js")
        for old, text in (("変える値を打つ", trial), ("置換する所を選ぶ", trial), ("検討案が空", group), ("値を打つ", compare),
                          ('confirm("消しますか？")', screens), ("${k}←", screens), ("土台・", screens)):
            self.assertNotIn(old, text)
        self.assertIn("fieldLabels[k]", screens)
        manual = screens[screens.index("async function renderManual"):screens.index("async function masterBookBody")]
        pick = manual[manual.index("const pick"):]
        self.assertLess(pick.index('showMessage("")'), pick.index("renderManual"))

    def test_one_primary_button_in_master_book_bar(self) -> None:
        screens = js("screens.js")
        bar = screens[screens.index("function masterBookBar"):screens.index("function masterDiffText")]
        self.assertEqual(bar.count('class: "primary"'), 1)
        self.assertLess(bar.index('class: "primary"'), bar.index("① Excel で開く"))
        self.assertLess(bar.index("① Excel で開く"), bar.index("② "))


class ColumnGuess(SampleDbCase):
    """8: 見出しの自動対応は空白・全角半角を見ず、よく使う別名も当てる。表の種類も見出しで選ぶ。"""

    def preview(self, name: str) -> dict:
        return self.ok("POST", "/api/import/preview",
                       body={"filename": name, "content": b64((SAMPLES / name).read_bytes())})

    def test_samples_map_and_table(self) -> None:
        # SampleDbCase で保存した対応づけは見ず、仮の当て方だけを見る
        expected = {
            "部品マスタ.csv": ("item", {"code": "品番", "name": "品名", "kind": "区分", "lifecycle": "状態",
                                       "has_children": "子部品有無"}),
            "構成マスタ.csv": ("bom_line", {"parent": "親品番", "level": "レベル", "child": "子品番", "quantity": "員数",
                                         "balloon": "風船番号"}),
            "原価.csv": ("cost", {"mbom_code": "MBOM品番"}),
            "年間台数.csv": ("volume", {"product": "製品品番", "annual": "年間生産台数"}),
        }
        for name, (table, columns) in expected.items():
            p = self.preview(name)
            self.assertEqual(p["guessed"], table, name)
            guess = {t["id"]: t["guess"] for t in p["tables"]}[table]
            for field, header in columns.items():
                self.assertEqual(guess.get(field), header, (name, field))

    def test_width_and_spaces_ignored(self) -> None:
        columns, score = guess_columns("cost", ["ＭＢＯＭ　品番", "材料費"])
        self.assertEqual((columns["mbom_code"], columns["material"], score), ("ＭＢＯＭ　品番", "材料費", 4))
        self.assertEqual(guess_columns("item", ["製品品番", "年間生産台数"])[1], 1)  # 品番を含むだけの弱い当たり


if __name__ == "__main__":
    unittest.main()
