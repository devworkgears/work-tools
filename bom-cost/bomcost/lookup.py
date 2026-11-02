"""品目照会の API: 版と品番1つで、その品の基本・原価（費目ごとの出どころ）・使われ方・構成・履歴をまとめて返す。

原価の重ね方は Store.overlay_master と同じ（費目ごとに BOM ＞ 開発番号の登録 ＞ 共通の登録）。
"""

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from .api import ApiError, Response, _change_text, _json_response, _known_codes, _s, _View
from .formula import Head
from .group import Group, _matches, _Structure
from .model import CostHead, Version
from .normalize import to_code
from .scenario import ChangeKind, Scenario

if TYPE_CHECKING:
    from .api import Api

MAX_CANDIDATES = 100
SOURCES = {"bom": "BOM", "dev": "開発番号の登録", "common": "共通の登録"}


class Lookup:
    """品目照会の画面の API の中身。api は Api（保存と版の読み込みを借りる）。"""

    def __init__(self, api: "Api") -> None:
        self.api = api
        self.store = api.store

    def routes(self) -> list[tuple[str, str, Callable[..., Response]]]:
        return [("GET", r"/api/lookup", self.lookup)]

    def lookup(self, params: dict[str, str]) -> Response:
        """q は品番か、品番・名称の一部（空白で区切った語の全部を含む）。

        品番がそのまま版にあればその品を返す。無ければ語で引き、1つならその品、何件も当たれば candidates だけを返す。
        """
        version_id = (params.get("version") or "").strip()
        if not version_id:
            raise ApiError(400, "版（version）が空")
        needle = (params.get("q") or "").strip()
        if not needle:
            raise ApiError(400, "品番・名称（q）が空")
        version = self.api._load_version(version_id)
        known = _known_codes(version)
        code = to_code(needle)
        if code not in known:
            hits = sorted(c for c in known if _matches((c, _name(version, c)), needle))
            if not hits:
                raise ApiError(404, f"「{needle}」に当たる品が版 {version_id} に無い")
            if len(hits) > 1:
                view = self._view(version, hits[0])
                return _json_response({
                    "version_id": version_id, "q": needle, "item": None,
                    "candidates": [{"code": c, "name": _name(version, c),
                                    "specification": _specification(version, c),
                                    "material": _s(view.cost(c, CostHead.MATERIAL)),
                                    "manufacturing": _s(view.cost(c, CostHead.MANUFACTURING))}
                                   for c in hits[:MAX_CANDIDATES]],
                    "truncated": len(hits) > MAX_CANDIDATES,
                })
            code = hits[0]
        brief = params.get("brief") == "1"
        item = self.item(version, code, brief=brief)
        if brief and params.get("summary") == "1":
            item["summary"] = self.summary(version, code, item["costs"])
        return _json_response({"version_id": version_id, "q": needle, "candidates": [], "truncated": False,
                               "item": item})

    def item(self, version: Version, code: str, brief: bool = False) -> dict[str, Any]:
        """照会の中身（基本・原価・使われ方・構成・履歴）。version はマスタ登録を敷いた版。"""
        raw = self.store.load_version(version.version_id, with_manual=False)
        layers = (("bom", raw), ("dev", self.store.load_manual(version.dev_number)), ("common", self.store.load_manual()))
        basics = {"code": code, "name": _name(version, code),
                  "specification": _specification(version, code), "costs": _costs(code, layers)}
        if brief:
            return basics
        structure = _Structure(version)
        item = version.items.get(code)
        item_source = next((key for key, layer in layers if code in layer.items), None)
        entries = self.store.bom_entries([code])
        product_kind = Group.product_kind(version, structure)
        return {
            **basics,
            # この品を計算の一番上にして式で出した値（costs の取込値と並べる）
            "calculated": self._calculated(version, code),
            "kind": "" if item is None else item.kind,
            "lifecycle": "" if item is None else item.lifecycle,
            "is_product": code in Group.product_codes(version, structure, product_kind),
            "in_bom": code in _known_codes(raw),
            "item_source": item_source,
            "item_source_label": None if item_source is None else SOURCES[item_source],
            "entries": [{"dev_number": d, "revision": r} for d, r in entries.get(code, [])],
            "used_in": _used_in(version, structure, code, product_kind),
            "structure": _tree(version, code),
            "revisions": self._revisions(version, code),
            "changes": self._changes(version, code),
        }

    def summary(self, version: Version, code: str, costs: list[dict[str, Any]]) -> dict[str, Any]:
        """簡易照会の枠の追加分: 計算した8費目・取り込んだ製造原価と計算値との差・直の親・使う製品の数・直の子の数・年間台数。

        枠は品番を押すたびに開くので、全製品を展開せず親を上へたどる（数は _used_in の製品の数と同じ）。
        年間台数は製品のときだけ（製品でなければ null）。costs は item() の costs。
        """
        structure = _Structure(version)
        products = set(Group.product_codes(version, structure, Group.product_kind(version, structure)))
        parents_of: dict[str, list[tuple[str, Decimal]]] = {}
        for parent, lines in structure.children.items():
            for line in lines:
                parents_of.setdefault(line.child, []).append((parent, line.quantity))
        calculated = self._calculated(version, code)
        computed = next(c["value"] for c in calculated if c["head"] == Head.MANUFACTURING.name)
        imported = next(c["value"] for c in costs if c["head"] == CostHead.MANUFACTURING.name)
        difference = None if computed is None or imported is None else _s(Decimal(computed) - Decimal(imported))
        is_product = code in products
        volume = version.volume(code) if is_product else None
        return {
            "calculated": calculated,
            "imported": {"value": imported, "difference": difference},  # difference は 計算値 − 取込値
            "parents": [{"code": p, "name": _name(version, p), "quantity": _s(q)}
                        for p, q in sorted(parents_of.get(code, ()))],
            "product_count": len((_ancestors(parents_of, code) & products) - {code}),
            "child_count": len(structure.children.get(code, ())),
            "is_product": is_product,
            "annual_volume": None if volume is None else _s(volume.annual),
        }

    def _calculated(self, version: Version, code: str) -> list[dict[str, Any]]:
        calculated = self._view(version, code).breakdown(code)
        return [{"head": h.name, "label": h.value, "value": _s(calculated.value(h)),
                 "incomplete": calculated.is_incomplete(h)} for h in Head]

    def _view(self, version: Version, code: str) -> _View:
        """版の構成のまま（一括の見方）で原価を出す見方。"""
        scenario = Scenario("", code, f"{code}#", version, CostHead.MATERIAL, self.store.load_scope_policy())
        return _View(scenario, "bulk", CostHead.MATERIAL, False)

    def _revisions(self, version: Version, code: str) -> list[dict[str, Any]]:
        """同じ開発番号の各版（版の番号の順）での、この品の原価（マスタ登録を敷いた値）。"""
        rows = []
        infos = sorted((v for v in self.store.list_versions() if v.dev_number == version.dev_number),
                       key=lambda v: v.revision)
        for info in infos:
            other = version if info.version_id == version.version_id else self.store.load_version(info.version_id)
            rows.append({
                "version_id": info.version_id,
                "revision": info.revision,
                "found": code in _known_codes(other),
                "costs": {h.name: _s(other.cost_value(code, h)) for h in CostHead},
            })
        return rows

    def _changes(self, version: Version, code: str) -> list[dict[str, Any]]:
        """同じ開発番号の版の試算で、この品を置換元・置換先・削除・追加・員数変更にしている変更（1回の操作に1行）。"""
        ids = {v.version_id for v in self.store.list_versions() if v.dev_number == version.dev_number}
        cache: dict[str, Version] = {version.version_id: version}
        rows: list[dict[str, Any]] = []
        seen: set[tuple[int, int, str]] = set()
        for info in self.store.list_scenarios():
            if info.version_id not in ids:
                continue
            if info.version_id not in cache:
                cache[info.version_id] = self.store.load_version(info.version_id)
            scenario = self.store.load_scenario(info.scenario_id, cache[info.version_id])
            for record in scenario.history:
                role = _role(record.change.kind, record.change.part, record.change.replacement, code)
                if role is None:
                    continue
                key = (info.scenario_id, record.batch or record.sequence, role)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "scenario_id": info.scenario_id,
                    "name": info.name,
                    "product": info.base,
                    "version_id": info.version_id,
                    "sequence": record.sequence,
                    "role": role,
                    "kind": record.change.kind.name,
                    "part": record.change.part,
                    "replacement": record.change.replacement,
                    "quantity": _s(record.change.quantity),
                    "text": _change_text(record.change),
                })
        return rows


