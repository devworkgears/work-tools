"""設定（率・品番の桁・区分の記号・型の償却年数・語の読み替え）。DB と同じフォルダの settings.json に置く。

率の既定は無い（入れ忘れたまま計算が通らないように、空なら計算を断る）。ほかの項目は空なら一般の形で動く。
起動の時に load で読み、設定の画面（/settings/）の保存で書き換える。1つのプロセスで使う設定は1つ。
"""

import json
import os
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

NAME = "settings.json"
PRODUCT_KIND = "製品"
PART_KIND = "部品"


class SettingsMissing(RuntimeError):
    """計算に要る設定が空。値の誤り（ValueError）とは別にして、未登録の扱いに紛れさせない。"""


@dataclass(frozen=True)
class Rates:
    adjustment: Decimal  # 調整費の率
    indirect: Decimal  # 間接費の率

    @property
    def factor(self) -> Decimal:
        """材料費から製造原価への係数。"""
        return (1 + self.adjustment) * (1 + self.indirect)


@dataclass(frozen=True)
class Settings:
    adjustment_percent: Decimal | None = None
    indirect_percent: Decimal | None = None
    # 原価の表の品番 ＝ 基本の品番（code_base 桁）＋枝番（code_branch 桁）＋後ろを埋める 0（合わせて code_total 桁）
    code_total: int | None = None
    code_base: int | None = None
    code_branch: int | None = None
    product_kind: str = ""
    part_kind: str = ""
    mold_years: Decimal | None = None
    labels: Mapping[str, str] = field(default_factory=dict)  # 画面と取り込みの見出しの語 → 使う語

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, Decimal) else dict(value) if f.name == "labels" else value
        return out


_PERCENTS = {"adjustment_percent": "調整費の率", "indirect_percent": "間接費の率"}
_DIGITS = {"code_total": "原価の品番の桁数", "code_base": "基本の品番の桁数", "code_branch": "枝番の桁数"}


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _number(value: Any, name: str) -> Decimal:
    try:
        number = None if isinstance(value, bool) else Decimal(str(value).strip())
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite():
        raise ValueError(f"{name}は数で入れてください（{value}）")
    return number


def parse(data: Any) -> Settings:
    """JSON の中身から。知らない項目・形の違いは ValueError。"""
    if not isinstance(data, dict):
        raise ValueError("設定の形が違います（項目と値の組で書く）")
    unknown = sorted(set(data) - {f.name for f in fields(Settings)})
    if unknown:
        raise ValueError(f"設定に知らない項目があります（{'・'.join(unknown)}）")
    out: dict[str, Any] = {}
    for key, name in _PERCENTS.items():
        if not _blank(data.get(key)):
            out[key] = _number(data[key], name)
            if not 0 <= out[key] < 100:
                raise ValueError(f"{name}は 0 以上 100 未満の % で入れてください（{data[key]}）")
    if ("adjustment_percent" in out) != ("indirect_percent" in out):
        raise ValueError("調整費の率と間接費の率は両方入れてください")
    for key, name in _DIGITS.items():
        if not _blank(data.get(key)):
            number = _number(data[key], name)
            if number != number.to_integral_value() or not 0 < number <= 100:
                raise ValueError(f"{name}は 1 以上の整数で入れてください（{data[key]}）")
            out[key] = int(number)
    digits = [out.get(k) for k in _DIGITS]
    if any(digits) and not all(digits):
        raise ValueError("品番の桁数は3つとも入れるか、3つとも空にしてください")
    if all(digits) and out["code_total"] < out["code_base"] + out["code_branch"]:
        raise ValueError("原価の品番の桁数は、基本の品番と枝番の桁数の合計以上にしてください")
    for key in ("product_kind", "part_kind"):
        if not _blank(data.get(key)):
            out[key] = str(data[key]).strip()
    if out.get("product_kind", PRODUCT_KIND) == out.get("part_kind", PART_KIND):
        raise ValueError("製品と部品の区分の記号は別の物にしてください")
    if not _blank(data.get("mold_years")):
        out["mold_years"] = _number(data["mold_years"], "型の償却年数")
        if out["mold_years"] <= 0:
            raise ValueError("型の償却年数は正の数で入れてください")
    labels = data.get("labels") or {}
    if not isinstance(labels, dict) or not all(isinstance(v, str) for v in labels.values()):
        raise ValueError("語の読み替えの形が違います（元の語 → 使う語 の組で書く）")
    out["labels"] = {k.strip(): v.strip() for k, v in labels.items() if k.strip() and v.strip()}
    return Settings(**out)


_current = Settings()


def current() -> Settings:
    return _current


def use(settings: Settings) -> None:
    global _current
    _current = settings


def path_for(db_path: Path | None) -> Path | None:
    return None if db_path is None else db_path.parent / NAME


def load(db_path: Path | None) -> Settings:
    """DB と同じフォルダの settings.json を読んで使う。無ければ空の設定。読めなければ ValueError。"""
    path = path_for(db_path)
    if path is None or not path.is_file():
        settings = Settings()
    else:
        try:
            settings = parse(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as e:
            raise ValueError(f"{path} を読めません（{e}）") from None
    use(settings)
    return settings


def write(path: Path, settings: Settings) -> None:
    part = path.with_suffix(".json.part")
    part.write_text(json.dumps(settings.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(part, path)


def save(db_path: Path | None, settings: Settings) -> None:
    """settings.json に書いてから使う（書けなければ今の設定のまま）。DB がファイルでない時は書かない。"""
    path = path_for(db_path)
    if path is not None:
        write(path, settings)
    use(settings)


# 値の使い方 -----------------------------------------------------------------


def rates() -> Rates:
    s = _current
    if s.adjustment_percent is None or s.indirect_percent is None:
        raise SettingsMissing(f"調整費と間接費の率が設定にありません（{NAME} を DB と同じフォルダに置いて起動し直してください）")
    return Rates(s.adjustment_percent / 100, s.indirect_percent / 100)


def product_kind() -> str:
    return _current.product_kind or PRODUCT_KIND


def part_kind() -> str:
    return _current.part_kind or PART_KIND


def mold_years() -> Decimal | None:
    return _current.mold_years


def labels() -> Mapping[str, str]:
    return _current.labels
