"""読み替えられる語の一覧。設定の画面に並べる（ここに無い語も、画面の文字としては読み替えられる）。"""

from dataclasses import dataclass

from .formula import Head
from .importer import TABLE_FIELDS
from .study import ATTRIBUTES


@dataclass(frozen=True)
class WordGroup:
    name: str
    words: tuple[str, ...]
    imports: bool = False  # 取り込むファイルの見出しを当てる時にも使う


_IMPORT_NAMES = {"item": "部品マスタ", "bom_line": "構成マスタ", "cost": "原価", "volume": "年間台数"}


def word_groups() -> list[WordGroup]:
    """語は、最初に出た組に1回だけ入れる。"""
    raw: list[WordGroup] = []
    for table, fields in TABLE_FIELDS.items():
        words = [f.label for f in fields]
        if table == "item":
            words += [label for _, label in ATTRIBUTES]
        raw.append(WordGroup(f"取り込みの見出し（{_IMPORT_NAMES[table]}）", tuple(words), True))
    raw.append(WordGroup("計算した費目", tuple(h.value for h in Head)))
    raw.append(WordGroup("画面の名前", ("対象品番", "仮品番", "構成変更", "費目変更", "バリエーション", "構成一覧", "変更一覧", "結果", "計算条件",
                                      "版確定", "品番検索", "使用先", "主対象", "風船", "子")))
    raw.append(WordGroup("検討の項目", ("検討番号", "題目", "担当", "承認者", "案件番号", "年間の影響", "1台あたりの平均の増減")))
    seen: set[str] = set()
    out = []
    for g in raw:
        words = tuple(w for w in dict.fromkeys(g.words) if w not in seen)
        seen.update(words)
        if words:
            out.append(WordGroup(g.name, words, g.imports))
    return out


def check_labels(labels: dict[str, str]) -> None:
    """2つの語が同じ語に見えるようになる読み替えを断る（Excel の見出しと取り込みの列が区別できなくなる）。"""
    words = {w for g in word_groups() for w in g.words} | set(labels)
    shown: dict[str, str] = {}
    for word in sorted(words):
        use = labels.get(word, word)
        if use in shown:
            raise ValueError(f"語の読み替えで「{shown[use]}」と「{word}」が同じ「{use}」になります")
        shown[use] = word
