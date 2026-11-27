"""設計検討の組立品の中の部品の置き換え（⑧の［開く］の窓と［一括置換］の全階層）（上の組立品を流用した仮品番にする）。データは架空。"""

import json
import unittest
from decimal import Decimal

from bomcost.api import Api
from bomcost.inside import Rebuild, expand_parents, input_edits, inside_rows
from bomcost.model import BomLine
from bomcost.store import Store
from bomcost.study import StudyStore, after_places, inside_lines
from test_study_level import ALL, D, LevelCase, bom


class InsideCase(LevelCase):
    """P1 は U×1、P2 は U×2、P3 は U×1。U は K×2・V×1、V は B×3。"""

    def rebuild(self, *rows: dict) -> Rebuild:
        return Rebuild(self.ws(), "A", input_edits(self.ws(), rows))

    def needs(self, *rows: dict) -> list[tuple]:
        return [(n["code"], n["suggested"]) for n in self.rebuild(*rows).needs()]

    def apply(self, *rows: dict, codes: dict[str, str] | None = None) -> None:
        """codes は 組立品 → 新しい品番（無ければ案のまま）。"""
        rebuild = self.rebuild(*rows)
        change = rebuild.change([{"code": n["code"], "name": n["name"], "new": (codes or {}).get(n["code"], n["suggested"])}
                                 for n in rebuild.needs()])
        self.store.save_inside(self.ws(), "A", change.added, change.lines, change.picks)

    def tops(self) -> dict[str, list[str]]:
        return {t: [p.part for p in after_places(self.ws(), "A") if p.product == t and p.balloon == "1" and p.quantity] for t in ("P1", "P2", "P3")}

    def inside(self, code: str) -> dict[tuple, Decimal]:
        return inside_lines(self.ws(), code)


