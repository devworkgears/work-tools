"""製品群の API: 版の製品を並べ、部品の名前で絞った製品×部品の行を、製品ごとの試算で替える・足す。

製品ごとの試算は今の試算（Scenario）をそのまま使い、検討名・土台の製品・版の組で引く（保存の形は変えない）。
変更は今の試算の変更（Api.apply_change）に渡す。取り消しは今の undo のほか、途中の1回の操作も抜ける（undo_change）。
"""

import json
import unicodedata
from collections import Counter
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

from .api import (XLSX_TYPE, ApiError, Response, _flag, _head, _json_response, _known_codes, _place, _require_known,
                  _record_json, _s, _text_field, _View, scenario_annual, version_label)
from .comparebook import compare_file_name, compare_workbook
from .formula import EDITABLE_HEADS, Head
from .model import BomLine, CostHead, Version
from .normalize import to_code
from .scenario import ChangeKind, ChangeRecord, Scenario

if TYPE_CHECKING:
    from .api import Api

DEFAULT_PRODUCT_LIMIT = 500
DEFAULT_ROW_LIMIT = 2000
MAX_LIMIT = 20000
PRODUCT_KIND_TEXT = "製品"
PRODUCT_KIND_CODE = "2"  # 区分の記号: 製品
PART_KIND_CODE = "1"  # 区分の記号: 部品（製品の記号が PRODUCT_KIND_CODE の時に付ける）


def part_kind(product_kind: str, kind: str) -> str:
    """部品として登録する品目の区分。記号が空で、製品の記号が PRODUCT_KIND_CODE なら PART_KIND_CODE。"""
    return kind or (PART_KIND_CODE if product_kind == PRODUCT_KIND_CODE else "")


def is_product_kind(product_kind: str, kind: str) -> bool:
    """部品に付けられない区分の記号か（製品とみなす記号と PRODUCT_KIND_CODE）。"""
    return bool(kind) and kind in (product_kind, PRODUCT_KIND_CODE)


# ③で並べる8費目（式の並び）。材料費小計のほかは製品を一番上にして式で出す
COMPARE_HEADS: tuple[Head, ...] = tuple(Head)


def _names(value: Any) -> list[str]:
    """検討名の並び。JSON の配列の文字列か、1つの名前。空と重なりは除く。"""
    text = str(value or "").strip()
    if text.startswith("["):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raise ApiError(400, f"検討名（names）を読めない: {text}") from None
        if not isinstance(raw, list):
            raise ApiError(400, f"検討名（names）を読めない: {text}")
        items = [str(x).strip() for x in raw]
    else:
        items = [text]
    return list(dict.fromkeys(x for x in items if x))


