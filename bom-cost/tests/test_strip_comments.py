"""配る zip に入れる時にコメントを落とす。中身（コード・文字列・正規表現）は変えない。"""

import ast
import unittest
from pathlib import Path

from strip_comments import strip, strip_css, strip_js, strip_py

APP = Path(__file__).resolve().parent.parent / "bomcost"


class StripJs(unittest.TestCase):
    def test_comments_go_and_code_stays(self) -> None:
        self.assertEqual(strip_js("// 行\nconst a = 1; // 行末\n/* 塊 */\nf(a);\n"), "const a = 1;\nf(a);\n")

    def test_strings_keep_comment_marks(self) -> None:
        src = 'const a = "// no"; const b = \'/* no */\'; const c = `x // ${a /* c */ + "}"} y`;\n'
        self.assertEqual(strip_js(src), 'const a = "// no"; const b = \'/* no */\'; const c = `x // ${a   + "}"} y`;\n')

    def test_regex_and_division(self) -> None:
        src = "const r = /a\\/\\/b[/]/g; const d = x / 2 / y; // 割り算\nif (s.match(/^\\/\\//)) z();\n"
        self.assertEqual(strip_js(src), "const r = /a\\/\\/b[/]/g; const d = x / 2 / y;\nif (s.match(/^\\/\\//)) z();\n")

    def test_regex_after_return(self) -> None:
        self.assertEqual(strip_js("function f() { return /x/.test(s); } // c\n"), "function f() { return /x/.test(s); }\n")

    def test_block_comment_across_lines_keeps_the_line_break(self) -> None:
        self.assertEqual(strip_js("a = b /* 1\n2 */\nc()\n"), "a = b\nc()\n")

    def test_nested_template(self) -> None:
        src = "x = `a ${f(`b ${c}`)} {d}`; // e\n"
        self.assertEqual(strip_js(src), "x = `a ${f(`b ${c}`)} {d}`;\n")


class StripOthers(unittest.TestCase):
    def test_python_drops_docstrings_and_comments(self) -> None:
        src = '"""m"""\n\n\ndef f():\n    """d"""\n\n\nclass C:\n    """c"""\n    x = "# keep"  # drop\n'
        self.assertEqual(strip_py(src), "def f():\n    pass\n\nclass C:\n    x = '# keep'\n")

    def test_css(self) -> None:
        self.assertEqual(strip_css('/* a */\n.x { content: "/* keep */"; } /* b */\n'), '.x { content: "/* keep */"; }\n')

    def test_other_files_are_untouched(self) -> None:
        self.assertEqual(strip("a.html", b"<!-- x -->"), b"<!-- x -->")

    def test_real_python_sources_keep_the_same_code(self) -> None:
        # docstring を外した構文木が、落とした後の構文木と同じ
        for path in APP.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    b = node.body
                    if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                        b.pop(0)
                        if not b and not isinstance(node, ast.Module):
                            b.append(ast.Pass())
            with self.subTest(path=path.name):
                self.assertEqual(ast.dump(ast.parse(strip_py(text))), ast.dump(tree))

    def test_real_js_and_css_lose_every_comment(self) -> None:
        for path in [*APP.rglob("*.js"), *APP.rglob("*.css")]:
            out = strip(path.name, path.read_bytes()).decode("utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("/*", out)
                self.assertFalse([line for line in out.splitlines() if line.lstrip().startswith("//")])


if __name__ == "__main__":
    unittest.main()
