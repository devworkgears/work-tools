"""入口の正規化。品番・フラグ・数量・金額の型の揺れをここで1回だけそろえる。

計算の本体は、ここを通った値（品番は str・数量と金額は Decimal・フラグは bool）だけを見る。
"""

from decimal import Decimal, InvalidOperation

NumberLike = Decimal | int | float | str


def to_decimal(value: NumberLike, *, name: str = "値") -> Decimal:
    """Decimal・int・float・数字の文字列（桁区切り・前後の空白可）を Decimal にする。

    float は Decimal(str(値)) で直す（Decimal(0.1) は 0.1000000000000000055… になるため）。
    bool は数として扱わない。
    """
    # bool は int の子クラスなので、int より先に弾く
    if isinstance(value, bool):
        raise TypeError(f"{name}に bool は使えない: {value!r}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"{name}を数として読めない: {value!r}") from exc
    else:
        raise TypeError(f"{name}に {type(value).__name__} は使えない: {value!r}")
    if not result.is_finite():
        raise ValueError(f"{name}が有限の数でない: {value!r}")
    return result


def to_optional_decimal(value: NumberLike | None, *, name: str = "値") -> Decimal | None:
    """空欄（None・空文字・空白だけ）は未登録として None のまま返す。0 とは区別する。"""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return to_decimal(value, name=name)


def to_code(value: str | int | float, *, name: str = "品番") -> str:
    """品番を文字列にそろえる。数字だけの品番が int で来ても同じ品番として結ぶ。前後の空白は落とす。"""
    if isinstance(value, bool):
        raise TypeError(f"{name}に bool は使えない: {value!r}")
    if isinstance(value, int):
        return str(value)
    # Excel 経由で数字だけの品番が 12345.0 の形で来ることがあるので、整数の値に限り int と同じに扱う
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            raise ValueError(f"{name}が空")
        return text
    # 小数部のある float の品番は元の表記が分からないので受けない
    raise TypeError(f"{name}に {type(value).__name__} は使えない: {value!r}")


def to_optional_code(value: str | int | None, *, name: str = "品番") -> str | None:
    """風船番号など、空欄があり得る識別子。空欄は None。"""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    return to_code(value, name=name)


def to_symbol(value: str | int | float | None, *, name: str = "記号") -> str:
    """区分・状態などの記号を文字列にそろえる。知らない記号も弾かない。空欄は空文字。"""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return ""
    return to_code(value, name=name)


_TRUE_TEXTS = {"1", "true", "yes"}
_FALSE_TEXTS = {"0", "false", "no", ""}


def to_flag(value: bool | int | str, *, name: str = "フラグ") -> bool:
    """1 / 0 / "1" / "0" / True / False を bool にそろえる。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_TEXTS:
            return True
        if text in _FALSE_TEXTS:
            return False
    raise ValueError(f"{name}を 1・0 として読めない: {value!r}")
