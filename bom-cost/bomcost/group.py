"""製品群の API: 版の製品を並べ、部品の名前で絞った製品×部品の行を、製品ごとの試算で替える・足す。

製品ごとの試算は今の試算（Scenario）をそのまま使い、検討名・土台の製品・版の組で引く（保存の形は変えない）。
変更は今の試算の変更（Api.add_change）に渡し、取り消しは今の undo を使う。
"""

import json
from collections import Counter
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from .api import ApiError, Response, _flag, _head, _json_response, _known_codes, _place, _require_known, _record_json, _s, _text_field, _View
from .model import BomLine, CostHead, Version
from .normalize import to_code
from .scenario import ChangeKind, ChangeRecord, Scenario

if TYPE_CHECKING:
    from .api import Api

DEFAULT_PRODUCT_LIMIT = 500
DEFAULT_ROW_LIMIT = 2000
MAX_LIMIT = 20000
PRODUCT_KIND_TEXT = "製品"
COMPARE_HEADS: tuple[CostHead, ...] = (
    CostHead.MATERIAL,
    CostHead.AMORTIZATION,
    CostHead.ADJUSTMENT,
    CostHead.DIRECT,
    CostHead.INDIRECT,
    CostHead.MANUFACTURING,
)


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


def _matches(text: str, needle: str) -> bool:
    return needle.casefold() in text.casefold()


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
            ("GET", r"/api/group/names", self.names),
            ("GET", r"/api/group/trials", self.trials),
            ("GET", r"/api/group/changes", self.changes),
            ("POST", r"/api/group/changes", self.add_change),
            ("GET", r"/api/group/tree", self.tree),
            ("POST", r"/api/group/scenarios", self.add_scenarios),
            ("GET", r"/api/group/compare", self.compare),
        ]

    # 比べる ------------------------------------------------------------------

    def compare(self, params: dict[str, str]) -> Response:
        """製品ごとに元（版の構成）の行と、試算がある検討名ごとの新（土台＋変更）の行を、6費目で並べる。

        償却費が出せない（足し上げる範囲に登録値が1つも無い）行があれば 400。
        ignore_amortization を付けると償却費は null で通す。incomplete は範囲のどこかに未登録がある費目。
        """
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
                         "bulk", CostHead.MATERIAL, False, amortization=True)
            scenario_ids = {n: self._scenarios(version, n) for n in names}
            for code in shown:
                product_name = self._name(version, code)
                rows.append(self._compare_row(bulk, code, code, product_name, None, None, ignore, failed))
                for n in names:
                    sid = scenario_ids[n].get(code)
                    if sid is None:
                        continue
                    view = _View(self.store.load_scenario(sid, version), "diff", CostHead.MATERIAL, False,
                                 amortization=True)
                    rows.append(self._compare_row(view, view.root, code, product_name, n, sid, ignore, failed))
        if failed:
            raise ApiError(400, "償却費を出せない（未登録あり）: " + "、".join(failed))
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
            }
        )

    @staticmethod
    def _compare_row(view: _View, root: str, code: str, product_name: str, name: str | None, sid: int | None,
                     ignore: bool, failed: list[str]) -> dict[str, Any]:
        costs: dict[str, str | None] = {}
        incomplete: list[str] = []
        for head in COMPARE_HEADS:
            if head is CostHead.AMORTIZATION and ignore:
                costs[head.name] = None
                continue
            value = view.cost(root, head)
            if view.incomplete(root, head):
                incomplete.append(head.name)
            if value is None and head is CostHead.AMORTIZATION:
                failed.append(code if name is None else f"{code}（{name}）")
            costs[head.name] = _s(value)
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
        """検討名の全製品の変更を1表に。差は履歴に残した変更前・変更後の原価から出し、合計の差も返す。"""
        version = self._version(params)
        name = (params.get("name") or "").strip()
        if not name:
            raise ApiError(400, "検討名（name）が空")
        rows: list[dict[str, Any]] = []
        total = Decimal(0)
        missing = 0
        for product, sid in sorted(self._scenarios(version, name).items()):
            scenario = self.store.load_scenario(sid, version)
            for records in _batches(scenario.history):
                row = _batch_json(records)
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
        """製品とみなす区分の記号。指定が無ければ「製品」、それも無ければ構成の一番上の親に多い記号。"""
        if requested:
            return requested
        kinds = {item.kind for item in version.items.values()}
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
            codes = [c for c in codes if _matches(c, needle) or _matches(self._name(version, c), needle)]
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
            {"code": c, "name": self._name(version, c), "has_structure": c in structure.children}
            for c in shown
        ]
        if name and shown:
            costs = self._costs(version, name, head, shown)
            for row in rows:
                row.update(costs[row["code"]])
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
            for part, usage in structure.usage(product).items():
                if part == product or not (_matches(part, needle) or _matches(self._name(version, part), needle)):
                    continue
                if len(rows) >= limit:
                    truncated = True
                    break
                rows.append(
                    {
                        "product": product,
                        "product_name": self._name(version, product),
                        "part": part,
                        "part_name": self._name(version, part),
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
        change = {k: body[k] for k in ("kind", "part", "replacement", "quantity", "parent", "balloon", "replacements")
                  if k in body}
        try:
            response = self.api.add_change(sid, {}, change)
        except Exception:
            if created:
                self.store.delete_scenario(sid)
            raise
        return _json_response(
            {"scenario_id": sid, "created": created, "product": product, "summary": response.json()}
        )

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


def _batches(history: tuple[ChangeRecord, ...]) -> list[list[ChangeRecord]]:
    """履歴を1回の操作ごとに束ねる（batch の無い行は1行で1回）。"""
    groups: list[list[ChangeRecord]] = []
    for record in history:
        if groups and record.batch is not None and groups[-1][0].batch == record.batch:
            groups[-1].append(record)
        else:
            groups.append([record])
    return groups


def _batch_json(records: list[ChangeRecord]) -> dict[str, Any]:
    """1回の操作を一覧の1行に。代わりの部品は replacements に並べ、原価は最初の行の前 → 最後の行の後。"""
    row = _record_json(records[0])
    row["replacements"] = [{"code": r.change.replacement, "quantity": _s(r.change.quantity)}
                           for r in records if r.change.replacement is not None]
    if len(records) > 1:
        row["text"] = f"{_place(records[0].change)} → " + "・".join(
            f"{x['code']} ×{x['quantity']}" for x in row["replacements"])
        last = records[-1]
        missing = any(r.difference is None for r in records)
        row["cost_after"] = _s(last.cost_after)
        row["difference"] = None if missing else _s(sum((r.difference for r in records), Decimal(0)))
    return row


def group_routes(api: "Api") -> list[tuple[str, str, Callable[..., Response]]]:
    return Group(api).routes()
