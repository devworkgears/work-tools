"""語の読み替え: 効く所（画面の文字・取り込みの見出し・Excel の見出し）と、読み替えられる語の一覧。語は架空。"""

import io
import unittest

import example_settings
from bomcost import Store, settings
from bomcost.api import Api
from bomcost.importer import guess_columns, load_openpyxl, save_workbook
from bomcost.words import word_groups

try:
    load_openpyxl()
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False



class WordsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(settings.use, example_settings.EXAMPLE)

    def use(self, **labels: str) -> None:
        settings.use(settings.parse({**example_settings.EXAMPLE.to_json(), "labels": labels}))


class Replace(WordsCase):
    def test_whole_word_or_before_a_note(self) -> None:
        self.use(数量="個数", 型費="金型代")
        for text, want in (("数量", "個数"), ("型費（円）", "金型代（円）"), ("変更前の数量", "変更前の数量"), ("（数量）", "（数量）"), ("", "")):
            self.assertEqual(settings.word(text), want)
            self.assertEqual(settings.source_word(want), text)

    def test_two_words_must_not_look_the_same(self) -> None:
        for labels in ({"系列": "タイプ"}, {"系列": "X", "タイプ": "X"}, {"a": "X", "系列": "X"}):
            with self.subTest(labels=labels), self.assertRaisesRegex(ValueError, "同じ"):
                settings.parse({"labels": labels})
        settings.parse({"labels": {"系列": "タイプ", "タイプ": "系列"}})  # 入れ替えは区別できる
        self.assertEqual(settings.parse({"labels": {"系列": "系列"}}).labels, {})

    def test_each_word_is_listed_once(self) -> None:
        words = [w for g in word_groups() for w in g.words]
        self.assertEqual(len(words), len(set(words)))
        for word in ("品番", "系列", "加工費", "構成一覧", "風船番号", "子"):
            self.assertIn(word, words)


class Import(WordsCase):
    def test_headers_are_guessed_by_both_words(self) -> None:
        self.use(品番="部番", 材料費="資材費")
        self.assertEqual(guess_columns("item", ["部番", "名称"])[0], {"code": "部番", "name": "名称"})
        self.assertEqual(guess_columns("item", ["品番", "名称"])[0], {"code": "品番", "name": "名称"})
        self.assertEqual(guess_columns("cost", ["原価の品番", "資材費（円）"])[0]["material"], "資材費（円）")


@unittest.skipUnless(HAS_OPENPYXL, "openpyxl が無い")
class Excel(WordsCase):
    def test_header_row_and_label_columns_only(self) -> None:
        self.use(品番="部番", 材料費="=資材費")
        wb = load_openpyxl().Workbook()
        ws = wb.active
        ws.title = "S"
        for row in (["品番", "材料費（円）", "名称"], ["品番", "材料費", "材料費"]):
            ws.append(row)
        read = load_openpyxl().load_workbook(io.BytesIO(save_workbook(wb, {"S": [2]}))).active
        self.assertEqual([[c.value for c in r] for r in read], [["部番", "=資材費（円）", "名称"], ["品番", "=資材費", "材料費"]])
        self.assertEqual(read["B2"].data_type, "s")


class SettingsApi(WordsCase):
    def test_settings_list_the_words(self) -> None:
        with Store(":memory:") as store:
            data = Api(store).handle("GET", "/api/settings").json()
        self.assertEqual([(g["name"], g["imports"]) for g in data["words"]][:2],
                         [("取り込みの見出し（部品マスタ）", True), ("取り込みの見出し（構成マスタ）", True)])


if __name__ == "__main__":
    unittest.main()