def _specification(version: Version, code: str) -> str:
    item = version.items.get(code)
    return "" if item is None else item.specification


def _name(version: Version, code: str) -> str:
    item = version.items.get(code)
    return "" if item is None else item.name


def _role(kind: ChangeKind, part: str, replacement: str | None, code: str) -> str | None:
    if kind is ChangeKind.REPLACE:
        return "置換元" if part == code else ("置換先" if replacement == code else None)
    return kind.value if part == code else None


def _costs(code: str, layers: tuple[tuple[str, Version], ...]) -> list[dict[str, Any]]:
    """登録できる全費目（CostHead）の値と出どころ。費目ごとに、強い層（BOM ＞ 開発番号 ＞ 共通）から最初に値のある層を採る。"""
    rows = []
    for head in CostHead:
        value, source = None, None
        for key, layer in layers:
            found = layer.cost_value(code, head)
            if found is not None:
                value, source = found, key
                break
        rows.append({"head": head.name, "label": head.value, "value": _s(value), "source": source,
                     "source_label": "未登録" if source is None else SOURCES[source]})
    return rows


def _used_in(version: Version, structure: _Structure, code: str, product_kind: str | None) -> list[dict[str, Any]]:
    """この品を構成に含む製品と1台あたりの使用数、製品の中の直の親（親1つあたりの数）。"""
    rows = []
    for product in Group.product_codes(version, structure, product_kind):
        if product == code:
            continue
        usage = structure.usage(product)
        if code not in usage:
            continue
        parents = [
            {"code": parent, "name": _name(version, parent), "quantity": _s(line.quantity)}
            for parent in dict.fromkeys([product, *usage])
            for line in structure.children.get(parent, ())
            if line.child == code
        ]
        rows.append({"product": product, "product_name": _name(version, product), "specification": _specification(version, product),
                     "quantity_per_unit": _s(usage[code]), "parents": parents})
    return rows