class ReplaceInside(InsideCase):
    def test_one_level_makes_one_new_assembly_in_every_product(self) -> None:
        row = {"path": ["U"], "old": "K", "new": "N"}
        self.assertEqual(self.needs(row), [("U", "U-K1")])
        self.assertEqual(self.changes(), {})  # 確かめるだけでは何も変わらない
        self.apply(row)
        self.assertEqual(self.tops(), {"P1": ["U-K1"], "P2": ["U-K1"], "P3": ["U-K1"]})
        self.assertEqual(self.inside("U-K1"), {("11", "N"): D(2), ("12", "V"): D(1)})
        # 直下の K は替わらない。材料費は K 10×2 → N 7×2 で 1台あたり −6
        self.assertEqual({p: after - now for p, (now, after, _) in self.material().items()}, {"P1": D(-6), "P2": D(-12), "P3": D(-6)})

    def test_two_levels_make_a_new_code_for_each_assembly_above(self) -> None:
        row = {"path": ["U", "V"], "old": "B", "new": "N"}
        self.assertEqual(self.needs(row), [("V", "V-K1"), ("U", "U-K1")])
        self.apply(row, codes={"U": "X1", "V": "X2"})
        self.assertEqual(self.inside("X1"), {("11", "K"): D(2), ("12", "X2"): D(1)})
        self.assertEqual(self.inside("X2"), {("21", "N"): D(3)})
        self.assertEqual({p: after - now for p, (now, after, _) in self.material().items()}, {"P1": D(6), "P2": D(12), "P3": D(6)})

    def test_the_next_part_of_the_same_assembly_asks_nothing(self) -> None:
        self.apply({"path": ["U"], "old": "K", "new": "N"})
        row = {"path": ["U-K1", "V"], "old": "B", "new": "N"}
        # V はまだ部品表の品番なので新しい品番が要る。U-K1 はそのまま中を直す
        self.assertEqual(self.needs(row), [("V", "V-K1")])
        self.apply(row)
        again = {"path": ["U-K1", "V-K1"], "old": "N", "new": "S"}
        self.assertEqual(self.needs(again), [])
        self.apply(again)
        self.assertEqual(self.inside("U-K1"), {("11", "N"): D(2), ("12", "V-K1"): D(1)})
        self.assertEqual(self.inside("V-K1"), {("21", "S"): D(3)})
        self.assertEqual([t.code for t in self.ws().temps], ["U-K1", "V-K1"])

    def test_one_product_only_leaves_the_others(self) -> None:
        self.apply({"path": ["U"], "old": "K", "new": "N", "product": "P2", "top": "1"})
        self.assertEqual(self.tops(), {"P1": ["U"], "P2": ["U-K1"], "P3": ["U"]})

    def test_a_temp_shared_by_other_products_is_not_changed_in_place(self) -> None:
        self.apply({"path": ["U"], "old": "K", "new": "N"})
        row = {"path": ["U-K1"], "old": "N", "new": "S", "product": "P2", "top": "1"}
        self.assertEqual(self.needs(row), [("U-K1", "U-K2")])
        self.apply(row)
        self.assertEqual(self.tops(), {"P1": ["U-K1"], "P2": ["U-K2"], "P3": ["U-K1"]})
        self.assertEqual(self.inside("U-K1")[("11", "N")], D(2))
        self.assertEqual(self.inside("U-K2"), {("11", "S"): D(2), ("12", "V"): D(1)})
        self.assertEqual({t.code: t.base for t in self.ws().temps}, {"U-K1": "U", "U-K2": "U"})
        # P2 だけが使う U-K2 は、P2 の行でそのまま直せる
        self.assertEqual(self.needs({"path": ["U-K2"], "old": "S", "new": "K", "product": "P2", "top": "1"}), [])

    def test_a_temp_used_in_another_plan_is_not_changed_in_place(self) -> None:
        self.apply({"path": ["U"], "old": "K", "new": "N"})
        self.store.add_plan(self.number)
        self.store.apply_replace(self.number, "B", "U", "U-K1")
        self.assertEqual(self.needs({"path": ["U-K1"], "old": "N", "new": "S"})[0], ("U-K1", "U-K2"))

    def test_a_temp_held_by_a_product_out_of_the_targets_is_not_changed_in_place(self) -> None:
        self.apply({"path": ["U"], "old": "K", "new": "N"})
        self.store.save_targets(self.number, ["P1", "P2"])  # P3 は外す（U-K1 への変更は残っている）
        row = {"path": ["U-K1"], "old": "N", "new": "S"}
        self.assertEqual(self.needs(row), [("U-K1", "U-K2")])
        self.apply(row)
        self.store.save_targets(self.number, ["P1", "P2", "P3"])
        self.assertEqual(self.tops(), {"P1": ["U-K2"], "P2": ["U-K2"], "P3": ["U-K1"]})
        self.assertEqual(self.inside("U-K1")[("11", "N")], D(2))

    def test_splitting_a_temp_with_a_typed_price_is_told(self) -> None:
        self.apply({"path": ["U"], "old": "K", "new": "N"})
        with self.conn:
            self.conn.execute("UPDATE v2_temp_item SET material = '99'")
        row = {"path": ["U-K1"], "old": "N", "new": "S", "product": "P2", "top": "1"}
        self.assertEqual([(n["suggested"], n["typed"]) for n in self.rebuild(row).needs()], [("U-K2", True)])

    def test_a_product_cannot_come_into_its_own_assembly(self) -> None:
        with self.assertRaisesRegex(ValueError, "自分の中"):
            self.apply({"path": ["U"], "old": "K", "new": "P1", "product": "P1", "top": "1"})
        self.assertEqual(self.ws().temps, [])

    def test_different_changes_by_product_get_different_codes(self) -> None:
        rows = ({"path": ["U"], "old": "K", "new": "N", "product": "P1", "top": "1"},
                {"path": ["U"], "old": "K", "new": "S", "product": "P2", "top": "1"},
                {"path": ["U"], "old": "K", "new": "N", "product": "P3", "top": "1"})
        self.assertEqual(self.needs(*rows), [("U", "U-K1"), ("U", "U-K2")])
        self.apply_each(rows)
        self.assertEqual(self.tops(), {"P1": ["U-K1"], "P2": ["U-K2"], "P3": ["U-K1"]})

    def apply_each(self, rows: tuple) -> None:
        rebuild = self.rebuild(*rows)
        change = rebuild.change([{"code": n["code"], "new": n["suggested"]} for n in rebuild.needs()])
        self.store.save_inside(self.ws(), "A", change.added, change.lines, change.picks)

    def test_same_content_made_again_uses_the_same_code(self) -> None:
        row = {"path": ["U"], "old": "K", "new": "N", "product": "P1", "top": "1"}
        self.apply(row)
        # P3 で同じ変更をすると、P1 の仮品番が案に出て、そのまま使う
        third = {**row, "product": "P3"}
        self.assertEqual(self.needs(third)[0][1], "U-K1")
        self.apply(third)
        self.assertEqual(self.tops(), {"P1": ["U-K1"], "P2": ["U"], "P3": ["U-K1"]})
        self.assertEqual(len(self.ws().temps), 1)

    def test_one_balloon_inside_only(self) -> None:
        self.store.replace_bom(bom_two_places(), ALL)
        self.apply({"path": ["U"], "old": "K", "new": "N", "balloon": "13"})
        self.assertEqual(self.inside("U-K1"), {("11", "K"): D(2), ("12", "V"): D(1), ("13", "N"): D(5)})

    def test_undo_is_the_same_operation(self) -> None:
        self.apply({"path": ["U"], "old": "K", "new": "N"})
        rows, _ = inside_rows(self.ws(), "A")
        self.assertEqual([(r.path, r.part, r.lines[0].was) for r in rows if r.lines[0].was], [(("U-K1",), "N", ["K"])])
        self.apply({"path": ["U-K1"], "old": "N", "new": "K", "balloon": "11"})
        self.assertEqual(self.tops(), {"P1": ["U"], "P2": ["U"], "P3": ["U"]})
        self.assertEqual(self.changes(), {})

    def test_undo_in_one_product_returns_to_the_original_assembly(self) -> None:
        self.apply({"path": ["U", "V"], "old": "B", "new": "N"})
        row = {"path": ["U-K1", "V-K1"], "old": "N", "new": "B", "product": "P2", "top": "1"}
        self.assertEqual(self.needs(row), [])
        self.apply(row)
        self.assertEqual(self.tops(), {"P1": ["U-K1"], "P2": ["U"], "P3": ["U-K1"]})
        self.assertEqual(self.inside("V-K1"), {("21", "N"): D(3)})

    def test_a_temp_with_a_typed_price_is_not_dropped_by_an_undo(self) -> None:
        self.apply({"path": ["U"], "old": "K", "new": "N"})
        with self.conn:
            self.conn.execute("UPDATE v2_temp_item SET material = '99'")
        self.apply({"path": ["U-K1"], "old": "N", "new": "K"})
        self.assertEqual(self.tops(), {"P1": ["U-K1"], "P2": ["U-K1"], "P3": ["U-K1"]})
        self.assertEqual(self.ws().temps[0].lines, ())

    def test_same_balloon_with_two_assemblies_is_handled_by_part(self) -> None:
        self.store.add_part(self.number, "A", "V", "1", ["P1"])  # 風船番号の無い V と U
        self.apply({"path": ["V"], "old": "B", "new": "N"})
        self.assertEqual(sorted(p.part for p in after_places(self.ws(), "A") if p.product == "P1" and p.quantity), ["K", "S", "U", "V-K1"])

    def test_refusals(self) -> None:
        for rows, text in [
            (({"path": ["U"], "old": "K", "new": "無い"},), "部品表にも仮品番にもありません"),
            (({"path": ["Z"], "old": "K", "new": "N"},), "直下にありません"),
            (({"path": ["U"], "old": "S", "new": "N"},), "U の中にありません"),
            (({"path": ["U", "S"], "old": "K", "new": "N"},), "S の中にありません"),
            (({"path": ["U"], "old": "K", "new": "N"}, {"path": ["U"], "old": "K", "new": "S", "balloon": "11"}), "2行あります"),
            (({"path": ["U"], "old": "K", "new": "N"}, {"path": ["U"], "old": "K", "new": "S"}), "2行あります"),
            (({"path": ["U"], "old": "V", "new": "N"}, {"path": ["U", "V"], "old": "B", "new": "N"}), "一緒に入れられません"),
        ]:
            with self.subTest(text), self.assertRaisesRegex(ValueError, text):
                self.rebuild(*rows).needs()
        row = {"path": ["U", "V"], "old": "B", "new": "N"}
        for codes, text in [({"U": "K"}, "部品表にある品番"), ({"U": "Q", "V": "Q"}, "中身の違う2つ"), ({"U": " "}, "入力してください")]:
            with self.subTest(text), self.assertRaisesRegex(ValueError, text):
                self.apply(row, codes=codes)
        self.apply({"path": ["U"], "old": "K", "new": "N"})
        with self.assertRaisesRegex(ValueError, "別の中身"):
            self.apply({"path": ["U-K1"], "old": "N", "new": "S", "product": "P1", "top": "1"}, codes={"U-K1": "U-K1"})
        with self.assertRaisesRegex(ValueError, "自身は入れられません"):
            self.apply({"path": ["U-K1"], "old": "N", "new": "U-K1"})
        self.assertEqual([t.code for t in self.ws().temps], ["U-K1"])

    def test_rename_carries_the_places_and_the_insides(self) -> None:
        self.apply({"path": ["U", "V"], "old": "B", "new": "N"})
        self.store.rename_temp(self.number, "V-K1", "VV")
        self.store.rename_temp(self.number, "U-K1", "UU")
        self.assertEqual(self.tops(), {"P1": ["UU"], "P2": ["UU"], "P3": ["UU"]})
        self.assertEqual(self.inside("UU"), {("11", "K"): D(2), ("12", "VV"): D(1)})
        self.assertEqual({p: after - now for p, (now, after, _) in self.material().items()}, {"P1": D(6), "P2": D(12), "P3": D(6)})
        # 仮品番の画面で2つの品番を入れ替えても、使っている所が付いてくる
        self.store.save_temps(self.number, [{"key": "VV", "code": "UU", "base": "V"}, {"key": "UU", "code": "VV", "base": "U"}])
        self.assertEqual(self.tops()["P1"], ["VV"])
        self.assertEqual(self.inside("VV"), {("11", "K"): D(2), ("12", "UU"): D(1)})
        for code, new, text in (("UU", "K", "部品表にある品番"), ("K", "X", "仮品番ではありません"), ("UU", "VV", "重複"), ("UU", "", "入力してください")):
            with self.subTest(text), self.assertRaisesRegex(ValueError, text):
                self.store.rename_temp(self.number, code, new)

    def test_api_gives_codes_and_saves(self) -> None:
        store = Store(":memory:")
        self.addCleanup(store.close)
        api = Api(store)
        study = StudyStore(store.connection)
        study.replace_bom(bom(), ALL)
        number = study.create_study()
        study.save_targets(number, ["P1", "P2"])

        def call(path: str, body: dict) -> dict:
            r = api.handle("POST", path, f"number={number}&plan=A", json.dumps(body, ensure_ascii=False).encode())
            self.assertEqual(r.status, 200, r.body.decode())
            return r.json()

        rows = [{"path": ["U"], "old": "K", "new": "N"}]
        data = call("/api/v2/inside/replace", {"rows": rows})
        self.assertEqual(data["made"], [{"code": "U", "new": "U-K1", "name": "U の名称", "typed": False}])
        self.assertEqual((data["typed"], data["bases"]), ([], {"U-K1": "U"}))
        data = call("/api/v2/temp-rename", {"code": "U-K1", "new": "T9"})
        self.assertEqual(data["tops"], {"T9": [{"product": "P1", "balloon": "1"}, {"product": "P2", "balloon": "1"}]})
        self.assertEqual({p["part"]: p["was"] for p in data["places"]}["T9"], ["U"])
        self.assertEqual([(r["part"], r["lines"]) for r in data["inside"] if r["path"] == ["T9"]][0],
                         ("N", [{"balloon": "11", "quantity": "2", "was": ["K"]}]))
        # 材料費を打った仮品番は、中を替えても金額が変わらないので知らせる
        with store.connection as c:
            c.execute("UPDATE v2_temp_item SET material = '99'")
        data = call("/api/v2/inside/replace", {"rows": [{"path": ["T9"], "old": "N", "new": "S"}]})
        self.assertEqual((data["typed"], data["made"]), (["T9"], []))

    def test_quantity_and_added_part_inside(self) -> None:
        tops = [{"product": "P1", "balloon": "1"}, {"product": "P3", "balloon": "1"}]
        self.apply({"path": ["U"], "tops": tops, "old": "K", "balloon": "11", "new": "N", "quantity": "5"},
                   {"path": ["U"], "tops": tops, "old": None, "balloon": "15", "new": "S", "quantity": "2"})
        self.assertEqual(self.tops(), {"P1": ["U-K1"], "P2": ["U"], "P3": ["U-K1"]})
        self.assertEqual(self.inside("U-K1"), {("11", "N"): D(5), ("12", "V"): D(1), ("15", "S"): D(2)})
        # 材料費: N 7×5 + S 30×2 − K 10×2
        self.assertEqual({p: after - now for p, (now, after, _) in self.material().items()}, {"P1": D(75), "P2": D(0), "P3": D(75)})
        # 数量だけ・消す・戻す（0 の箇所は足すのと同じ）
        self.apply({"path": ["U-K1"], "old": "S", "balloon": "15", "quantity": "0"}, {"path": ["U-K1", "V"], "old": "B", "balloon": "21", "quantity": "4"})
        self.assertEqual(self.inside("U-K1"), {("11", "N"): D(5), ("12", "V-K1"): D(1)})
        self.assertEqual(self.inside("V-K1"), {("21", "B"): D(4)})
        self.apply({"path": ["U-K1", "V-K1"], "old": "B", "balloon": "21", "quantity": "3"},
                   {"path": ["U-K1"], "old": "N", "balloon": "11", "new": "K", "quantity": "2"})
        self.assertEqual(self.tops(), {"P1": ["U"], "P2": ["U"], "P3": ["U"]})

    def test_a_row_for_a_parent_reaches_every_place_of_it_in_the_products(self) -> None:
        # V を直下にも持つ P1（U の中と直下）と、U の中だけの P2・P3。P1・P2 だけを変える
        self.store.add_places(self.number, "A", [{"product": "P1", "balloon": "8", "part": "V", "quantity": "1"}])
        rows = expand_parents(self.ws(), "A", [{"parent": "V", "products": ["P1", "P2"], "balloon": "21", "old": "B", "new": "N"}])
        self.assertEqual([(r["path"], [t["product"] for t in r["tops"]]) for r in rows], [(["U", "V"], ["P1", "P2"]), (["V"], ["P1"])])
        self.apply(*rows)
        self.assertEqual(self.tops(), {"P1": ["U-K1"], "P2": ["U-K1"], "P3": ["U"]})
        self.assertEqual(self.inside("U-K1")[("12", "V-K1")], D(1))
        self.assertEqual([p.part for p in after_places(self.ws(), "A") if p.product == "P1" and p.balloon == "8"], ["V-K1"])
        with self.assertRaisesRegex(ValueError, "下にありません"):
            expand_parents(self.ws(), "A", [{"parent": "V", "products": ["P1"], "old": "B", "new": "S"}])

    def test_quantity_and_added_part_refusals(self) -> None:
        top = {"tops": [{"product": "P1", "balloon": "1"}]}
        for rows, text in [
            (({"path": ["U"], "old": None, "balloon": "11", "new": "K", "quantity": "1"},), "もうあります"),
            (({"path": ["U"], "old": None, "balloon": "15", "new": "無い", "quantity": "1"},), "部品表にも仮品番にもありません"),
            (({"path": ["U"], "old": "K", "balloon": "11", "quantity": "-1"},), "0以上"),
            (({"path": ["U"], "old": None, "balloon": "15", "new": "S"},), "入力してください"),
            (({"path": ["U"], "tops": [], "old": "K", "new": "N"},), "tops は配列"),
            (({"path": ["U"], "tops": [{"product": "P1", "balloon": "9"}], "old": "K", "new": "N"},), "直下にありません"),
            (({"path": ["U"], "tops": [*top["tops"], {"product": "P2", "balloon": "9"}], "old": "K", "new": "N"},), "直下にありません"),
            (({"path": ["U"], **top, "old": "K", "quantity": "1"}, {"path": ["U"], **top, "old": "K", "quantity": "3"}), "2行あります"),
        ]:
            with self.subTest(text), self.assertRaisesRegex(ValueError, text):
                self.rebuild(*rows).needs()
        self.assertEqual(self.changes(), {})

    def test_stale_codes_are_refused(self) -> None:
        rebuild = self.rebuild({"path": ["U"], "old": "K", "new": "N"})
        with self.assertRaisesRegex(ValueError, "やり直し"):
            rebuild.change([])


def bom_two_places():
    v = bom()
    v.add_bom_lines([BomLine("U", "13", "K", 5)])
    return v


if __name__ == "__main__":
    unittest.main()
