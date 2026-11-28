"""配る zip に入れるソースから、コメントと docstring を落とす（リポのソースはそのまま）。

Python: 構文木に読み、docstring を外して書き直す（# のコメントは構文木に無いので落ちる）。
JS: 文字列・テンプレート文字列・正規表現の中を避けて // と /* */ を落とす。
CSS: 文字列の中を避けて /* */ を落とす。
"""

import ast

# この文字の後の / は割り算でなく正規表現の始まり
_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else", "yield", "await"}


def strip_py(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body.pop(0)
                if not body and not isinstance(node, ast.Module):
                    body.append(ast.Pass())
    return ast.unparse(tree) + "\n"


def _skip_string(text: str, i: int) -> int:
    """text[i] の引用符で始まる文字列の、閉じた後の位置。"""
    quote, i = text[i], i + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote or (text[i] == "\n" and quote != "`"):
            return i + 1
        i += 1
    raise ValueError("閉じていない文字列")


def _skip_regex(text: str, i: int) -> int:
    """text[i] の / で始まる正規表現の、閉じた後の位置（フラグは普通の語として続く）。"""
    i += 1
    klass = False
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "\n":
            raise ValueError("閉じていない正規表現")
        if c == "[":
            klass = True
        elif c == "]":
            klass = False
        elif c == "/" and not klass:
            return i + 1
        i += 1
    raise ValueError("閉じていない正規表現")


def _regex_allowed(out: list[str]) -> bool:
    """ここまでに書いた物から見て、次の / が正規表現の始まりか。"""
    text = "".join(out).rstrip()
    if not text:
        return True
    if text[-1] in _REGEX_AFTER:
        return True
    word = ""
    for c in reversed(text):
        if not (c.isalnum() or c in "_$"):
            break
        word = c + word
    return word in _REGEX_KEYWORDS


def _drop_blank_lines(text: str) -> str:
    """コメントだけだった行の跡（空白だけの行）と、行末の空白を落とす。"""
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line) + "\n"


def strip_js(text: str) -> str:
    out: list[str] = []
    i, depth = 0, []  # depth: テンプレート文字列の ${ の中で数える { の深さ
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if c in "'\"":
            end = _skip_string(text, i)
            out.append(text[i:end])
            i = end
        elif c == "`" or (c == "}" and depth and depth[-1] == 0):
            # テンプレート文字列: ` か、${ の閉じから、次の ${ か閉じの ` まで
            if c == "}":
                depth.pop()
            j = i + 1
            while j < len(text):
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "`":
                    j += 1
                    break
                if text[j] == "$" and j + 1 < len(text) and text[j + 1] == "{":
                    j += 2
                    depth.append(0)
                    break
                j += 1
            else:
                raise ValueError("閉じていないテンプレート文字列")
            out.append(text[i:j])
            i = j
        elif c == "/" and nxt == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
        elif c == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("閉じていないコメント")
            out.append("\n" if "\n" in text[i:end] else " ")  # 改行をまたぐコメントは改行に（行末の ; の補いを変えない）
            i = end + 2
        elif c == "/" and _regex_allowed(out):
            end = _skip_regex(text, i)
            out.append(text[i:end])
            i = end
        else:
            if depth and c == "{":
                depth[-1] += 1
            elif depth and c == "}":
                depth[-1] -= 1
            out.append(c)
            i += 1
    return _drop_blank_lines("".join(out))


def strip_css(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c in "'\"":
            end = _skip_string(text, i)
            out.append(text[i:end])
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("閉じていないコメント")
            i = end + 2
        else:
            out.append(c)
            i += 1
    return _drop_blank_lines("".join(out))


STRIPPERS = {".py": strip_py, ".js": strip_js, ".css": strip_css}


def strip(name: str, data: bytes) -> bytes:
    """拡張子で決めて落とす。対象でないファイルはそのまま返す。"""
    for suffix, fn in STRIPPERS.items():
        if name.endswith(suffix):
            return fn(data.decode("utf-8")).encode("utf-8")
    return data