def _ancestors(parents_of: dict[str, list[tuple[str, Decimal]]], code: str) -> set[str]:
    """code の上にある品番の全部（直の親から一番上まで）。循環していても止まる。"""
    found: set[str] = set()
    stack = [code]
    while stack:
        for parent, _ in parents_of.get(stack.pop(), ()):
            if parent not in found:
                found.add(parent)
                stack.append(parent)
    return found


def _tree(version: Version, code: str) -> list[dict[str, Any]]:
    """この品の下の構成（字下げの深さ・親1つあたりの数・この品1つあたりの数）。子が無ければ空。"""
    rows: list[dict[str, Any]] = []

    def walk(parent: str, level: int, multiplier: Decimal, path: frozenset[str]) -> None:
        if parent in path:
            raise ValueError(f"構成が循環している: {parent}")
        for line in version.children(parent):
            usage = multiplier * line.quantity
            rows.append({"level": level, "parent": parent, "code": line.child, "name": _name(version, line.child),
                         "specification": _specification(version, line.child),
                         "quantity": _s(line.quantity), "usage": _s(usage),
                         "has_children": bool(version.children(line.child))})
            walk(line.child, level + 1, usage, path | {parent})

    walk(code, 1, Decimal(1), frozenset())
    return rows


def lookup_routes(api: "Api") -> list[tuple[str, str, Callable[..., Response]]]:
    return Lookup(api).routes()