def _limit(value: Any, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip()
    if not text.isdigit() or int(text) < 1:
        raise ApiError(400, f"件数の上限（limit）は 1 以上の整数にする: {value}")
    return min(int(text), MAX_LIMIT)


def _fold(text: str) -> str:
    """全角半角・大文字小文字・ハイフンの有無を同じに扱う形。"""
    return unicodedata.normalize("NFKC", text).casefold().replace("-", "").replace("‐", "").replace("−", "")


def _words(needle: str) -> list[str]:
    return [w for w in (_fold(x) for x in unicodedata.normalize("NFKC", needle).split()) if w]


def _matches(texts: tuple[str, ...], needle: str) -> bool:
    """空白で区切った語の全部が、どれかの text に含まれる。"""
    folded = [_fold(t) for t in texts]
    return all(any(w in t for t in folded) for w in _words(needle))


def _code_starts(code: str, needle: str) -> bool:
    return any(_fold(code).startswith(w) for w in _words(needle))


class _Structure:
    """版の構成を親 → 子の行（同じ子品番は合算）に1回だけ並べ直す。製品の数だけたどるので毎回の走査を避ける。"""

    def __init__(self, version: Version) -> None:
        merged: dict[str, dict[str, Decimal]] = {}
        for line in version.lines:
            children = merged.setdefault(line.parent, {})
            children[line.child] = children.get(line.child, Decimal(0)) + line.quantity
        self.children = {
            parent: tuple(BomLine(parent, None, child, qty) for child, qty in kids.items())
            for parent, kids in merged.items()
        }
        child_codes = {line.child for line in version.lines}
        self.tops = [p for p in self.children if p not in child_codes]

    def usage(self, product: str) -> dict[str, Decimal]:
        """製品1台あたりの各品目の使用数（途中の組立品も含む・見つけた順）。"""
        usage: dict[str, Decimal] = {}

        def walk(code: str, multiplier: Decimal, path: frozenset[str]) -> None:
            if code in path:
                raise ValueError(f"構成が循環している: {code}")
            for line in self.children.get(code, ()):
                qty = multiplier * line.quantity
                usage[line.child] = usage.get(line.child, Decimal(0)) + qty
                walk(line.child, qty, path | {code})

        walk(product, Decimal(1), frozenset())
        return usage


class Group:
    """製品群の画面の API の中身。api は Api（保存・版・試算の読み込みと試算の変更を借りる）。"""

    def __init__(self, api: "Api") -> None:
        self.api = api
        self.store = api.store

    def routes(self) -> list[tuple[str, str, Callable[..., Response]]]:
        return [
            ("GET", r"/api/group/products", self.products),
            ("GET", r"/api/group/parts", self.parts),
            ("GET", r"/api/group/parts/by-code", self.parts_by_code),
            ("POST", r"/api/group/changes/by-code", self.replace_code),
            ("GET", r"/api/group/names", self.names),
            ("GET", r"/api/group/trials", self.trials),
            ("DELETE", r"/api/group/trials", self.delete_trial),
            ("GET", r"/api/group/changes", self.changes),
            ("POST", r"/api/group/changes", self.add_change),
            ("POST", r"/api/group/changes/bulk", self.add_changes),
            ("POST", r"/api/group/changes/undo", self.undo_change),
            ("GET", r"/api/group/tree", self.tree),
            ("GET", r"/api/group/heads", self.heads),
            ("GET", r"/api/group/heads/all", self.trial_heads),
            ("POST", r"/api/group/heads/bulk", self.set_heads),
            ("POST", r"/api/group/scenarios", self.add_scenarios),
            ("GET", r"/api/group/compare", self.compare),
            ("GET", r"/api/group/compare/table", self.compare_table),
            ("GET", r"/api/group/compare/table/xlsx", self.compare_table_xlsx),
            ("GET", r"/api/group/compare/breakdown", self.compare_breakdown),
        ]

    # 比べる ------------------------------------------------------------------

    def compare(self, params: dict[str, str]) -> Response:
        """製品ごとに元（版の構成）の行と、試算がある検討名ごとの新（土台＋変更）の行を、8費目（COMPARE_HEADS）で並べる。

        製品の償却費が未登録の行があれば 400。ignore_amortization を付けると償却費は null で、式では 0 として通す。
        incomplete は未登録を除いた・0 とした値を使った費目。
        新の行には元との差（differences）と変更の一覧（changes・原価は今の登録値で当て直した値）を添え、summary に検討名×費目の合計の差と、
        下がった・上がった・変わらない・未登録の製品の数を置く。
        """
        version, names, ignore, codes, limit, rows = self._compare_rows(params, with_changes=True)
        return _json_response(
            {
                "version_id": version.version_id,
                "names": names,
                "heads": [{"id": h.name, "label": h.value} for h in COMPARE_HEADS],
                "ignore_amortization": ignore,
                "count": len(codes),
                "limit": limit,
                "truncated": len(codes) > limit,
                "rows": rows,
                "summary": _compare_summary(names, rows),
            }
        )

    def compare_table(self, params: dict[str, str]) -> Response:
        """compare と同じ問い合わせを、提出用の表の形で。試算がある製品ごとに 現行・検討名ごとの 変更後・差・差%、下に計。

        差% は 差 ÷ 現行 ×100（小数1桁・四捨五入）で、現行が未登録か 0 なら None。計は検討名×費目で、
        現行・変更後のどちらかが未登録の製品を除いた合計（除いた数を excluded に）。
        製品ごとの volume は版の年間台数（マスタ登録を重ねた値）、検討名ごとの effect は 差 × 年間台数（年間効果額）で、
        どちらかが未登録なら None。effect_totals は検討名×費目の年間効果額の合計と、None で除いた製品の数。
        """
        return _json_response(self._table_data(params)[1])

    def compare_table_xlsx(self, params: dict[str, str]) -> Response:
        """compare/table と同じ問い合わせの表を .xlsx で（比較のシートと前提のシート）。"""
        version, data = self._table_data(params)
        query = (params.get("q") or "").strip()
        body = compare_workbook(data, version_label(version), query, datetime.now())
        # 版 ID は画面に出さないので、ファイル名も開発番号と第何版で
        name = compare_file_name(f"{version.dev_number or version.version_id}第{version.revision}版", data["names"])
        return Response(200, body, XLSX_TYPE, {
            "Content-Disposition": f"attachment; filename=\"bom-cost-compare.xlsx\"; filename*=UTF-8''{quote(name)}",
        })

    def compare_breakdown(self, params: dict[str, str]) -> Response:
        """選んだ製品（products）の材料費の内訳。製品の直の子ごとに 材料費小計 × 員数 を、子の合計に対する割合の高い順に。

        by は まとめ方（code＝品番・name＝名称・name_spec＝名称＋記号・寸法）、weight は 数え方（unit＝1台ずつ足す・
        volume＝年間台数を掛けて足す。台数が未登録の製品は除いて excluded に）。series の先頭は現行、続いて検討名（names）
        ごとの変更後（その検討名の試算が無い製品は現行の構成で数え、数を without_trial に）。
        材料費が未登録の子は 0 にせず、行の amount を None（一部の子だけなら分かる分の合計）にして missing に数え、割合の分母から除く。
        """
        version = self._version(params)
        names = _names(params.get("names"))
        products = [to_code(c) for c in _names(params.get("products"))]
        if not products:
            raise ApiError(400, "製品（products）が空")
        for code in products:
            _require_known(version, code, "製品")
        by = (params.get("by") or "code").strip()
        if by not in BREAKDOWN_KEYS:
            raise ApiError(400, f"まとめ方（by）は {'・'.join(BREAKDOWN_KEYS)} のどれか: {by}")
        weight = (params.get("weight") or "unit").strip()
        if weight not in ("unit", "volume"):
            raise ApiError(400, f"数え方（weight）は unit・volume のどちらか: {weight}")
        weights: dict[str, Decimal] = {}
        excluded: list[str] = []
        for code in products:
            volume = version.volume(code)
            if weight == "unit":
                weights[code] = Decimal(1)
            elif volume is None:
                excluded.append(code)
            else:
                weights[code] = volume.annual
        structure = _Structure(version)
        view = _View(Scenario("", products[0], f"{products[0]}#", version, CostHead.MATERIAL,
                              self.store.load_scope_policy()), "bulk", CostHead.MATERIAL, False)
        current = {code: _base_parts(structure, code) for code in weights}
        series = [_breakdown_series(None, view, version, by, weights, current, 0)]
        for name in names:
            scenario_ids = self._scenarios(version, name)
            parts = {code: (_trial_parts(structure, self.store.load_scenario(scenario_ids[code], version))
                            if code in scenario_ids else current[code]) for code in weights}
            series.append(_breakdown_series(name, view, version, by, weights, parts,
                                            sum(1 for code in weights if code not in scenario_ids)))
        return _json_response({"version_id": version.version_id, "by": by, "weight": weight,
                               "products": list(weights), "excluded": excluded, "series": series})

    def _table_data(self, params: dict[str, str]) -> tuple[Version, dict[str, Any]]:
        version, names, ignore, codes, limit, rows = self._compare_rows(params, with_changes=False)
        products: list[dict[str, Any]] = []
        for row in rows:
            if row["kind"] == "base":
                volume = version.volume(row["product"])
                products.append({"product": row["product"], "product_name": row["product_name"], "specification": row["specification"],
                                 "volume": None if volume is None else _s(volume.annual),
                                 "current": row["costs"], "incomplete": row["incomplete"], "trials": []})
                continue
            p = products[-1]
            percents = {h: _percent(row["differences"][h], p["current"][h]) for h in row["costs"]}
            effects = {h: _effect(row["differences"][h], p["volume"]) for h in row["costs"]}
            p["trials"].append({"name": row["name"], "scenario_id": row["scenario_id"], "after": row["costs"],
                                "difference": row["differences"], "percent": percents, "effect": effects,
                                # 未登録を除いた・0 とした値を使った費目（差・差%・年間効果額も一部未登録）
                                "incomplete": sorted(set(row["incomplete"]) | set(p["incomplete"]))})
        return version, {
            "version_id": version.version_id,
            "names": names,
            "heads": [{"id": h.name, "label": h.value} for h in COMPARE_HEADS],
            "ignore_amortization": ignore,
            "count": len(codes),
            "limit": limit,
            "truncated": len(codes) > limit,
            "products": [p for p in products if p["trials"]],
            "without_trial": sum(1 for p in products if not p["trials"]),
            "totals": {n: _table_totals(n, products) for n in names},
            "effect_totals": {n: _effect_totals(n, products) for n in names},
        }

    def _compare_rows(self, params: dict[str, str], with_changes: bool
                      ) -> tuple[Version, list[str], bool, list[str], int, list[dict[str, Any]]]:
        version = self._version(params)
        names = _names(params.get("names"))
        if not names:
            raise ApiError(400, "検討名（names）が空")
        ignore = _flag(params.get("ignore_amortization"))
        _, _, codes = self._products(version, params)
        limit = _limit(params.get("limit"), DEFAULT_PRODUCT_LIMIT)
        shown = codes[:limit]
        rows: list[dict[str, Any]] = []
        failed: list[str] = []
        if shown:
            policy = self.store.load_scope_policy()
            bulk = _View(Scenario("", shown[0], f"{shown[0]}#", version, CostHead.MATERIAL, policy),
                         "bulk", CostHead.MATERIAL, False, ignore_amortization=ignore)
            scenario_ids = {n: self._scenarios(version, n) for n in names}
            for code in shown:
                product_name = self._name(version, code)
                base = self._compare_row(bulk, code, code, product_name, None, None, ignore, failed)
                base["specification"] = version.items[code].specification if code in version.items else ""
                rows.append(base)
                for n in names:
                    sid = scenario_ids[n].get(code)
                    if sid is None:
                        continue
                    scenario = self.store.load_scenario(sid, version)
                    view = _View(scenario, "diff", CostHead.MATERIAL, False, ignore_amortization=ignore)
                    row = self._compare_row(view, view.root, code, product_name, n, sid, ignore, failed)
                    row["differences"] = _differences(base["costs"], row["costs"])
                    if with_changes:
                        row["changes"] = [_batch_json(records) for records in replayed_batches(scenario)]
                    rows.append(row)
        if failed:
            raise ApiError(400, "償却費を出せない（未登録あり）: " + "、".join(failed))
        return version, names, ignore, codes, limit, rows

    @staticmethod
    def _compare_row(view: _View, root: str, code: str, product_name: str, name: str | None, sid: int | None,
                     ignore: bool, failed: list[str]) -> dict[str, Any]:
        b = view.breakdown(root)
        if not ignore and b.value(Head.AMORTIZATION) is None:
            failed.append(code if name is None else f"{code}（{name}）")
        costs = {head.name: _s(b.value(head)) for head in COMPARE_HEADS}
        incomplete = [head.name for head in COMPARE_HEADS if b.is_incomplete(head)]
        return {
            "kind": "base" if name is None else "trial",
            "product": code,
            "product_name": product_name,
            "name": name,
            "scenario_id": sid,
            "costs": costs,
            "incomplete": incomplete,
        }

    # 変更の一覧 --------------------------------------------------------------

    def changes(self, params: dict[str, str]) -> Response:
        """検討名の全製品（product を付けるとその製品だけ）の変更を、1回の操作ごとに1表に。合計の差も返す。

        原価は履歴に残した値でなく、今の版（manual を重ねたもの）の上で変更の並びを当て直した値（記録は書き換えない）。
        費目は head（省けば試算ごとの費目）。
        """
        version = self._version(params)
        name = (params.get("name") or "").strip()
        if not name:
            raise ApiError(400, "検討名（name）が空")
        head = _head(params["head"], CostHead.MATERIAL) if (params.get("head") or "").strip() else None
        only = (params.get("product") or "").strip()
        only = to_code(only) if only else None
        rows: list[dict[str, Any]] = []
        total = Decimal(0)
        missing = 0
        for product, sid in sorted(self._scenarios(version, name).items()):
            if only is not None and product != only:
                continue
            scenario = self.store.load_scenario(sid, version)
            for recorded, records in zip(_batches(scenario.history), replayed_batches(scenario, head)):
                row = _batch_json(records)
                row["sequence"], row["batch"] = recorded[0].sequence, recorded[0].batch
                row["part_name"] = self._name(version, row["part"])
                row["part_specification"] = version.items[row["part"]].specification if row["part"] in version.items else ""
                row["part_kind"] = self._kind(version, row["part"])
                row["replacement_specifications"] = {
                    r["code"]: version.items[r["code"]].specification if r["code"] in version.items else ""
                    for r in row["replacements"]
                }
                for r in row["replacements"]:
                    r["name"] = self._name(version, r["code"])
                    r["kind"] = self._kind(version, r["code"])
                if row["difference"] is None:
                    missing += 1
                else:
                    total += Decimal(row["difference"])
                rows.append({"product": product, "product_name": self._name(version, product),
                             "scenario_id": sid, **row})
        return _json_response(
            {
                "version_id": version.version_id,
                "name": name,
                "rows": rows,
                "count": len(rows),
                "difference": _s(total),
                "missing_count": missing,  # 変更前か変更後が未登録で、合計に入れなかった行の数
            }
        )

    # 製品 --------------------------------------------------------------------

    @staticmethod
    def product_kind(version: Version, structure: _Structure, requested: str = "") -> str | None:
        """製品とみなす区分の記号。指定 → 品目にあれば PRODUCT_KIND_CODE → 「製品」 → 構成の一番上の親に多い記号の順。"""
        if requested:
            return requested
        kinds = {item.kind for item in version.items.values()}
        if PRODUCT_KIND_CODE in kinds:
            return PRODUCT_KIND_CODE
        if PRODUCT_KIND_TEXT in kinds:
            return PRODUCT_KIND_TEXT
        top_kinds = Counter(
            version.items[code].kind for code in structure.tops if code in version.items and version.items[code].kind
        )
        return top_kinds.most_common(1)[0][0] if top_kinds else None

    @staticmethod
    def product_codes(version: Version, structure: _Structure, kind: str | None) -> list[str]:
        """区分が製品の品目と、区分が空の構成の一番上の親。品番の順。"""
        codes = {code for code, item in version.items.items() if kind and item.kind == kind}
        for code in structure.tops:
            item = version.items.get(code)
            if item is None or not item.kind:
                codes.add(code)
        return sorted(codes)

    def _products(self, version: Version, params: dict[str, str]) -> tuple[_Structure, str | None, list[str]]:
        structure = _Structure(version)
        kind = self.product_kind(version, structure, (params.get("kind") or "").strip())
        codes = self.product_codes(version, structure, kind)
        needle = (params.get("q") or "").strip()
        if needle:
            codes = [c for c in codes if _matches((c, self._name(version, c)), needle)]
        return structure, kind, codes

    def _trial_only(self, version: Version, params: dict[str, str], codes: list[str]) -> list[str]:
        """trial を付けると、検討名の試算がある製品だけに絞る。"""
        if not _flag(params.get("trial")):
            return codes
        name = (params.get("name") or "").strip()
        if not name:
            raise ApiError(400, "検討名（name）が空")
        found = self._scenarios(version, name)
        return [c for c in codes if c in found]

    @staticmethod
    def _name(version: Version, code: str) -> str:
        item = version.items.get(code)
        return "" if item is None else item.name

    @staticmethod
    def _kind(version: Version, code: str) -> str:
        item = version.items.get(code)
        return "" if item is None else item.kind

    def _version(self, params: dict[str, Any], key: str = "version") -> Version:
        version_id = str(params.get(key) or "").strip()
        if not version_id:
            raise ApiError(400, f"版（{key}）が空")
        return self.api._load_version(version_id)

    def _scenarios(self, version: Version, name: str) -> dict[str, int]:
        """検討名と版がそろう試算を、土台の製品 → 試算の ID で（同じ組が何本かあれば最初の1本）。"""
        found: dict[str, int] = {}
        for s in self.store.list_scenarios():
            if s.name == name and s.version_id == version.version_id:
                found.setdefault(s.base, s.scenario_id)
        return found

    def products(self, params: dict[str, str]) -> Response:
        """版の製品の一覧。name（検討名）があれば製品ごとに 今・後・差・変更の件数 を添える。"""
        version = self._version(params)
        structure, kind, codes = self._products(version, params)
        codes = self._trial_only(version, params, codes)
        limit = _limit(params.get("limit"), DEFAULT_PRODUCT_LIMIT)
        shown = codes[:limit]
        name = (params.get("name") or "").strip()
        head = _head(params.get("head"), CostHead.MATERIAL)
        rows = [
            {"code": c, "name": self._name(version, c),
             "specification": version.items[c].specification if c in version.items else "", "has_structure": c in structure.children}
            for c in shown
        ]
        totals: dict[str, Any] = {}
        if name and shown:
            costs = self._costs(version, name, head, shown)
            for row in rows:
                row.update(costs[row["code"]])
            known = [Decimal(r["difference"]) for r in rows if r["difference"] is not None]
            # 表に出した製品の差の合計（差が未登録の製品は除き、その数を添える）
            totals = {"difference": _s(sum(known, Decimal(0))) if known or len(known) == len(rows) else None,
                      "missing_count": len(rows) - len(known)}
        return _json_response(
            {
                "version_id": version.version_id,
                "kind": kind,
                "name": name,
                "head": head.name,
                "head_label": head.value,
                "count": len(codes),
                "limit": limit,
                "truncated": len(codes) > limit,
                "products": rows,
                **totals,
            }
        )

    def _costs(self, version: Version, name: str, head: CostHead, codes: list[str]) -> dict[str, dict[str, Any]]:
        policy = self.store.load_scope_policy()
        # 土台の製品の今の原価は、一括モードの見方（未登録の扱いも同じ）で1つの計算を使い回す
        bulk = _View(Scenario(name, codes[0], f"{codes[0]}#", version, head, policy), "bulk", head, False)
        scenario_ids = self._scenarios(version, name)
        result: dict[str, dict[str, Any]] = {}
        for code in codes:
            current = bulk.cost(code)
            after, count, sid = current, 0, scenario_ids.get(code)
            if sid is not None:
                scenario = self.store.load_scenario(sid, version)
                count = len(scenario.history)
                after = _View(scenario, "diff", head, False).total
            difference = None if current is None or after is None else after - current
            result[code] = {
                "scenario_id": sid,
                "current": _s(current),
                "after": _s(after),
                "difference": _s(difference),
                "change_count": count,
            }
        return result

    def tree(self, params: dict[str, str]) -> Response:
        """製品の構成の code（省けば根）の行と直下の子の行。行の形は /api/scenarios/<id>/tree と同じ。

        検討名にその製品の試算があれば試算の構成（差分の見方・役割の印つき）、無ければ版の構成（一括の見方）。
        by_balloon を付けると版の構成の行を風船番号ごとに分ける。
        """
        version = self._version(params)
        product = to_code(_text_field(params, "product", "製品の品番"))
        _require_known(version, product, "製品")
        head = _head(params.get("head"), CostHead.MATERIAL)
        name = (params.get("name") or "").strip()
        by_balloon = _flag(params.get("by_balloon"))
        sid = self._scenarios(version, name).get(product) if name else None
        if sid is not None:
            view = _View(self.store.load_scenario(sid, version), "diff", head, by_balloon)
        else:
            scenario = Scenario(name, product, f"{product}#", version, head, self.store.load_scope_policy())
            view = _View(scenario, "bulk", head, by_balloon)
        return _json_response({"scenario_id": sid, **self.api.tree_payload(view, params.get("code"))})

    def heads(self, params: dict[str, str]) -> Response:
        """製品の8費目を現行（版の構成）と変更後（検討名の試算・無ければ現行と同じ）で。editable は費目変更で書き換えられる費目。"""
        version = self._version(params)
        product = to_code(_text_field(params, "product", "製品の品番"))
        _require_known(version, product, "製品")
        name = (params.get("name") or "").strip()
        sid = self._scenarios(version, name).get(product) if name else None
        current, after = self._head_values(version, name, product, sid, COMPARE_HEADS)
        return _json_response({
            "product": product, "scenario_id": sid,
            "heads": [{"id": h.name, "label": h.value} for h in COMPARE_HEADS],
            "editable": [h.name for h in EDITABLE_HEADS],
            "current": {h.name: _s(current.value(h)) for h in COMPARE_HEADS},
            "after": {h.name: _s(after.value(h)) for h in COMPARE_HEADS},
            "incomplete": sorted(h.name for h in current.incomplete | after.incomplete),
        })

    def _head_values(self, version: Version, name: str, product: str, sid: int | None,
                     heads: tuple[Head, ...]) -> tuple[Any, Any]:
        """製品の費目の内訳を現行（版の構成）と変更後（試算 sid・無ければ現行と同じ）で。"""
        scenario = Scenario(name, product, f"{product}#", version, CostHead.MATERIAL, self.store.load_scope_policy())
        current = _View(scenario, "bulk", CostHead.MATERIAL, False).breakdown(product)
        if sid is None:
            return current, current
        view = _View(self.store.load_scenario(sid, version), "diff", CostHead.MATERIAL, False)
        return current, view.breakdown(view.root)

    def trial_heads(self, params: dict[str, str]) -> Response:
        """検討名の試算がある製品ごとに、費目変更で書き換えられる費目（EDITABLE_HEADS）の現行・変更後。"""
        version = self._version(params)
        name = _text_field(params, "name", "検討名")
        sids = self._scenarios(version, name)
        rows = []
        for product in sorted(sids):
            current, after = self._head_values(version, name, product, sids[product], EDITABLE_HEADS)
            rows.append({"product": product, "product_name": self._name(version, product), "scenario_id": sids[product],
                         "current": {h.name: _s(current.value(h)) for h in EDITABLE_HEADS},
                         "after": {h.name: _s(after.value(h)) for h in EDITABLE_HEADS}})
        return _json_response({"version_id": version.version_id, "name": name,
                               "heads": [{"id": h.name, "label": h.value} for h in EDITABLE_HEADS], "rows": rows})

    def set_heads(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """検討名で、製品ごとの費目（items: [{product, values: {費目: 値}}]）をまとめて費目変更にする。

        空の値は飛ばす。製品ごとに1回の操作（batch）で、取り消しも製品ごと。全部が通ってから保存し、1つでも通らなければ何も残さない。
        """
        name = _text_field(body, "name", "検討名")
        version = self._version(body, "version_id")
        head = _head(body.get("head"), CostHead.MATERIAL)
        items = body.get("items")
        if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
            raise ApiError(400, "変える所（items）は製品・費目の値の組の配列にする")
        existing = self._scenarios(version, name)
        touched: dict[str, Scenario] = {}
        recorded_at = datetime.now()
        count = 0
        for item in items:
            product = to_code(_text_field(item, "product", "製品の品番"))
            _require_known(version, product, "製品")
            if product in touched:
                raise ApiError(400, f"製品 {product} が2回ある")
            values = item.get("values")
            if not isinstance(values, dict):
                raise ApiError(400, f"{product} の費目の値（values）は 費目 → 値 のオブジェクトにする")
            given = {k: v for k, v in values.items() if str(v if v is not None else "").strip() != ""}
            if not given:
                continue
            sid = existing.get(product)
            scenario = self._new_scenario(version, name, product, head) if sid is None else self.store.load_scenario(sid, version)
            changes = [self.api.head_change(scenario, {"target_head": k, "part": product, "quantity": str(v).strip()})
                       for k, v in given.items()]
            scenario.apply_batch(changes, recorded_at, scenario_annual(scenario))
            touched[product] = scenario
            count += len(changes)
        if not count:
            raise ApiError(400, "変える値が無い")
        self.store.save_scenarios(touched.values())
        return _json_response({"version_id": version.version_id, "name": name, "count": count,
                               "products": sorted(touched)})

    # 部品の検索 --------------------------------------------------------------

    def parts(self, params: dict[str, str]) -> Response:
        """製品の構成のどこかに入っている、品番か名前に part を含む部品を、製品×部品の行で返す。"""
        version = self._version(params)
        needle = (params.get("part") or "").strip()
        if not needle:
            raise ApiError(400, "部品の名前・品番の一部（part）が空")
        structure, _, codes = self._products(version, params)
        codes = self._trial_only(version, params, codes)
        limit = _limit(params.get("limit"), DEFAULT_ROW_LIMIT)
        name = (params.get("name") or "").strip()
        scenario_ids = self._scenarios(version, name) if name else {}
        rows: list[dict[str, Any]] = []
        truncated = False
        for product in codes:
            changed: dict[str, list[str]] = {}
            if product in scenario_ids:
                scenario = self.store.load_scenario(scenario_ids[product], version)
                for change in scenario.changes:
                    if change.kind is ChangeKind.REPLACE and change.replacement is not None:
                        changed.setdefault(change.part, []).append(change.replacement)
            hits = [(p, u) for p, u in structure.usage(product).items()
                    if p != product and _matches((p, self._name(version, p)), needle)]
            # 品番が語で始まる部品を上に（中ほどの一致も消さない）
            hits.sort(key=lambda h: not _code_starts(h[0], needle))
            for part, usage in hits:
                if len(rows) >= limit:
                    truncated = True
                    break
                rows.append(
                    {
                        "product": product,
                        "product_name": self._name(version, product),
                        "part": part,
                        "part_name": self._name(version, part),
                        "part_specification": version.items[part].specification if part in version.items else "",
                        "usage": _s(usage),
                        "replaced_by": changed.get(part, []),
                        "scenario_id": scenario_ids.get(product),
                    }
                )
            if truncated:
                break
        return _json_response(
            {"version_id": version.version_id, "part": needle, "limit": limit, "truncated": truncated, "rows": rows}
        )

    def parts_by_code(self, params: dict[str, str]) -> Response:
        """部品を品番ごとに集計する。場所別の表示上限は適用しない。"""
        version = self._version(params)
        needle = _text_field(params, "part", "部品")
        structure, _, products = self._products(version, params)
        products = self._trial_only(version, params, products)
        scenarios = self._scenarios(version, (params.get("name") or "").strip())
        grouped: dict[str, dict[str, Any]] = {}
        for product in products:
            replacements: dict[str, set[str]] = {}
            if product in scenarios:
                for change in self.store.load_scenario(scenarios[product], version).changes:
                    if change.kind is ChangeKind.REPLACE and change.replacement:
                        replacements.setdefault(change.part, set()).add(change.replacement)
            for part in structure.usage(product):
                if part == product or not _matches((part, self._name(version, part)), needle):
                    continue
                row = grouped.setdefault(part, {
                    "part": part, "part_name": self._name(version, part),
                    "part_specification": version.items[part].specification if part in version.items else "",
                    "product_count": 0, "place_count": 0, "replaced_by": [],
                })
                row["product_count"] += 1
                row["place_count"] += len(_places(version, structure, product, part))
                row["replaced_by"] = sorted(set(row["replaced_by"]) | replacements.get(part, set()))
        rows = sorted(grouped.values(), key=lambda r: (not _code_starts(r["part"], needle), r["part"]))
        limit = _limit(params.get("limit"), DEFAULT_ROW_LIMIT)
        return _json_response({"version_id": version.version_id, "part": needle,
                               "rows": rows[:limit], "count": len(rows), "truncated": len(rows) > limit})

    def replace_code(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """対象製品内の指定品番の全場所を、既存の一括置換で検証して保存する。"""
        version = self._version(body, "version_id")
        part = to_code(_text_field(body, "part", "部品"))
        _require_known(version, part, "部品")
        scope = {k: str(body[k]) for k in ("name", "trial", "q", "kind") if k in body}
        structure, _, products = self._products(version, scope)
        products = self._trial_only(version, scope, products)
        items = [{"product": product, "part": part} for product in products
                 if product != part and part in structure.usage(product)]
        return self.add_changes(params, {**body, "items": items})

    def names(self, params: dict[str, str]) -> Response:
        """版を使う試算の検討名（重複なし・作った順）。"""
        version_id = (params.get("version") or "").strip()
        names = [s.name for s in self.store.list_scenarios() if not version_id or s.version_id == version_id]
        return _json_response(list(dict.fromkeys(names)))

    def trials(self, params: dict[str, str]) -> Response:
        """試作キー（検討名）と版の組ごとの、試算がある製品の数（作った順）。version を付けるとその版だけ。"""
        version_id = (params.get("version") or "").strip()
        bases: dict[tuple[str, str], set[str]] = {}
        for s in self.store.list_scenarios():
            if not version_id or s.version_id == version_id:
                bases.setdefault((s.name, s.version_id), set()).add(s.base)
        return _json_response([{"name": n, "version_id": v, "count": len(b)} for (n, v), b in bases.items()])

    def delete_trial(self, params: dict[str, str]) -> Response:
        """版のその検討名の試算（製品ごと）と変更の記録を全部消す。無ければ 404。"""
        version_id = (params.get("version") or "").strip()
        name = (params.get("name") or "").strip()
        if not version_id or not name:
            raise ApiError(400, "版（version）と検討名（name）を付ける")
        deleted = self.store.delete_trial(version_id, name)
        if not deleted:
            raise ApiError(404, f"検討案 {name} は無い")
        return _json_response({"version_id": version_id, "name": name, "deleted": deleted})

    # 替える・足す ------------------------------------------------------------

    def add_change(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """検討名・製品の試算に変更を1件足す。試算が無ければ作り、変更が通らなければ作った試算を消す。"""
        name = _text_field(body, "name", "検討名")
        product = to_code(_text_field(body, "product", "製品の品番"))
        version = self._version(body, "version_id")
        _require_known(version, product, "製品")
        head = _head(body.get("head"), CostHead.MATERIAL)
        sid = self._scenarios(version, name).get(product)
        created = sid is None
        if created:
            sid = self.store.save_scenario(self._new_scenario(version, name, product, head))
        change = {k: body[k] for k in ("kind", "part", "replacement", "quantity", "parent", "balloon", "replacements",
                                       "target_head") if k in body}
        try:
            response = self.api.add_change(sid, {}, change)
        except Exception:
            if created:
                self.store.delete_scenario(sid)
            raise
        return _json_response(
            {"scenario_id": sid, "created": created, "product": product, "summary": response.json()}
        )

    def add_changes(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """検討名で、製品×部品の組（items）の全部を同じ代わりの部品（replacement・quantity）に置換する。

        1つの組は、製品の構成でその部品が入る場所（親・風船番号）ごとに1回の場所の置換（POST changes と同じ記録）。
        quantity が空なら場所ごとに版の員数のまま。全部が通ってから保存し、1つでも通らなければ何も残さない。
        """
        name = _text_field(body, "name", "検討名")
        version = self._version(body, "version_id")
        head = _head(body.get("head"), CostHead.MATERIAL)
        replacement = _text_field(body, "replacement", "代わりの品番")
        quantity = str(body.get("quantity") or "").strip()
        items = body.get("items")
        if not isinstance(items, list) or not items or not all(isinstance(x, dict) for x in items):
            raise ApiError(400, "置換する所（items）は製品・部品の組の配列にする")
        structure = _Structure(version)
        existing = self._scenarios(version, name)
        touched: dict[str, Scenario] = {}
        count = 0
        for item in items:
            product = to_code(_text_field(item, "product", "製品の品番"))
            part = to_code(_text_field(item, "part", "部品の品番"))
            _require_known(version, product, "製品")
            scenario = touched.get(product)
            if scenario is None:
                sid = existing.get(product)
                scenario = (self._new_scenario(version, name, product, head) if sid is None
                            else self.store.load_scenario(sid, version))
                touched[product] = scenario
            places = _places(version, structure, product, part)
            if not places:
                raise ApiError(400, f"{part} は {product} の構成に無い")
            for parent, balloon, place_qty in places:
                self.api.apply_change(scenario, {
                    "kind": "REPLACE", "part": part, "parent": parent, "balloon": balloon,
                    "replacements": [{"code": replacement, "quantity": quantity or _s(place_qty)}],
                })
                count += 1
        self.store.save_scenarios(touched.values())
        return _json_response({"version_id": version.version_id, "name": name, "count": count,
                               "products": sorted(touched)})

    def undo_change(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """試算（scenario_id）の、sequence で始まる1回の操作を取り消す。最後のものでなくてもよい。"""
        sid = _int_field(body, "scenario_id", "試算")
        sequence = _int_field(body, "sequence", "変更の番号")
        scenario = self.api._load_scenario(sid)
        removed = scenario.remove_batch(sequence)
        if not removed:
            raise ApiError(400, f"取り消す変更が無い: {sequence}")
        self.store.save_scenario(scenario)
        return _json_response({"scenario_id": sid, "removed": len(removed), "change_count": len(scenario.history)})

    def add_scenarios(self, params: dict[str, str], body: dict[str, Any]) -> Response:
        """検討名で、一覧（q・kind で絞った後・件数の上限も一覧と同じ）の製品ごとに試算をまとめて作る。既にあれば作らない。"""
        name = _text_field(body, "name", "検討名")
        version = self._version(body, "version_id")
        head = _head(body.get("head"), CostHead.MATERIAL)
        params = {k: str(body[k]) for k in ("q", "kind") if body.get(k) not in (None, "")}
        _, _, codes = self._products(version, params)
        limit = _limit(body.get("limit"), DEFAULT_PRODUCT_LIMIT)
        existing = self._scenarios(version, name)
        # 全部の計算が通ってから保存する（途中の製品で循環などが見つかったら1つも作らない）
        new = [self._new_scenario(version, name, c, head) for c in codes[:limit] if c not in existing]
        for scenario in new:
            self.store.save_scenario(scenario)
        return _json_response(
            {
                "version_id": version.version_id,
                "name": name,
                "created": len(new),
                "existing": sum(1 for c in codes[:limit] if c in existing),
                "truncated": len(codes) > limit,
            }
        )

    def _new_scenario(self, version: Version, name: str, product: str, head: CostHead) -> Scenario:
        scenario = Scenario(name, product, self._trial_code(version, name, product), version, head,
                            self.store.load_scope_policy())
        scenario.calculate()  # 構成の循環などをここで弾く
        return scenario

    @staticmethod
    def _trial_code(version: Version, name: str, product: str) -> str:
        """試算の製品（仮の品目）の品番。版の品番とぶつからないものにする。"""
        known = _known_codes(version)
        code = f"{product}#{name}"
        n = 2
        while code in known:
            code = f"{product}#{name}#{n}"
            n += 1
        return code


# 内訳のまとめ方 → 品目からまとめの鍵を作る関数。名称が空の品目は品番でまとめる（名前の無い物どうしを1行にしない）
BREAKDOWN_KEYS: dict[str, Callable[[str, str, str], str]] = {
    "code": lambda code, name, spec: code,
    "name": lambda code, name, spec: name or f"\0{code}",
    "name_spec": lambda code, name, spec: f"{name}\0{spec}" if name else f"\0{code}",
}

# 製品の直の子 → 員数と、材料費を足す品番ごとの数量（ふつうは子そのもの。組立品の中の変更はその組立品の行に足す）
_Parts = dict[str, dict[str, Any]]


def _base_parts(structure: _Structure, product: str) -> _Parts:
    return {line.child: {"quantity": line.quantity, "terms": {line.child: line.quantity}}
            for line in structure.children.get(product, ())}


def _trial_parts(structure: _Structure, scenario: Scenario) -> _Parts:
    """検討案の変更を当てた直の子（Scenario.root_entries と同じ当て方）。組立品の中の変更は、その場所の親を含む直の子へ
    使用数の割合で分けて足す。製品の直下の変更と場所の無い変更は直の子の行そのものを増減する。"""
    product = scenario.base
    parts: _Parts = {}

    def add(child: str, quantity: Decimal, code: str, amount: Decimal) -> None:
        row = parts.setdefault(child, {"quantity": Decimal(0), "terms": {}})
        row["quantity"] += quantity
        row["terms"][code] = row["terms"].get(code, Decimal(0)) + amount

    kids = structure.children.get(product, ())
    for code, qty, is_base, parent in scenario.root_entries():
        if is_base:
            for line in kids:
                add(line.child, qty * line.quantity, line.child, qty * line.quantity)
            continue
        shares = {}
        if parent is not None and parent != product:
            shares = {line.child: line.quantity * (Decimal(1) if line.child == parent
                                                   else structure.usage(line.child).get(parent, Decimal(0)))
                      for line in kids}
        total = sum(shares.values(), Decimal(0))
        if not total:
            add(code, qty, code, qty)
            continue
        for child, share in shares.items():
            if share:
                add(child, Decimal(0), code, qty * share / total)
    return {c: r for c, r in parts.items() if any(r["terms"].values())}


def _breakdown_series(name: str | None, view: _View, version: Version, by: str, weights: dict[str, Decimal],
                      parts: dict[str, _Parts], without_trial: int) -> dict[str, Any]:
    """製品ごとの直の子を重み（weights）を掛けてまとめ方 by の行に足し、金額の高い順に割合と累積の割合を付ける。"""
    key_of = BREAKDOWN_KEYS[by]
    groups: dict[str, dict[str, Any]] = {}
    for product, w in weights.items():
        for child, row in parts[product].items():
            item = version.items.get(child)
            child_name, spec = ("", "") if item is None else (item.name, item.specification)
            key = key_of(child, child_name, spec)
            g = groups.setdefault(key, {
                "kind": "child", "key": key, "code": child, "codes": [], "name": child_name, "specification": spec,
                "quantity": Decimal(0), "amount": None, "missing": 0, "incomplete": False})
            if child not in g["codes"]:
                g["codes"].append(child)
            g["quantity"] += w * row["quantity"]
            known, found, missing = Decimal(0), False, False
            for code, qty in row["terms"].items():
                if not qty:
                    continue
                cost = view.cost(code, CostHead.MATERIAL)
                if cost is None:
                    missing = True
                    continue
                known, found = known + qty * cost, True
                g["incomplete"] |= view.incomplete(code, CostHead.MATERIAL)
            g["missing"] += missing
            if found:
                g["amount"] = (g["amount"] or Decimal(0)) + w * known
    rows = sorted(groups.values(), key=lambda g: (g["amount"] is None, -(g["amount"] or 0), g["code"]))
    total = sum((g["amount"] for g in rows if g["amount"] is not None), Decimal(0))
    found = any(g["amount"] is not None for g in rows)
    running = Decimal(0)
    for g in rows:
        g["codes"].sort()
        if g["amount"] is not None:
            running += g["amount"]
        g["share"] = None if g["amount"] is None or not total else _share(g["amount"], total)
        g["cumulative"] = None if g["amount"] is None or not total else _share(running, total)
        g["quantity"], g["amount"] = _s(g["quantity"]), _s(g["amount"])
    return {"name": name, "total": _s(total) if found else None, "missing": sum(1 for g in rows if g["missing"]),
            "without_trial": without_trial, "rows": rows}


def _share(part: Decimal, total: Decimal) -> str:
    return _s((part / total * 100).quantize(Decimal("0.1"), ROUND_HALF_UP))


def _differences(base: dict[str, str | None], after: dict[str, str | None]) -> dict[str, str | None]:
    """費目ごとの 新 − 元。どちらかが未登録なら None。"""
    return {
        head: None if base[head] is None or after[head] is None else _s(Decimal(after[head]) - Decimal(base[head]))
        for head in base
    }


def _percent(difference: str | None, current: str | None) -> str | None:
    """差 ÷ 現行 ×100 を小数1桁（四捨五入）で。どちらかが未登録か現行が 0 なら None。"""
    if difference is None or current is None or Decimal(current) == 0:
        return None
    return _s((Decimal(difference) / Decimal(current) * 100).quantize(Decimal("0.1"), ROUND_HALF_UP))


def _table_totals(name: str, products: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """検討名の試算がある製品の、費目ごとの 現行・変更後・差 の合計と差%。片方でも未登録の製品は除いて数える。"""
    totals: dict[str, dict[str, Any]] = {}
    for head in COMPARE_HEADS:
        pairs = [(p["current"][head.name], t["after"][head.name])
                 for p in products for t in p["trials"] if t["name"] == name]
        known = [(Decimal(c), Decimal(a)) for c, a in pairs if c is not None and a is not None]
        current = sum((c for c, _ in known), Decimal(0))
        after = sum((a for _, a in known), Decimal(0))
        found = bool(known)
        difference = _s(after - current) if found else None
        totals[head.name] = {
            "current": _s(current) if found else None,
            "after": _s(after) if found else None,
            "difference": difference,
            "percent": _percent(difference, _s(current) if found else None),
            "excluded": len(pairs) - len(known),
        }
    return totals


def _effect(difference: str | None, volume: str | None) -> str | None:
    """年間効果額＝差 × 年間台数。どちらかが未登録なら None。"""
    return None if difference is None or volume is None else _s(Decimal(difference) * Decimal(volume))


def _effect_totals(name: str, products: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """検討名の費目ごとの年間効果額の合計。年間台数か差が未登録の製品は除いて数える（除いた数を excluded に）。"""
    totals: dict[str, dict[str, Any]] = {}
    for head in COMPARE_HEADS:
        values = [t["effect"][head.name] for p in products for t in p["trials"] if t["name"] == name]
        known = [Decimal(v) for v in values if v is not None]
        totals[head.name] = {"effect": _s(sum(known, Decimal(0))) if known else None,
                             "excluded": len(values) - len(known)}
    return totals


def _compare_summary(names: list[str], rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """検討名 → 費目 → 合計の差（未登録の製品は除く）と、下がった・上がった・変わらない・未登録の製品の数。"""
    summary: dict[str, dict[str, dict[str, Any]]] = {}
    for name in names:
        trials = [r for r in rows if r["kind"] == "trial" and r["name"] == name]
        by_head: dict[str, dict[str, Any]] = {}
        for head in COMPARE_HEADS:
            values = [r["differences"][head.name] for r in trials]
            known = [Decimal(v) for v in values if v is not None]
            by_head[head.name] = {
                "difference": _s(sum(known, Decimal(0))),
                "down": sum(1 for v in known if v < 0),
                "up": sum(1 for v in known if v > 0),
                "same": sum(1 for v in known if v == 0),
                "missing": len(values) - len(known),
            }
        summary[name] = {"products": len(trials), "heads": by_head}
    return summary


def _batches(history: tuple[ChangeRecord, ...]) -> list[list[ChangeRecord]]:
    """履歴を1回の操作ごとに束ねる（batch の無い行は1行で1回）。"""
    groups: list[list[ChangeRecord]] = []
    for record in history:
        if groups and record.batch is not None and groups[-1][0].batch == record.batch:
            groups[-1].append(record)
        else:
            groups.append([record])
    return groups


def replayed_batches(scenario: Scenario, head: CostHead | None = None) -> list[list[ChangeRecord]]:
    """履歴の操作を、試算の今の版・計上範囲の上で記録のときと同じ apply_batch で当て直した行（1回の操作ごと）。

    原価は今の登録値で、費目は head（省けば試算の費目）。試算そのものと保存した記録は変えない。
    """
    head = head or scenario.head
    fresh = Scenario(scenario.name, scenario.base, scenario.product_code, scenario.version, head,
                     scenario.policy, scenario.note)
    batches = [
        list(fresh.apply_batch([r.change for r in records], records[0].recorded_at, records[0].volume,
                               records[0].allocation))
        for records in _batches(scenario.history)
    ]
    # 前・後の原価は製品の表と同じ見方で出す（足し上げる範囲に登録値が1つも無ければ 0 でなく未登録）
    records = fresh.history
    totals = [
        _View(Scenario(fresh.name, fresh.base, fresh.product_code, fresh.version, head, fresh.policy,
                       history=records[:k]), "diff", head, False).total
        for k in range(len(records) + 1)
    ]
    by_sequence = {r.sequence: i for i, r in enumerate(records)}
    return [
        [ChangeRecord(**{**r.__dict__, "cost_before": totals[by_sequence[r.sequence]],
                         "cost_after": totals[by_sequence[r.sequence] + 1]}) for r in batch]
        for batch in batches
    ]


def _places(version: Version, structure: _Structure, product: str, part: str) -> list[tuple[str, str | None, Decimal]]:
    """製品の構成で part が入る場所（親・風船番号・員数）。製品の中の組立品の下も含む。"""
    places = []
    for parent in dict.fromkeys([product, *structure.usage(product)]):
        places.extend((parent, line.balloon, line.quantity)
                      for line in version.children(parent, by_balloon=True) if line.child == part)
    return places


def _int_field(body: dict[str, Any], key: str, label: str) -> int:
    text = str(body.get(key) if body.get(key) is not None else "").strip()
    if not text.isdigit():
        raise ApiError(400, f"{label}（{key}）は整数にする: {text}")
    return int(text)


def _batch_json(records: list[ChangeRecord]) -> dict[str, Any]:
    """1回の操作を一覧の1行に。代わりの部品は replacements に並べ、原価は最初の行の前 → 最後の行の後。"""
    row = _record_json(records[0])
    row["replacements"] = [{"code": r.change.replacement, "quantity": _s(r.change.quantity)}
                           for r in records if r.change.replacement is not None]
    # 費目変更の操作で書き換えた費目と値（まとめて変えた時は何費目か）
    row["heads"] = [{"id": r.change.target_head, "label": Head[r.change.target_head].value, "value": _s(r.change.quantity)}
                    for r in records if r.change.target_head is not None]
    if len(records) > 1:
        row["text"] = ("費目変更 " + "・".join(f"{x['label']} → {x['value']}" for x in row["heads"]) if row["heads"]
                       else f"{_place(records[0].change)} → " + "・".join(
                           f"{x['code']} ×{x['quantity']}" for x in row["replacements"]))
        last = records[-1]
        missing = any(r.difference is None for r in records)
        row["cost_after"] = _s(last.cost_after)
        row["difference"] = None if missing else _s(sum((r.difference for r in records), Decimal(0)))
    return row


def group_routes(api: "Api") -> list[tuple[str, str, Callable[..., Response]]]:
    return Group(api).routes()
