"""設計検討の階層: 変えられるのは製品の直下だけ・組立品の中は流用した仮品番で替える・置換の製品ごと。データは架空。"""

import io
import json
import sqlite3
import unittest
from decimal import Decimal

import example_settings  # noqa: F401
from bomcost.api import Api
from bomcost.importer import load_openpyxl
from bomcost.model import BomLine, CostHead, CostRow, Item, Version
from bomcost.store import Store
from bomcost.studyapi import StudyApi, change_effects
from bomcost.structure import structure
from bomcost.study import DEEP, NEGATIVE, Slot, StudyStore, after_places, calculate, check_changes, pair_slots

ALL = ("item", "bom_line", "cost", "volume")
D = Decimal


def bom(**material: str | None) -> Version:
    """P1・P2 は組立品 U（K×2・V×1）と、直下の K×1・S×1。V は組立品（B×3）。P3 は U だけ。材料費 K 10・S 30・B 5・N 7。"""
    v = Version("t")
    for code in ("P1", "P2", "P3", "U", "V", "K", "S", "B", "N"):
        v.add_item(Item(code, f"{code} の名称"))
    v.add_bom_lines([
        BomLine("P1", "1", "U", 1), BomLine("P1", "2", "K", 1), BomLine("P1", "3", "S", 1),
        BomLine("P2", "1", "U", 2), BomLine("P2", "2", "K", 4), BomLine("P2", "3", "S", 1),
        BomLine("P3", "1", "U", 1),
        BomLine("U", "11", "K", 2), BomLine("U", "12", "V", 1),
        BomLine("V", "21", "B", 3),
    ])
    for code, value in {"K": "10", "S": "30", "B": "5", "N": "7", **material}.items():
        v.add_cost(CostRow(code, {CostHead.MATERIAL: value}))
    return v


def lose_temps(store: StudyStore, number: str, keep: tuple[str, ...] = ()) -> None:
    """使っている仮品番が消えた状態を作る（前の版で消した物など）。⑨からは使っている仮品番を消せないので、表から直に消す。"""
    sid = store.study_id(number)
    with store._conn:
        for table in ("v2_temp_item", "v2_temp_slot"):
            store._conn.execute(f"DELETE FROM {table} WHERE study_id = ? AND code NOT IN ({','.join('?' * len(keep))})", (sid, *keep))
        store._touch(sid)


class LevelCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.store = StudyStore(self.conn)
        self.store.replace_bom(bom(), ALL)
        self.number = self.store.create_study()
        self.store.save_targets(self.number, ["P1", "P2", "P3"])

    def ws(self, version: int | None = None):
        return self.store.workspace(self.number, version)

    def changes(self) -> dict[tuple, tuple[str, str]]:
        """（製品, 風船番号, 現行の品番）→（変えた後の品番, 数量）。"""
        return {(c.product, c.slot.balloon, c.slot.now): (c.slot.part, str(c.slot.quantity)) for c in self.ws().plan_changes("A")}

    def material(self, version: int | None = None) -> dict[str, tuple]:
        rows = calculate(self.ws(version), CostHead.MATERIAL)["rows"]
        return {r["product"]: (r["now"], r["plans"]["A"], r["incomplete"]["A"]) for r in rows}

    def deep_change(self) -> None:
        """前の版のモックが書いた、組立品の中の変更。"""
        sid = self.store.study_id(self.number)
        with self.conn:
            self.conn.execute("INSERT INTO v2_slot VALUES (?, 'A', 'P1', 'U', '11', 'K', 'N', '2', '2', 1)", (sid,))


class FirstLevel(LevelCase):
    def test_lists_and_replace_see_only_the_first_level(self) -> None:
        self.assertEqual({p.part for p in after_places(self.ws(), "A")}, {"K", "S", "U"})
        self.store.apply_replace(self.number, "A", "K", "N")
        # U の中の K は替わらない
        self.assertEqual(self.changes(), {("P1", "2", "K"): ("N", "1"), ("P2", "2", "K"): ("N", "4")})
        with self.assertRaisesRegex(ValueError, "直下"):
            self.store.apply_replace(self.number, "A", "B", "N")

    def test_structure_has_direct_places_and_the_insides_once(self) -> None:
        found = structure(self.ws(), "A", ["P1", "P2"])
        self.assertEqual([(s.balloon, s.part, s.quantity) for s in found.direct["P2"]], [("1", "U", D(2)), ("2", "K", D(4)), ("3", "S", D(1))])
        self.assertEqual({c: [(s.now, s.part, s.quantity) for s in lines] for c, lines in found.inside.items()},
                         {"U": [("K", "K", D(2)), ("V", "V", D(1))], "V": [("B", "B", D(3))]})

    def test_old_deep_changes_are_marked_left_out_and_kept(self) -> None:
        self.deep_change()
        self.assertEqual([c.problems for c in check_changes(self.ws())], [(DEEP,)])
        now, after, incomplete = self.material()["P1"]
        self.assertEqual((now, after, incomplete), (now, now, True))
        # 直下の画面から変えても消えない
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "2", "part": "K", "quantity": "3"}])
        self.assertEqual(self.changes(), {("P1", "11", "K"): ("N", "2"), ("P1", "2", "K"): ("K", "3")})
        self.assertEqual(self.store.drop_deep_changes(self.number, "A"), 1)
        self.assertEqual(self.changes(), {("P1", "2", "K"): ("K", "3")})

    def test_version_taken_before_this_keeps_its_value(self) -> None:
        self.deep_change()
        version = self.store.take_release(self.number)
        row = self.conn.execute("SELECT snapshot FROM v2_release").fetchone()
        old = json.loads(row[0])
        for key in ("level1", "slots", "strict"):
            del old[key]
        old["changes"] = [["A", "P1", "U", "11", "K", "-2", "replace", None, 1], ["A", "P1", "U", "11", "N", "2", "replace", None, 2]]
        old["temps"] = [["Z", "仮", "1"]]
        with self.conn:
            self.conn.execute("UPDATE v2_release SET snapshot = ?", (json.dumps(old),))
        now, after, incomplete = self.material(version)["P1"]
        self.assertEqual((after - now, incomplete), (D(2 * 7 - 2 * 10), False))
        self.assertEqual(self.ws(version).temps[0][:4], ("Z", "仮", D(1), None))


class PlaceReplace(LevelCase):
    def rows(self) -> dict[tuple, tuple]:
        return {(p.product, p.balloon, p.part): (p.quantity, p.was) for p in after_places(self.ws(), "A")}

    def test_only_the_chosen_products_change_and_come_back(self) -> None:
        self.store.apply_places(self.number, "A", [{"product": "P2", "balloon": "2", "old": "K", "new": "N"},
                                                   {"product": "P1", "balloon": "3", "old": "S", "new": "N"}])
        self.assertEqual(self.rows()[("P2", "2", "N")], (D(4), ["K"]))
        self.assertIn(("P1", "2", "K"), self.rows())
        self.store.back_places(self.number, "A", [{"product": "P2", "balloon": "2", "now": "K", "part": "N"}])
        self.assertEqual(self.changes(), {("P1", "3", "S"): ("N", "1")})

    def test_swap_at_once_and_one_bad_row_changes_nothing(self) -> None:
        self.store.apply_places(self.number, "A", [{"product": "P1", "balloon": "2", "old": "K", "new": "S"},
                                                   {"product": "P1", "balloon": "3", "old": "S", "new": "K"}])
        self.assertEqual(self.rows()[("P1", "2", "S")], (D(1), ["K"]))
        before = self.changes()
        for bad in ({"product": "P2", "balloon": "9", "old": "K", "new": "N"}, {"product": "ZZ", "balloon": "2", "old": "K", "new": "N"},
                    {"product": "P2", "balloon": "3", "old": "S", "new": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.apply_places(self.number, "A", [{"product": "P2", "balloon": "2", "old": "K", "new": "N"}, bad])
        with self.assertRaisesRegex(ValueError, "2行"):
            self.store.apply_places(self.number, "A", [{"product": "P2", "balloon": "2", "old": "K", "new": n} for n in ("N", "S")])
        self.assertEqual(self.changes(), before)

    def test_places_without_balloons_come_back_one_by_one(self) -> None:
        v = bom_of([BomLine("P1", None, "K", 2), BomLine("P1", None, "S", 3), BomLine("N", "1", "B", 1), BomLine("P2", None, "V", 1)])
        self.store.replace_bom(v, ["item", "bom_line"])
        self.store.apply_places(self.number, "A", [{"product": "P1", "old": "K", "new": "N"}, {"product": "P1", "old": "S", "new": "B"}])
        self.store.back_places(self.number, "A", [{"product": "P1", "balloon": None, "now": "K", "part": "N"}])
        self.assertEqual(self.changes(), {("P1", None, "S"): ("B", "3")})
        for bad in ({"now": "K", "part": "N"}, {"now": None, "part": "B"}):
            with self.assertRaisesRegex(ValueError, "開き直して"):
                self.store.back_places(self.number, "A", [{"product": "P1", "balloon": None, **bad}])


class Derived(LevelCase):
    def temp(self, **more: object) -> None:
        self.store.save_temps(self.number, [{"code": "T1", "name": "組立改", "base": "U", **more}])

    def test_material_is_the_base_plus_the_differences(self) -> None:
        self.temp()
        self.store.save_temp_lines(self.number, "T1", [{"balloon": "11", "part": "N", "quantity": "2"}, {"balloon": "12", "part": "V", "quantity": "1"}])
        # U は 2×10＋3×5＝35。K 2個を N 2個に → 35−20＋14＝29
        self.assertEqual(self.ws().bom.cost_value("T1", CostHead.MATERIAL), D(29))
        self.store.apply_replace(self.number, "A", "U", "T1")
        self.assertEqual({p: a - n for p, (n, a, _) in self.material().items()}, {"P1": D(-6), "P2": D(-12), "P3": D(-6)})
        self.assertFalse(any(x[2] for x in self.material().values()))
        self.temp(material="100")  # 打った値があればそちら（中の変更は残る）
        self.assertEqual(self.ws().bom.cost_value("T1", CostHead.MATERIAL), D(100))
        self.assertEqual(len(self.ws().temps[0].lines), 1)
        self.temp()
        self.assertEqual(self.ws().bom.cost_value("T1", CostHead.MATERIAL), D(29))

    def test_registered_assembly_cost_is_kept_as_the_base(self) -> None:
        self.store.replace_bom(bom(U="50"), ["cost"])
        self.temp()
        self.store.save_temp_lines(self.number, "T1", [{"balloon": "11", "part": "K", "quantity": "3"}, {"balloon": "12", "part": "V", "quantity": "1"}])
        self.assertEqual(self.ws().bom.cost_value("T1", CostHead.MATERIAL), D(60))

    def test_unknown_part_or_lost_base_is_unregistered(self) -> None:
        self.store.save_temps(self.number, [{"code": "T1", "base": "U"}, {"code": "XX"}])
        rows = [{"balloon": "11", "part": "XX", "quantity": "2"}, {"balloon": "12", "part": "V", "quantity": "1"}]
        self.store.save_temp_lines(self.number, "T1", rows)
        lose_temps(self.store, self.number, ("T1",))  # 使った後に XX が消えた
        self.store.apply_replace(self.number, "A", "U", "T1")
        self.assertIsNone(self.ws().bom.cost_value("T1", CostHead.MATERIAL))
        self.assertTrue(self.material()["P1"][2])
        empty = Version("t")
        self.store.replace_bom(empty, ALL)
        self.assertIsNone(self.ws().bom.cost_value("T1", CostHead.MATERIAL))
        with self.assertRaisesRegex(ValueError, "流用元 U が部品表にありません"):
            self.store.save_temp_lines(self.number, "T1", rows)
        self.store.save_temps(self.number, [{"code": "T1", "name": "名前だけ直す", "base": "U"}])  # 流用元が無い間も、ほかは直せる
        self.store.replace_bom(bom(XX="1"), ALL)
        self.assertEqual(len(self.ws().temps[0].lines), 1)

    def test_three_levels_by_temps_in_temps_and_no_loop(self) -> None:
        self.store.save_temps(self.number, [{"code": "T1", "base": "U"}, {"code": "T2", "base": "V"}])
        self.store.save_temp_lines(self.number, "T2", [{"balloon": "21", "part": "N", "quantity": "3"}])
        self.store.save_temp_lines(self.number, "T1", [{"balloon": "11", "part": "K", "quantity": "2"}, {"balloon": "12", "part": "T2", "quantity": "1"}])
        self.assertEqual(self.ws().bom.cost_value("T2", CostHead.MATERIAL), D(21))
        self.assertEqual(self.ws().bom.cost_value("T1", CostHead.MATERIAL), D(41))
        for code, part in (("T1", "T1"), ("T2", "T1")):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "自身は入れられません"):
                self.store.save_temp_lines(self.number, code, [{"balloon": "21", "part": part, "quantity": "1"}])

    def test_renaming_keeps_the_inside_and_a_new_base_clears_it(self) -> None:
        self.temp()
        self.store.save_temp_lines(self.number, "T1", [{"balloon": "12", "part": "V", "quantity": "1"}])
        self.store.save_temps(self.number, [{"code": "T9", "key": "T1", "base": "U"}])
        self.assertEqual((self.ws().temps[0].code, self.ws().temps[0].lines), ("T9", (Slot("11", "K", "K", D(0), D(2)),)))
        self.store.save_temps(self.number, [{"code": "T9", "key": "T9", "base": "V"}])
        self.assertEqual(self.ws().temps[0].lines, ())
        for base in ("K", "ZZ", "T9"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                self.store.save_temps(self.number, [{"code": "T9", "key": "T9", "base": base}])

    def test_old_temp_table_gets_the_base_column(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE v2_temp_item (study_id INTEGER, position INTEGER, code TEXT, name TEXT, material TEXT, "
                     "PRIMARY KEY (study_id, position))")
        store = StudyStore(conn)
        number = store.create_study()
        store.save_temps(number, [{"code": "T1", "name": "仮", "material": "3"}])
        self.assertEqual(store.workspace(number).temps[0].material, D(3))

    def test_a_quantity_change_in_the_base_keeps_the_typed_quantity(self) -> None:
        self.temp()
        self.store.save_temp_lines(self.number, "T1", [{"balloon": "11", "part": "K", "quantity": "1"}, {"balloon": "12", "part": "V", "quantity": "1"}])
        v = bom()
        self.store.replace_bom(bom_of([BomLine("U", "11", "K", 9), BomLine("U", "12", "V", 1), *v.children("V", by_balloon=True)]), ["bom_line"])
        self.assertEqual(self.ws().bom.cost_value("T1", CostHead.MATERIAL), D(25))  # K 1個＋V 15

    def test_version_keeps_the_inside(self) -> None:
        self.temp()
        self.store.save_temp_lines(self.number, "T1", [{"balloon": "11", "part": "N", "quantity": "2"}, {"balloon": "12", "part": "V", "quantity": "1"}])
        self.store.apply_replace(self.number, "A", "U", "T1")
        before = self.material()
        version = self.store.take_release(self.number)
        lose_temps(self.store, self.number)
        self.store.replace_bom(bom(N="999"), ALL)
        self.assertEqual(self.material(version), before)
        self.assertEqual(self.ws(version).temps[0].base, "U")


class LevelApi(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.api = Api(self.store)
        StudyStore(self.store.connection).replace_bom(bom(), ALL)
        number = self.ok("POST", "/api/v2/studies", {"products": ["P1", "P2"]}, "")["header"]["number"]
        self.q = f"number={number}&plan=A"
        self.number = number

    def put_inside(self, code: str, rows: list[dict[str, str]]) -> None:
        """仮品番の中の行を入れる（画面の窓は外したので、保存の処理を直に呼ぶ）。"""
        StudyStore(self.store.connection).save_temp_lines(self.number, code, rows)

    def ok(self, method: str, path: str, body: object = None, query: str | None = None) -> dict:
        r = self.api.handle(method, path, self.q if query is None else query, b"" if body is None else json.dumps(body).encode())
        self.assertLess(r.status, 300, r.body.decode("utf-8"))
        return r.json()

    def test_the_whole_structure_goes_to_its_own_excel(self) -> None:
        # 対象品番の全部を、変えた後の構成で全階層まで1行1箇所。変えた箇所に印
        self.ok("POST", "/api/v2/replace/apply", {"rows": [{"product": "P1", "balloon": "3", "old": "S", "new": "N"}]})
        r = self.api.handle("GET", "/api/v2/export/structure", self.q, b"")
        self.assertEqual(r.status, 200)
        self.assertIn("filename*=UTF-8''%E6%A7%8B%E6%88%90_", r.headers["Content-Disposition"])
        book = load_openpyxl().load_workbook(io.BytesIO(r.body))
        self.assertEqual(book.sheetnames, ["構成", "品目"])
        # 品番は1列・親の下に子の順。数量（変更後）は、その箇所のレベルの列（見出しは 1・2・3）にだけ入る
        self.assertEqual([c.value for c in book["構成"][1]][11:14], ["1", "2", "3"])
        rows = list(book["構成"].iter_rows(min_row=2, values_only=True))
        self.assertEqual([(x[2], x[3], x[4], x[6], x[9], x[10], x[11:14], x[14]) for x in rows if x[0] == "P1"], [
            (1, "P1", "1", "U", "U", 1, (1, None, None), None),
            (2, "U", "11", "K", "K", 2, (None, 2, None), None),
            (2, "U", "12", "V", "V", 1, (None, 1, None), None),
            (3, "V", "21", "B", "B", 3, (None, None, 3), None),
            (1, "P1", "2", "K", "K", 1, (1, None, None), None),
            (1, "P1", "3", "N", "S", 1, (1, None, None), "変更")])
        self.assertEqual({x[0] for x in rows}, {"P1", "P2"})
        self.assertEqual(sum(1 for x in rows if x[0] == "P2"), 6)
        # 品目: 構成に出てくる品番を1行に1つ（出てきた順）。材料費・製造原価は画面の品番の右の列と同じ値
        self.assertEqual([c.value for c in book["品目"][1]], ["品番", "名称", "仕様", "材料費", "製造原価"])
        listed = list(book["品目"].iter_rows(min_row=2, values_only=True))
        self.assertTrue(all(x[3] is not None and x[4] is not None for x in listed))
        self.assertEqual([x[0] for x in listed], list(dict.fromkeys(x[6] for x in rows)))
        shown = self.ok("POST", "/api/v2/lookup", {"codes": [x[0] for x in listed]})["items"]
        for code, name, _, material, manufacturing in listed:
            self.assertEqual((name, material, manufacturing),
                             (shown[code]["name"], *(None if shown[code][k] is None else float(shown[code][k]) for k in ("material", "manufacturing"))))

    def test_screens_show_the_inside_of_a_derived_temp(self) -> None:
        got = self.ok("PUT", "/api/v2/temp-items", {"rows": [{"code": "T1", "name": "組立改", "material": "", "base": "U"}]})["rows"]
        self.assertEqual((got[0]["derived"], got[0]["changed"], got[0]["base"]), ("35", 0, "U"))
        self.put_inside("T1", [{"balloon": "11", "part": "N", "quantity": "2"}, {"balloon": "12", "part": "V", "quantity": "1"}])
        self.assertEqual(self.ok("GET", "/api/v2/temp-items")["rows"][0]["changed"], 1)
        self.ok("POST", "/api/v2/replace/apply", {"rows": [{"product": "P2", "balloon": "1", "old": "U", "new": "T1"}]})
        data = self.ok("GET", "/api/v2/changes")
        self.assertEqual([(s["kind"], s["old"], s["new"], s["products"]) for s in data["summary"]], [("置き換え", "U", "T1", ["P2"])])
        self.assertEqual([(s["kind"], s["old"], s["new"], s["quantity"]) for s in data["temps"]["T1"]["changes"]], [("置き換え", "K", "N", "2")])
        self.assertEqual(data["deep"], 0)
        # ⑤⑧ は、仮品番の中を流用元と変えた後の組で出す
        got = self.ok("GET", "/api/v2/structure")
        self.assertEqual([(r["now"], r["part"]) for r in got["inside"]["T1"]], [("K", "N"), ("V", "V")])
        self.assertEqual((got["bases"], got["items"]["T1"]["temp"]), ({"T1": "U"}, True))
        # 数の合わない行（同じ風船に2つ足す）も落とさない
        self.put_inside("T1", [{"balloon": "11", "part": "N", "quantity": "1"}, {"balloon": "11", "part": "S", "quantity": "1"},
                               {"balloon": "12", "part": "V", "quantity": "1"}])
        back = self.ok("GET", "/api/v2/structure")["inside"]["T1"]
        self.assertEqual(sorted((r["part"], r["quantity"]) for r in back), [("N", "1"), ("S", "1"), ("V", "1")])

    def test_the_new_code_of_an_assembly_is_asked_before_it_is_made(self) -> None:
        # ⑧ の組立品の中の窓: 入れる前に、付く品番の候補を書かずに返し、画面で選んだ品番で入れる
        edit = [{"parent": "U", "products": ["P1", "P2"], "balloon": "11", "old": "K", "new": "N"}]
        before = self.ok("GET", "/api/v2/temp-items")["rows"]
        needs = self.ok("POST", "/api/v2/structure/needs", {"inside": edit})["needs"]
        self.assertEqual([(n["code"], n["suggested"], n["fresh"]) for n in needs], [("U", "U-K1", True)])
        self.assertEqual(self.ok("GET", "/api/v2/temp-items")["rows"], before)  # 聞くだけでは何も書かない
        made = self.ok("POST", "/api/v2/structure", {"inside": edit, "numbers": {"U": "U-NEW"}})["made"]
        self.assertEqual([(m["code"], m["new"]) for m in made], [("U", "U-NEW")])
        self.assertEqual([r["code"] for r in self.ok("GET", "/api/v2/temp-items")["rows"]], ["U-NEW"])

    def test_a_check_writes_nothing_and_the_answer_is_keyed_by_the_suggested_code(self) -> None:
        # 一括置換の全階層は、直下を書く前に中の分を check で試す。断られる品番なら、ここで止まって何も書かない
        rows = [{"path": ["U"], "old": "K", "new": "N", "balloon": "11"}]
        refused = self.api.handle("POST", "/api/v2/inside/replace", self.q, json.dumps({"rows": rows, "numbers": {"U-K1": "S"}, "check": True}).encode())
        self.assertEqual(refused.status, 400)
        checked = self.ok("POST", "/api/v2/inside/replace", {"rows": rows, "numbers": {"U-K1": "U-OK"}, "check": True})
        self.assertEqual([(m["code"], m["new"]) for m in checked["made"]], [("U", "U-OK")])
        self.assertEqual(self.ok("GET", "/api/v2/temp-items")["rows"], [])
        # 窓の答えは自動の品番をキーにする（同じ元品番が中身違いで2つ出ても取り違えない）
        made = self.ok("POST", "/api/v2/inside/replace", {"rows": rows, "numbers": {"U-K1": "U-OK"}})["made"]
        self.assertEqual([(m["code"], m["new"]) for m in made], [("U", "U-OK")])

    def test_the_bulk_replace_over_all_levels_asks_the_new_code_too(self) -> None:
        # 一括置換の全階層: 組立品の中の行（path の形）でも、先に候補を聞いて、選んだ品番で入れる
        rows = [{"path": ["U"], "old": "K", "new": "N", "balloon": "11"}]
        needs = self.ok("POST", "/api/v2/structure/needs", {"rows": rows})["needs"]
        self.assertEqual([(n["code"], n["suggested"], n["fresh"]) for n in needs], [("U", "U-K1", True)])
        made = self.ok("POST", "/api/v2/inside/replace", {"rows": rows, "numbers": {"U": "U-ALL"}})["made"]
        self.assertEqual([(m["code"], m["new"]) for m in made], [("U", "U-ALL")])

    def test_excel_gets_a_sheet_only_when_a_temp_has_changes_inside(self) -> None:
        def book():
            return load_openpyxl().load_workbook(io.BytesIO(self.api.handle("GET", "/api/v2/export/study", self.q).body))

        self.ok("PUT", "/api/v2/temp-items", {"rows": [{"code": "T1", "name": "組立改", "base": "U"}]})
        self.assertNotIn("仮品番の中", book().sheetnames)
        self.put_inside("T1", [{"balloon": "11", "part": "N", "quantity": "2"}, {"balloon": "12", "part": "V", "quantity": "1"}])
        self.assertEqual(book().sheetnames[-2:], ["仮品番の中", "計算条件"])
        rows = list(book()["仮品番の中"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(rows, [("T1", "組立改", "U", "置き換え", "K", "K の名称", "N", "N の名称", "11", None, 2, 2)])
        effects = list(book()["変更ごとの影響"].iter_rows(values_only=True))
        self.assertEqual((effects[0][:6], len(effects)), (("変更", "元品番", "元の名称", "新品番", "新の名称", "製品の数"), 1))
        self.assertIn(("仮品番", "T1", "組立改", 29, "元品番 U から計算"), list(book()["計算条件"].iter_rows(values_only=True)))

    def test_moved_places_go_back_to_the_current_side_in_one_call(self) -> None:
        # ⑧ の［戻す］［全て戻す］の送り方
        before = self.ok("GET", "/api/v2/structure")
        self.ok("POST", "/api/v2/structure", {
            "quantity": [{"product": "P1", "balloon": "3", "now": "S", "part": "S", "quantity": "0"},
                         {"product": "P2", "balloon": "2", "now": "K", "part": "K", "quantity": "9"}],
            "add": [{"product": "P1", "balloon": "9", "part": "N", "quantity": "2"}]})
        self.ok("POST", "/api/v2/structure", {"replace": [{"product": "P2", "balloon": "2", "now": "K", "old": "K", "new": "N"}]})
        direct = self.ok("GET", "/api/v2/structure")["direct"]
        moved = [{"product": p, **{k: r[k] for k in ("balloon", "now", "part")}} for p, rows in direct.items() for r in rows
                 if r["now"] != r["part"] or r["before"] != r["quantity"]]
        self.assertEqual(len(moved), 3)
        self.ok("POST", "/api/v2/structure", {"back": moved})
        self.assertEqual(self.ok("GET", "/api/v2/changes")["rows"], [])
        # 組立品の中: 置き換え＋数量・追加・削除
        at = {"parent": "U", "products": ["P1", "P2"]}
        self.ok("POST", "/api/v2/structure", {"inside": [
            {**at, "balloon": "11", "now": "K", "old": "K", "new": "N", "quantity": "5"}, {**at, "balloon": "12", "now": "V", "old": "V", "quantity": "0"},
            {**at, "balloon": "13", "old": None, "new": "S", "quantity": "1"}]})
        self.assertEqual({r["part"] for r in self.ok("GET", "/api/v2/structure")["direct"]["P1"] if r["balloon"] == "1"}, {"U-K1"})
        at["parent"] = "U-K1"
        self.ok("POST", "/api/v2/structure", {"inside": [
            {**at, "balloon": "11", "now": "K", "old": "N", "new": "K", "quantity": "2"}, {**at, "balloon": "12", "old": None, "new": "V", "quantity": "1"},
            {**at, "balloon": "13", "now": None, "old": "S", "quantity": "0"}]})
        after = self.ok("GET", "/api/v2/structure")
        self.assertEqual((after["direct"], after["inside"]), (before["direct"], before["inside"]))

    def test_two_places_with_the_same_part_inside_are_pointed_at_by_the_current_part(self) -> None:
        at = {"parent": "U", "products": ["P1", "P2"], "balloon": "11"}
        self.ok("POST", "/api/v2/structure", {"inside": [{**at, "old": None, "new": "S", "quantity": "3"}]})
        at["parent"] = "U-K1"
        self.ok("POST", "/api/v2/structure", {"inside": [{**at, "now": "K", "old": "K", "new": "N"}, {**at, "now": None, "old": "S", "new": "N"}]})
        rows = lambda: [(r["now"], r["part"], r["quantity"]) for r in self.ok("GET", "/api/v2/structure")["inside"]["U-K1"]]
        self.assertEqual(rows(), [("K", "N", "2"), ("V", "V", "1"), (None, "N", "3")])
        self.ok("POST", "/api/v2/structure", {"inside": [{**at, "now": "K", "old": "N", "quantity": "8"}]})
        self.assertEqual(rows(), [("K", "N", "8"), ("V", "V", "1"), (None, "N", "3")])
        # 足した箇所を、もう戻っている品番に替えると、その箇所に足す
        self.ok("POST", "/api/v2/structure", {"inside": [{**at, "now": "K", "old": "N", "new": "K", "quantity": "2"}]})
        self.ok("POST", "/api/v2/structure", {"inside": [{**at, "now": None, "old": "N", "new": "K"}]})
        self.assertEqual(rows(), [("K", "K", "5"), ("V", "V", "1")])

    def test_after_parts_tells_places_and_deeper_parts(self) -> None:
        data = self.ok("GET", "/api/v2/after-parts")
        self.assertEqual([(r["path"], r["part"], r["lines"]) for r in data["inside"]], [
            (["U"], "K", [{"balloon": "11", "quantity": "2", "was": []}]), (["U"], "V", [{"balloon": "12", "quantity": "1", "was": []}]),
            (["U", "V"], "B", [{"balloon": "21", "quantity": "3", "was": []}])])
        self.assertEqual(data["tops"], {"U": [{"product": "P1", "balloon": "1"}, {"product": "P2", "balloon": "1"}]})
        self.assertIn({"product": "P2", "balloon": "2", "part": "K", "quantity": "4", "was": [], "before": "4", "now": "K"}, data["places"])
        got = self.ok("POST", "/api/v2/replace/apply", {"rows": [{"product": "P2", "balloon": "2", "old": "K", "new": "N"}]})
        self.assertIn({"product": "P2", "balloon": "2", "part": "N", "quantity": "4", "was": ["K"], "before": "4", "now": "K"}, got["places"])

    def test_structure_edits_direct_places_and_insides_in_one_call(self) -> None:
        got = self.ok("POST", "/api/v2/structure", {
            "quantity": [{"product": "P1", "balloon": "3", "part": "S", "quantity": "0"}],
            "replace": [{"product": "P2", "balloon": "2", "old": "K", "new": "N"}],
            "add": [{"product": "P1", "balloon": "9", "part": "N", "quantity": "2"}],
            "inside": [{"parent": "U", "products": ["P1", "P2"], "balloon": "11", "old": "K", "quantity": "3"}]})
        self.assertEqual(got, {"made": [{"code": "U", "new": "U-K1", "name": "U の名称", "typed": False}], "typed": []})
        data = self.ok("GET", "/api/v2/structure")
        self.assertEqual([(r["balloon"], r["now"], r["part"], r["quantity"]) for r in data["direct"]["P1"]],
                         [("1", "U", "U-K1", "1"), ("2", "K", "K", "1"), ("3", "S", "S", "0"), ("9", None, "N", "2")])
        self.assertEqual([(r["now"], r["part"], r["quantity"]) for r in data["direct"]["P2"]], [("U", "U-K1", "2"), ("K", "N", "4"), ("S", "S", "1")])
        self.assertEqual([(r["part"], r["before"], r["quantity"]) for r in data["inside"]["U-K1"]], [("K", "2", "3"), ("V", "1", "1")])

    def test_list_hides_old_deep_changes_and_the_list_can_drop_them(self) -> None:
        with self.store.connection as c:
            c.execute("INSERT INTO v2_slot VALUES (1, 'A', 'P1', 'U', '11', 'K', 'K', '3', '2', 1)")
        data = self.ok("GET", "/api/v2/structure")
        self.assertFalse(any(r["before"] != r["quantity"] or r.get("problem") for rows in data["direct"].values() for r in rows))
        self.assertEqual(self.ok("GET", "/api/v2/changes")["deep"], 1)
        self.assertEqual(self.ok("POST", "/api/v2/changes/drop-deep", {})["rows"], [])


class Effects(LevelCase):
    """変更ごとの影響: 足すと全体の差になる・分からない分は足さずに印を付ける。率は見本（10%・20%）。"""

    def setUp(self) -> None:
        super().setUp()
        self.store.save_conditions(self.number, {"P1": "100", "P2": "10"})
        self.store.save_targets(self.number, ["P1", "P2"])

    def effects(self, version: int | None = None) -> list[dict]:
        ws = self.ws(version)
        return change_effects(ws, "A", StudyApiRows.rows(ws))

    def test_sum_is_the_total_difference_of_every_product(self) -> None:
        self.store.apply_replace(self.number, "A", "K", "N")
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "3", "part": "S", "quantity": "2"}])
        self.store.add_part(self.number, "A", "B", "2", ["P1", "P2"])
        self.store.save_owns(self.number, "A", [{"product": "P1", "head": "processing", "value": "4"},
                                                {"product": "P2", "head": "direct", "value": "50"}])
        got = self.effects()
        result = calculate(self.ws())
        self.assertEqual(sum(e["impact"] for e in got), result["impact"]["A"])
        for row in result["rows"]:
            parts = [p["unit"] for e in got for p in e["products"] if p["product"] == row["product"]]
            self.assertEqual(sum(parts), row["plans"]["A"] - row["now"])
        self.assertEqual([abs(e["impact"]) for e in got], sorted((abs(e["impact"]) for e in got), reverse=True))
        swap = next(e for e in got if e["kind"] == "置き換え")
        # K 10 → N 7 を P1 は1個・P2 は4個。×1.1×1.2
        self.assertEqual((swap["unit_min"], swap["unit_max"], swap["impact"]), (D("-15.84"), D("-3.96"), D("-554.40")))
        self.assertEqual({e["head"] for e in got if e["kind"] == "製品の費目"}, {"加工費", "直接費"})
        self.assertEqual(result["base"]["A"], sum(r["now"] * r["annual"] for r in result["rows"]))
        version = self.store.take_release(self.number)
        self.store.save_owns(self.number, "A", [])
        self.assertEqual(self.effects(version), got)

    def test_own_heads_of_a_product_without_a_value_are_not_added(self) -> None:
        # 部品も材料費も無い製品は、結果の年間の影響に入らない。費目の書き換えの影響も足さない
        self.store.replace_bom(bom_of([BomLine("P1", "1", "N", 1), BomLine("Z", "1", "P2", 1)]), ["item", "bom_line"])
        self.store.save_owns(self.number, "A", [{"product": p, "head": "direct", "value": "5"} for p in ("P1", "P2")])
        (own,) = self.effects()
        self.assertEqual([(p["product"], p["unit"]) for p in own["products"]], [("P1", D(5)), ("P2", None)])
        self.assertEqual((own["impact"], own["incomplete"]), (calculate(self.ws())["impact"]["A"], True))

    def test_unknown_price_annual_and_left_out_changes_are_marked(self) -> None:
        self.store.save_conditions(self.number, {"P1": "100"})  # P2 は年間台数が無い
        self.store.apply_replace(self.number, "A", "K", "N")
        self.store.save_temps(self.number, [{"code": "ZZ"}])
        self.store.apply_places(self.number, "A", [{"product": "P1", "balloon": "3", "old": "S", "new": "ZZ"}])
        lose_temps(self.store, self.number)
        self.deep_change()
        got = {(e["kind"], e["old"], e["new"], tuple(e["problems"])): e for e in self.effects()}
        swap = got[("置き換え", "K", "N", ())]
        self.assertEqual((swap["impact"], swap["incomplete"]), (D("-396.00"), True))
        unknown = got[("置き換え", "S", "ZZ", ("未登録", "単価待ち"))]
        # 単価が分かるまで現行のまま（元の S 30 も引かない）
        self.assertEqual((unknown["unit_max"], unknown["impact"], unknown["incomplete"]), (None, None, True))
        self.assertEqual(sum(e["impact"] for e in self.effects() if e["impact"] is not None), calculate(self.ws())["impact"]["A"])
        self.assertIsNone(got[("置き換え", "K", "N", (DEEP,))]["impact"])
        self.assertEqual(len(self.effects()), 3)


class RefuseUnregistered(LevelCase):
    """部品表にも仮品番にも無い品番は、打った時に断る。もう構成にある物は、そのまま保存できる。"""

    MESSAGE = "ZZ は部品表にも仮品番にもありません"

    def test_every_input_refuses_and_changes_nothing(self) -> None:
        self.store.save_temps(self.number, [{"code": "T1", "base": "U"}])
        good = {"product": "P2", "balloon": "2", "old": "K", "new": "N"}
        rows = [{"product": "P3", "balloon": "8", "part": "N", "quantity": "1"}, {"product": "P3", "balloon": "9", "part": "ZZ", "quantity": "1"}]
        for call in (
            lambda: self.store.apply_replace(self.number, "A", "K", "ZZ"),
            lambda: self.store.apply_places(self.number, "A", [good, {**good, "product": "P1", "new": "ZZ"}]),
            lambda: self.store.add_part(self.number, "A", "ZZ", "1", ["P1"]),
            lambda: self.store.add_places(self.number, "A", rows),
            lambda: self.store.save_temp_lines(self.number, "T1", [{"balloon": "11", "part": "ZZ", "quantity": "2"}]),
        ):
            with self.assertRaisesRegex(ValueError, self.MESSAGE):
                call()
        self.assertEqual((self.changes(), self.ws().temps[0].lines), ({}, ()))
        # 仮品番で取れば使える
        self.store.save_temps(self.number, [{"code": "T1", "base": "U"}, {"code": "ZZ"}])
        self.store.apply_replace(self.number, "A", "K", "ZZ")
        self.store.save_temp_lines(self.number, "T1", [{"balloon": "11", "part": "ZZ", "quantity": "2"}])

    def test_parts_lost_later_do_not_block_other_edits(self) -> None:
        self.store.save_temps(self.number, [{"code": "ZZ"}])
        self.store.apply_places(self.number, "A", [{"product": "P1", "balloon": "3", "old": "S", "new": "ZZ"}])
        lose_temps(self.store, self.number)
        self.store.save_quantities(self.number, "A", [{"product": "P1", "balloon": "2", "part": "K", "quantity": "5"},
                                                      {"product": "P1", "balloon": "3", "part": "ZZ", "quantity": "2"}])
        self.assertEqual(self.changes(), {("P1", "2", "K"): ("K", "5"), ("P1", "3", "S"): ("ZZ", "2")})


class HoldUnpriced(LevelCase):
    """単価の無い品番への置き換えは、単価が分かるまで現行のまま。"""

    def setUp(self) -> None:
        super().setUp()
        self.store.save_temps(self.number, [{"code": "ZZ"}])  # 材料費は空

    def test_empty_temp_is_held_until_priced(self) -> None:
        self.store.save_temps(self.number, [{"code": "T1", "name": "新しい部品"}])
        self.store.apply_replace(self.number, "A", "S", "T1")
        got = self.material()
        for product in ("P1", "P2"):
            self.assertEqual(got[product], (got[product][0], got[product][0], True))
        self.store.save_temps(self.number, [{"code": "T1", "name": "新しい部品", "material": "40"}])
        for product in ("P1", "P2"):
            now, after, incomplete = self.material()[product]
            self.assertEqual((after - now, incomplete), (D(10), False))

    def test_only_the_waiting_pair_is_held(self) -> None:
        # 同じ製品の、ほかの変更と、足した部品・消した部品は効く
        self.store.apply_places(self.number, "A", [{"product": "P1", "balloon": "3", "old": "S", "new": "ZZ"},
                                                   {"product": "P1", "balloon": "2", "old": "K", "new": "N"}])
        self.store.add_part(self.number, "A", "B", "2", ["P1"])
        now, after, incomplete = self.material()["P1"]
        self.assertEqual((after - now, incomplete), (D(-3 + 10), True))
        self.store.save_quantities(self.number, "A", [{"product": "P2", "balloon": "3", "part": "S", "quantity": "0"}])
        self.store.add_part(self.number, "A", "ZZ", "1", ["P2"])  # 単価の無い部品を足しただけなら、消した S は引く
        now, after, incomplete = self.material()["P2"]
        self.assertEqual((after - now, incomplete), (D(-30), True))

    def test_several_changes_in_one_slot_pair_by_quantity(self) -> None:
        # 風船番号の無い部品表（同じ箇所とみなす）
        self.store.replace_bom(bom_of([BomLine("P1", None, "K", 2), BomLine("P1", None, "S", 1)]), ["bom_line"])
        self.store.apply_places(self.number, "A", [{"product": "P1", "balloon": None, "old": "S", "new": "ZZ"},
                                                   {"product": "P1", "balloon": None, "old": "K", "new": "N"}])
        now, after, incomplete = self.material()["P1"]
        self.assertEqual((after - now, incomplete), (D(2 * 7 - 2 * 10), True))

    def test_version_taken_before_this_keeps_its_value(self) -> None:
        self.store.apply_places(self.number, "A", [{"product": "P1", "balloon": "3", "old": "S", "new": "ZZ"}])
        version = self.store.take_release(self.number)
        now, after, _ = self.material(version)["P1"]
        self.assertEqual(after, now)
        snapshot = json.loads(self.conn.execute("SELECT snapshot FROM v2_release").fetchone()[0])
        del snapshot["hold"]
        with self.conn:
            self.conn.execute("UPDATE v2_release SET snapshot = ?", (json.dumps(snapshot),))
        now, after, _ = self.material(version)["P1"]
        self.assertEqual(after - now, D(-30))


def bom_of(lines: list[BomLine]) -> Version:
    v = Version("t")
    for code in sorted({c for l in lines for c in (l.parent, l.child)}):
        v.add_item(Item(code, code))
    v.add_bom_lines(lines)
    return v


class OldDatabase(unittest.TestCase):
    """変更を増減で持っていた前の版の DB と、固めた版。"""

    OLD = """CREATE TABLE v2_change (study_id INTEGER, plan TEXT, product TEXT, parent TEXT, balloon TEXT, part TEXT, delta TEXT, source TEXT,
                                     origin TEXT, seq INTEGER);
             CREATE TABLE v2_temp_line (study_id INTEGER, code TEXT, balloon TEXT, part TEXT, delta TEXT);"""

    def open(self, with_bom: bool = True) -> tuple[StudyStore, str]:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        store = StudyStore(conn)
        if with_bom:
            store.replace_bom(bom(), ALL)
        number = store.create_study()
        store.save_targets(number, ["P1", "P2", "P3"])
        store.save_temps(number, [{"code": "T1", "material": "5"}])
        conn.executescript(self.OLD)
        with conn:
            for i, (product, parent, balloon, part, delta, source) in enumerate((
                    ("P1", "P1", "2", "K", "-1", "replace"), ("P1", "P1", "2", "N", "3", "quantity"), ("P1", "P1", "3", "S", "-1", "quantity"),
                    ("P1", "P1", "3", "B", "2", "add"), ("P2", "P2", "2", "K", "2", "rewrite"), ("P1", "U", "11", "K", "-2", "replace"),
                    ("P1", "U", "11", "N", "2", "replace"))):
                conn.execute("INSERT INTO v2_change VALUES (1, 'A', ?, ?, ?, ?, ?, ?, NULL, ?)", (product, parent, balloon, part, delta, source, i))
            conn.execute("INSERT INTO v2_temp_line VALUES (1, 'T1', '11', 'K', '-2')")
            conn.execute("INSERT INTO v2_temp_line VALUES (1, 'T1', '11', 'N', '2')")
            conn.execute("UPDATE v2_temp_item SET base = 'U', material = NULL")
        return StudyStore(conn), number

    EXPECTED = {("P1", "P1", "2", "K"): ("N", "3"), ("P1", "P1", "3", "S"): ("S", "0"), ("P1", "P1", "3", None): ("B", "2"),
                ("P2", "P2", "2", "K"): ("K", "6"), ("P1", "U", "11", "K"): ("N", "2")}

    def found(self, store: StudyStore, number: str, version: int | None = None) -> dict:
        return {(c.product, c.parent, c.slot.balloon, c.slot.now): (c.slot.part, str(c.slot.quantity)) for c in store.workspace(number, version).changes}

    def test_changes_are_taken_over_once(self) -> None:
        store, number = self.open()
        self.assertEqual(self.found(store, number), self.EXPECTED)
        ws = store.workspace(number)
        self.assertEqual(ws.temps[0].lines, (Slot("11", "K", "N", D(2), D(2)),))
        self.assertEqual([c.problems for c in check_changes(ws) if c.problems], [(DEEP,)])
        again = StudyStore(store._conn)
        self.assertEqual(self.found(again, number), self.EXPECTED)
        # 前の版で開き直して空の表ができていても、次に開ける
        store._conn.executescript(self.OLD)
        self.assertEqual(self.found(StudyStore(store._conn), number), self.EXPECTED)

    def test_an_emptied_bom_waits_for_the_next_import(self) -> None:
        # 設定を替えて部品表が空の間は、現行の数量が分からないので移さない
        store, number = self.open(with_bom=False)
        self.assertEqual(self.found(store, number), {})
        store.replace_bom(bom(), ALL)
        self.assertEqual(self.found(store, number), self.EXPECTED)

    def test_old_versions_are_read_with_the_same_rule_and_their_own_bom(self) -> None:
        store, number = self.open()
        store.take_release(number)
        data = json.loads(store._conn.execute("SELECT snapshot FROM v2_release").fetchone()[0])
        del data["slots"], data["strict"]
        data["changes"] = [["A", "P1", "P1", "2", "K", "-1", "replace", None, 1], ["A", "P1", "P1", "2", "N", "1", "replace", None, 2]]
        data["temps"] = [["T1", "", None, "U", [["11", "K", "-2"], ["11", "N", "2"]]]]
        with store._conn as c:
            c.execute("UPDATE v2_release SET snapshot = ?", (json.dumps(data),))
        store.replace_bom(Version("t"), ALL)
        self.assertEqual(self.found(store, number, 1), {("P1", "P1", "2", "K"): ("N", "1")})
        self.assertEqual(store.workspace(number, 1).temps[0].lines, (Slot("11", "K", "N", D(2), D(2)),))

    def test_pairs_of_old_changes(self) -> None:
        now = {("1", "A"): D(2), ("1", "B"): D(3), ("2", "C"): D(4)}
        for deltas, expected in (
            ({("1", "A"): D(-2), ("1", "B"): D(-3), ("1", "X"): D(3), ("1", "Y"): D(2)}, [("A", "Y", D(2)), ("B", "X", D(3))]),  # 数の合う相手
            ({("2", "C"): D(-1), ("2", "X"): D(1)}, [("C", "C", D(3)), (None, "X", D(1))]),  # 0 にならなければ置き換えではない
            ({("2", "C"): D(1)}, [("C", "C", D(5))]),
            ({("9", "L"): D(-2), ("9", "X"): D(2)}, [("L", "X", D(2))]),  # 現行の品番が無くなっていた
            ({("2", "C"): D(-6)}, [("C", "C", D(-2))]),
        ):
            with self.subTest(deltas=deltas):
                self.assertEqual([(s.now, s.part, s.quantity) for s in pair_slots(now, deltas)], expected)

    def test_a_negative_quantity_is_left_out(self) -> None:
        store, number = self.open()
        with store._conn as c:
            c.execute("UPDATE v2_slot SET quantity = '-1' WHERE product = 'P2'")
        ws = store.workspace(number)
        self.assertEqual([c.problems for c in check_changes(ws) if c.change.product == "P2"], [(NEGATIVE,)])
        row = calculate(ws, CostHead.MATERIAL)["rows"][1]
        self.assertEqual((row["plans"]["A"], row["incomplete"]["A"]), (row["now"], True))


class StudyApiRows:
    """API の変更の行を、ソケット無しで作る。"""

    @staticmethod
    def rows(ws) -> list[dict]:
        api = StudyApi.__new__(StudyApi)
        api._ws = lambda params: ws
        return api._change_rows({"plan": "A"})


class RefuseDeletingUsedTemps(LevelCase):
    """使っている仮品番（変更に出てくる・ほかの仮品番の中にある）は消さない。使っていない物と、品番の打ち替えは通す。"""

    def codes(self) -> list[str]:
        return [t.code for t in self.ws().temps]

    def test_a_used_temp_is_not_deleted(self) -> None:
        self.store.save_temps(self.number, [{"code": "T1", "base": "U"}, {"code": "T2"}, {"code": "T3"}])
        self.store.save_temp_lines(self.number, "T1", [{"balloon": "11", "part": "T2", "quantity": "2"}, {"balloon": "12", "part": "V", "quantity": "1"}])
        self.store.apply_places(self.number, "A", [{"product": "P1", "balloon": "3", "old": "S", "new": "T3"}])
        for keep, message in (([{"code": "T1", "base": "U"}, {"code": "T2"}], "仮品番 T3 は構成変更で使っているので消せません"),
                              ([{"code": "T1", "base": "U"}, {"code": "T3"}], "仮品番 T2 は、仮品番 T1 の中で使っているので消せません")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.store.save_temps(self.number, keep)
        self.assertEqual(self.codes(), ["T1", "T2", "T3"])
        # 使っている仮品番も、品番の打ち替えはできる（使っている所も替わる）
        self.store.save_temps(self.number, [{"code": "T1", "base": "U"}, {"code": "T2"}, {"key": "T3", "code": "T9"}])
        self.assertEqual(self.codes(), ["T1", "T2", "T9"])
        # 使っていない仮品番は消せる
        self.store.save_temps(self.number, [{"code": "T1", "base": "U"}, {"code": "T2"}, {"code": "T9"}, {"code": "T4"}])
        self.store.save_temps(self.number, [{"code": "T1", "base": "U"}, {"code": "T2"}, {"code": "T9"}])
        self.assertEqual(self.codes(), ["T1", "T2", "T9"])


if __name__ == "__main__":
    unittest.main()
