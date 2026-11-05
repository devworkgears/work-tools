"""SQLite への保存。グループ（BOM・マスタ登録・型費・流用先・部品の対応表・変更）と、全体で1つの設定を持つ。

グループどうしは何も共有しない。品目・構成・原価・年間台数は layer（bom＝取り込み・master＝マスタ登録）で分けて持つ。
金額と数量は TEXT（Decimal の文字列）で持つ。SQLite に Decimal の型が無く、REAL では丸めが出るため。
空欄（未登録）は NULL で、0 と分ける。
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .importer import TABLE_FIELDS, ColumnMap, normalize_headers
from .model import (
    MANUAL_VERSION_ID,
    BomLine,
    CostHead,
    CostRow,
    ImportWarning,
    Item,
    Source,
    Version,
    Volume,
    overlay_manual,
)
from .mold import Allocation, MoldCost, ReuseTarget
from .normalize import to_code, to_optional_code
from .rollup import Scope, ScopePolicy
from .scenario import Change, ChangeKind, ChangeRecord, Scenario

ALL_ITEMS = "*"
BOM = "bom"
MASTER = "master"
BOM_TABLES: tuple[str, ...] = tuple(TABLE_FIELDS)  # item・bom_line・cost・volume

SCHEMA = """
CREATE TABLE IF NOT EXISTS grp (
    group_id TEXT PRIMARY KEY,
    dev_number TEXT NOT NULL DEFAULT '',
    dev_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    copied_from TEXT,
    imported_at TEXT,
    source_name TEXT NOT NULL DEFAULT '',
    warnings TEXT,
    bom_tables TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS item (
    group_id TEXT NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    layer TEXT NOT NULL CHECK (layer IN ('bom', 'master')),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    specification TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    has_children INTEGER NOT NULL,
    PRIMARY KEY (group_id, layer, code)
);
CREATE TABLE IF NOT EXISTS bom_line (
    group_id TEXT NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    layer TEXT NOT NULL CHECK (layer IN ('bom', 'master')),
    parent TEXT NOT NULL,
    balloon TEXT,
    child TEXT NOT NULL,
    quantity TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bom_line_parent ON bom_line (group_id, layer, parent);
CREATE TABLE IF NOT EXISTS bom_conflict (
    group_id TEXT NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    layer TEXT NOT NULL CHECK (layer IN ('bom', 'master')),
    parent TEXT NOT NULL,
    balloon TEXT,
    child TEXT NOT NULL,
    quantity TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bom_conflict_group ON bom_conflict (group_id, layer);
CREATE TABLE IF NOT EXISTS cost (
    group_id TEXT NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    layer TEXT NOT NULL CHECK (layer IN ('bom', 'master')),
    code TEXT NOT NULL,
    mbom_code TEXT,
    material TEXT,
    direct TEXT,
    indirect TEXT,
    adjustment TEXT,
    amortization TEXT,
    manufacturing TEXT,
    processing TEXT,
    PRIMARY KEY (group_id, layer, code)
);
CREATE TABLE IF NOT EXISTS volume (
    group_id TEXT NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    layer TEXT NOT NULL CHECK (layer IN ('bom', 'master')),
    product TEXT NOT NULL,
    annual TEXT NOT NULL,
    PRIMARY KEY (group_id, layer, product)
);
CREATE TABLE IF NOT EXISTS mold_cost (
    group_id TEXT NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    part TEXT NOT NULL,
    amount TEXT NOT NULL,
    years TEXT NOT NULL,
    PRIMARY KEY (group_id, part)
);
CREATE TABLE IF NOT EXISTS reuse_target (
    group_id TEXT NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    part TEXT NOT NULL,
    product TEXT NOT NULL,
    quantity TEXT NOT NULL,
    replaced_part TEXT
);
CREATE TABLE IF NOT EXISTS part_map (
    group_id TEXT NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    old_product TEXT NOT NULL,
    new_product TEXT NOT NULL,
    old_part TEXT NOT NULL,
    new_part TEXT NOT NULL,
    old_balloon TEXT,
    new_balloon TEXT
);
CREATE TABLE IF NOT EXISTS scope (
    code TEXT NOT NULL,
    head TEXT NOT NULL,
    scope TEXT NOT NULL,
    PRIMARY KEY (code, head)
);
CREATE TABLE IF NOT EXISTS column_map (
    headers TEXT NOT NULL,
    table_name TEXT NOT NULL,
    columns TEXT NOT NULL,
    PRIMARY KEY (headers, table_name)
);
CREATE TABLE IF NOT EXISTS label (
    column_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    meaning TEXT NOT NULL,
    PRIMARY KEY (column_name, symbol)
);
CREATE TABLE IF NOT EXISTS scenario (
    scenario_id INTEGER PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES grp(group_id) ON DELETE CASCADE,
    base TEXT NOT NULL,
    product_code TEXT NOT NULL,
    head TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (group_id, base)
);
CREATE TABLE IF NOT EXISTS change (
    scenario_id INTEGER NOT NULL REFERENCES scenario(scenario_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    part TEXT NOT NULL,
    replacement TEXT,
    quantity TEXT NOT NULL,
    cost_before TEXT,
    cost_after TEXT,
    group_id TEXT NOT NULL,
    head TEXT NOT NULL,
    volume TEXT,
    allocation TEXT,
    parent TEXT,
    balloon TEXT,
    batch INTEGER,
    target_head TEXT,
    PRIMARY KEY (scenario_id, sequence)
);
"""

PART_MAP_COLUMNS = "group_id, old_product, new_product, old_part, new_part, old_balloon, new_balloon"
CHANGE_COLUMNS = (
    "scenario_id, sequence, recorded_at, kind, part, replacement, quantity, cost_before, cost_after, "
    "group_id, head, volume, allocation, parent, balloon, batch, target_head"
)
GROUP_COLUMNS = ("group_id, dev_number, dev_name, note, created_at, copied_from, imported_at, source_name, warnings, "
                 "bom_tables")

COST_COLUMNS: tuple[tuple[str, CostHead], ...] = (
    ("material", CostHead.MATERIAL),
    ("direct", CostHead.DIRECT),
    ("indirect", CostHead.INDIRECT),
    ("adjustment", CostHead.ADJUSTMENT),
    ("amortization", CostHead.AMORTIZATION),
    ("manufacturing", CostHead.MANUFACTURING),
    ("processing", CostHead.PROCESSING),
)

PartMapKey = tuple[str, str, str, str, str | None, str | None]


class OldDatabaseError(RuntimeError):
    """版の形の古い DB。グループの形に移さないので、開かずに止める。"""


@dataclass(frozen=True)
class GroupInfo:
    group_id: str
    dev_number: str
    dev_name: str
    note: str
    created_at: datetime | None
    copied_from: str | None
    imported_at: datetime | None  # 最後の取り込み
    source_name: str
    warning_count: int  # 最後の取り込みの検証の結果（警告）の件数
    bom_tables: tuple[str, ...]  # BOM に入っている表（BOM_TABLES の並び）


@dataclass(frozen=True)
class PartMapRow:
    old_part: str
    new_part: str
    old_balloon: str | None
    new_balloon: str | None


@dataclass(frozen=True)
class ScenarioInfo:
    scenario_id: int
    base: str
    group_id: str
    product_code: str = ""  # 試算の製品（仮の品目）の品番


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _warnings_json(warnings: Iterable[ImportWarning]) -> str:
    return json.dumps([[w.file, w.row, w.message] for w in warnings], ensure_ascii=False)


def _warnings_from_json(text: str | None) -> tuple[ImportWarning, ...]:
    return () if text is None else tuple(ImportWarning(f, r, m) for f, r, m in json.loads(text))


def _time(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def bom_subset(version: Version, tables: Iterable[str], version_id: str | None = None) -> Version:
    """version の表のうち tables（item・bom_line・cost・volume）だけを写した版。構成の数量の食い違いは構成に付く。"""
    tables = set(tables)
    v = Version(version.version_id if version_id is None else version_id, version.imported_at, version.note,
                version.source_name, warnings=version.warnings, dev_number=version.dev_number,
                dev_name=version.dev_name)
    if "item" in tables:
        v.add_items(version.items.values())
    if "bom_line" in tables:
        v.add_bom_lines(version.lines)
        for conflict in version.conflicts:
            v.add_conflict(conflict.key, conflict.quantities)
    if "cost" in tables:
        v.add_costs(version.cost_rows)
    if "volume" in tables:
        for volume in version.volumes.values():
            v.add_volume(volume)
    return v


class Store:
    """SQLite のファイル1つ（テストでは ":memory:"）。"""

    def __init__(self, path: str | Path) -> None:
        # ファイルの DB の場所（":memory:" は None）。マスタのブックを同じフォルダに置くのに使う
        self.path: Path | None = None if str(path) == ":memory:" else Path(path)
        self._conn = sqlite3.connect(str(path))
        tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "version" in tables or (tables & {"item", "scenario"} and "grp" not in tables):
            self._conn.close()
            raise OldDatabaseError(f"{path} は版の形の古い DB なので開けない（DB を作り直す: 新しいファイルで立てて取り込み直す）")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # グループ ------------------------------------------------------------

    def new_group_id(self) -> str:
        """まだ使っていないグループの ID（数字の文字列）。"""
        n = 1 + max((int(r[0]) for r in self._conn.execute("SELECT group_id FROM grp") if r[0].isdigit()), default=0)
        return str(n)

    def create_group(
        self,
        dev_number: str,
        dev_name: str = "",
        note: str = "",
        *,
        group_id: str | None = None,
        copied_from: str | None = None,
        created_at: datetime | None = None,
        bom: Version | None = None,
        bom_tables: Iterable[str] = (),
        warnings: Iterable[ImportWarning] = (),
        manual: Version | None = None,
        molds: Iterable[MoldCost] = (),
        reuse: Iterable[ReuseTarget] = (),
        part_maps: Iterable[PartMapKey] = (),
        scenarios: Iterable[Scenario] = (),
    ) -> str:
        """グループを1回の書き込みで作り、ID を返す（group_id を省けば new_group_id）。

        bom は BOM に入れる表（bom_tables）の中身、manual はマスタ登録。scenarios はこのグループの版の上の試算
        （scenario_id は振り直し、履歴の版はこのグループにする）。
        """
        group_id = group_id or self.new_group_id()
        tables = [t for t in BOM_TABLES if t in set(bom_tables)]
        if self._conn.execute("SELECT 1 FROM grp WHERE group_id = ?", (group_id,)).fetchone():
            raise ValueError(f"グループ {group_id} はもうある")
        with self._conn:
            self._conn.execute(
                f"INSERT INTO grp ({GROUP_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (group_id, dev_number, dev_name, note, _iso(created_at or datetime.now().replace(microsecond=0)),
                 copied_from, _iso(None if bom is None else bom.imported_at),
                 "" if bom is None else bom.source_name, _warnings_json(warnings), json.dumps(tables)),
            )
            if bom is not None:
                self._insert_rows(group_id, BOM, bom_subset(bom, tables))
            if manual is not None:
                self._insert_rows(group_id, MASTER, manual)
            self._write_masters(group_id, molds, reuse, part_maps)
            for scenario in scenarios:
                scenario.scenario_id = None
                self._write_scenario(scenario, group_id)
        return group_id

    def save_group(self, version: Version, warnings: Iterable[ImportWarning] | None = None) -> str:
        """version の4つの表を全部 BOM に入れたグループを作る（グループの ID は version.version_id）。"""
        return self.create_group(version.dev_number or version.version_id, version.dev_name, version.note,
                                 group_id=version.version_id, created_at=version.imported_at, bom=version,
                                 bom_tables=BOM_TABLES,
                                 warnings=version.warnings if warnings is None else warnings)

    def import_bom(
        self,
        group_id: str,
        bom: Version,
        tables: Iterable[str],
        warnings: Iterable[ImportWarning],
        scenarios: Iterable[Scenario] = (),
        removed_scenarios: Iterable[int] = (),
    ) -> None:
        """まだ入っていない表（tables）の BOM を1回の書き込みで入れる。もう入っている表があれば ValueError。

        最後の取り込みの日時・元のファイル・警告は bom の値にする。scenarios（当て直した試算）は保存し直し、
        removed_scenarios の試算は消す。
        """
        info = self.group_info(group_id)
        tables = set(tables)
        taken = tables & set(info.bom_tables)
        if taken:
            raise ValueError("もう入っている表は取り込めない: " + "・".join(t for t in BOM_TABLES if t in taken))
        merged = [t for t in BOM_TABLES if t in tables | set(info.bom_tables)]
        with self._conn:
            self._insert_rows(group_id, BOM, bom_subset(bom, tables))
            self._conn.execute(
                "UPDATE grp SET imported_at = ?, source_name = ?, warnings = ?, bom_tables = ? WHERE group_id = ?",
                (_iso(bom.imported_at), bom.source_name, _warnings_json(warnings), json.dumps(merged), group_id),
            )
            self._conn.executemany("DELETE FROM scenario WHERE scenario_id = ? AND group_id = ?",
                                   [(sid, group_id) for sid in removed_scenarios])
            for scenario in scenarios:
                self._write_scenario(scenario, group_id)

    def list_groups(self) -> list[GroupInfo]:
        """グループの一覧。作った日時の新しい順。"""
        rows = self._conn.execute(
            f"SELECT {GROUP_COLUMNS} FROM grp ORDER BY created_at DESC, rowid DESC").fetchall()
        return [_group_info(r) for r in rows]

    def group_info(self, group_id: str) -> GroupInfo:
        row = self._conn.execute(f"SELECT {GROUP_COLUMNS} FROM grp WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            raise KeyError(f"グループ {group_id} は無い")
        return _group_info(row)

    def group_warnings(self, group_id: str) -> tuple[ImportWarning, ...]:
        """最後の取り込みの検証の結果（保存した警告）。グループが無ければ KeyError。"""
        row = self._conn.execute("SELECT warnings FROM grp WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            raise KeyError(f"グループ {group_id} は無い")
        return _warnings_from_json(row[0])

    def set_group_note(self, group_id: str, note: str) -> None:
        """グループの検討内容（メモ）だけを書き換える。無いグループは KeyError。"""
        with self._conn:
            cursor = self._conn.execute("UPDATE grp SET note = ? WHERE group_id = ?", (note, group_id))
        if cursor.rowcount == 0:
            raise KeyError(f"グループ {group_id} は無い")

    def delete_group(self, group_id: str) -> None:
        """グループと中身（BOM・マスタ登録・型費・流用先・対応表・変更）を消す。流用先のグループは残る。"""
        with self._conn:
            cursor = self._conn.execute("DELETE FROM grp WHERE group_id = ?", (group_id,))
        if cursor.rowcount == 0:
            raise KeyError(f"グループ {group_id} は無い")

    def load_bom(self, group_id: str) -> Version:
        """グループの BOM（取り込んだ分だけ）。グループが無ければ KeyError。"""
        return self._load_rows(group_id, BOM, Source.IMPORTED)

    def load_manual(self, group_id: str) -> Version:
        """グループのマスタ登録（品目・原価・構成・台数）。登録が無ければ空の版。"""
        manual = Version(MANUAL_VERSION_ID)
        self._read_rows(manual, group_id, MASTER, Source.MANUAL)
        return manual

    def save_manual(self, manual: Version, group_id: str) -> None:
        """グループのマスタ登録の品目・原価・構成・台数を、まるごと入れ替える。"""
        self.group_info(group_id)
        with self._conn:
            self._delete_rows(group_id, MASTER)
            self._insert_rows(group_id, MASTER, manual)

    def overlay_master(self, version: Version) -> Version:
        """BOM の版に、そのグループのマスタ登録を重ねる（マスタ登録 ＞ BOM。overlay_manual）。"""
        return overlay_manual(version, self.load_manual(version.version_id))

    def load_group(self, group_id: str, with_manual: bool = True) -> Version:
        """グループの版。with_manual=True ならマスタ登録を重ねる（隠れた BOM は manual_overlaps）。"""
        version = self.load_bom(group_id)
        return self.overlay_master(version) if with_manual else version

    def _delete_rows(self, group_id: str, layer: str) -> None:
        for table in ("item", "bom_line", "bom_conflict", "cost", "volume"):
            self._conn.execute(f"DELETE FROM {table} WHERE group_id = ? AND layer = ?", (group_id, layer))

    def _insert_rows(self, group_id: str, layer: str, version: Version) -> None:
        c = self._conn
        c.executemany(
            "INSERT INTO item (group_id, layer, code, name, kind, lifecycle, has_children, specification) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(group_id, layer, i.code, i.name, i.kind, i.lifecycle, int(i.has_children), i.specification)
             for i in version.items.values()],
        )
        c.executemany(
            "INSERT INTO bom_line VALUES (?, ?, ?, ?, ?, ?)",
            [(group_id, layer, l.parent, l.balloon, l.child, str(l.quantity)) for l in version.lines],
        )
        c.executemany(
            "INSERT INTO bom_conflict VALUES (?, ?, ?, ?, ?, ?)",
            [(group_id, layer, *conflict.key, str(q)) for conflict in version.conflicts for q in conflict.quantities],
        )
        c.executemany(
            f"INSERT INTO cost (group_id, layer, code, mbom_code, {', '.join(n for n, _ in COST_COLUMNS)}) "
            f"VALUES ({', '.join('?' * (4 + len(COST_COLUMNS)))})",
            [(group_id, layer, r.code, r.mbom_code, *(_text(r.value(h)) for _, h in COST_COLUMNS))
             for r in version.cost_rows],
        )
        c.executemany(
            "INSERT INTO volume VALUES (?, ?, ?, ?)",
            [(group_id, layer, v.product, str(v.annual)) for v in version.volumes.values()],
        )

    def _load_rows(self, group_id: str, layer: str, source: Source) -> Version:
        info = self.group_info(group_id)
        v = Version(group_id, info.imported_at, info.note, info.source_name,
                    warnings=self.group_warnings(group_id), dev_number=info.dev_number, dev_name=info.dev_name)
        self._read_rows(v, group_id, layer, source)
        return v

    def _read_rows(self, v: Version, group_id: str, layer: str, source: Source) -> None:
        q = self._conn.execute
        key = (group_id, layer)
        for code, name, kind, lifecycle, has_children, specification in q(
            "SELECT code, name, kind, lifecycle, has_children, specification FROM item "
            "WHERE group_id = ? AND layer = ? ORDER BY rowid", key,
        ):
            v.add_item(Item(code, name, kind, lifecycle, bool(has_children), source, specification))
        v.add_bom_lines(
            BomLine(p, b, ch, Decimal(qty))
            for p, b, ch, qty in q(
                "SELECT parent, balloon, child, quantity FROM bom_line WHERE group_id = ? AND layer = ? ORDER BY rowid",
                key,
            )
        )
        conflicts: dict[tuple[str, str | None, str], list[Decimal]] = {}
        for p, b, ch, qty in q(
            "SELECT parent, balloon, child, quantity FROM bom_conflict WHERE group_id = ? AND layer = ? ORDER BY rowid",
            key,
        ):
            conflicts.setdefault((p, b, ch), []).append(Decimal(qty))
        for conflict_key, quantities in conflicts.items():
            v.add_conflict(conflict_key, quantities)
        columns = ", ".join(name for name, _ in COST_COLUMNS)
        for row in q(
            f"SELECT code, mbom_code, {columns} FROM cost WHERE group_id = ? AND layer = ? ORDER BY rowid", key
        ):
            values = {h: _decimal(t) for (_, h), t in zip(COST_COLUMNS, row[2:])}
            v.add_cost(CostRow(row[0], values, mbom_code=row[1], source=source))
        for product, annual in q(
            "SELECT product, annual FROM volume WHERE group_id = ? AND layer = ? ORDER BY rowid", key
        ):
            v.add_volume(Volume(product, Decimal(annual), source))

    def apply_master(
        self,
        group_id: str,
        manual: Version | None,
        molds: Iterable[MoldCost],
        reuse: Iterable[ReuseTarget],
        part_maps: Iterable[PartMapKey],
        removed_molds: Iterable[str] = (),
        removed_reuse: Iterable[tuple[str, str]] = (),
        removed_part_maps: Iterable[PartMapKey] = (),
    ) -> None:
        """マスタのブックの取り込みを1回の書き込みで入れる（途中で落ちたら何も入らない）。

        manual はグループの登録と入れ替える（None なら触らない）。型費と流用先は同じキーを入れ替え、
        対応表は足す。removed_* の型費（部品）・流用先（部品, 製品）・対応表の行は消す。
        """
        self.group_info(group_id)
        with self._conn:
            if manual is not None:
                self._delete_rows(group_id, MASTER)
                self._insert_rows(group_id, MASTER, manual)
            self._conn.executemany("DELETE FROM mold_cost WHERE group_id = ? AND part = ?",
                                   [(group_id, p) for p in removed_molds])
            self._conn.executemany("DELETE FROM reuse_target WHERE group_id = ? AND part = ? AND product = ?",
                                   [(group_id, p, pr) for p, pr in removed_reuse])
            self._conn.executemany(
                "DELETE FROM part_map WHERE group_id = ? AND old_product = ? AND new_product = ? AND old_part = ? "
                "AND new_part = ? AND old_balloon IS ? AND new_balloon IS ?",
                [(group_id, *key) for key in removed_part_maps],
            )
            self._write_masters(group_id, molds, reuse, part_maps)

    def _write_masters(self, group_id: str, molds: Iterable[MoldCost], reuse: Iterable[ReuseTarget],
                       part_maps: Iterable[PartMapKey]) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO mold_cost (group_id, part, amount, years) VALUES (?, ?, ?, ?)",
            [(group_id, m.part, str(m.amount), str(m.years)) for m in molds],
        )
        for t in reuse:
            self._conn.execute("DELETE FROM reuse_target WHERE group_id = ? AND part = ? AND product = ?",
                               (group_id, t.part, t.product))
            self._conn.execute(
                "INSERT INTO reuse_target (group_id, part, product, quantity, replaced_part) VALUES (?, ?, ?, ?, ?)",
                (group_id, t.part, t.product, str(t.quantity_per_unit), t.replaced_part),
            )
        self._conn.executemany(
            f"INSERT INTO part_map ({PART_MAP_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(group_id, *key) for key in part_maps],
        )

    # 型費・流用先・部品の対応表（グループごと） --------------------------------

    def save_mold_cost(self, mold: MoldCost, group_id: str) -> None:
        self.group_info(group_id)
        with self._conn:
            self._write_masters(group_id, [mold], (), ())

    def mold_costs(self, group_id: str) -> dict[str, MoldCost]:
        rows = self._conn.execute(
            "SELECT part, amount, years FROM mold_cost WHERE group_id = ? ORDER BY rowid", (group_id,)
        ).fetchall()
        return {p: MoldCost(p, Decimal(a), Decimal(y)) for p, a, y in rows}

    def delete_mold_cost(self, part: str, group_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM mold_cost WHERE group_id = ? AND part = ?", (group_id, to_code(part)))

    def add_reuse_target(self, target: ReuseTarget, group_id: str) -> None:
        self.group_info(group_id)
        with self._conn:
            self._conn.execute(
                "INSERT INTO reuse_target (group_id, part, product, quantity, replaced_part) VALUES (?, ?, ?, ?, ?)",
                (group_id, target.part, target.product, str(target.quantity_per_unit), target.replaced_part),
            )

    def reuse_targets(self, group_id: str, part: str | None = None) -> list[ReuseTarget]:
        rows = self._conn.execute(
            "SELECT part, product, quantity, replaced_part FROM reuse_target WHERE group_id = ? ORDER BY rowid",
            (group_id,),
        ).fetchall()
        targets = [ReuseTarget(p, pr, Decimal(qty), old) for p, pr, qty, old in rows]
        return targets if part is None else [t for t in targets if t.part == to_code(part)]

    def delete_reuse_target(self, target: ReuseTarget, group_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM reuse_target WHERE group_id = ? AND part = ? AND product = ?",
                (group_id, target.part, target.product),
            )

    @staticmethod
    def part_map_key(
        old_product: str, new_product: str, old_part: str, new_part: str,
        old_balloon: str | int | None = None, new_balloon: str | int | None = None,
    ) -> PartMapKey:
        return (*(to_code(c) for c in (old_product, new_product, old_part, new_part)),
                to_optional_code(old_balloon, name="風船番号"), to_optional_code(new_balloon, name="風船番号"))

    def add_part_map(self, group_id: str, *key: Any) -> None:
        """対応表の行（元の製品, 新しい製品, 元の部品, 新しい部品, 元の風船番号, 新しい風船番号）を足す。"""
        self.group_info(group_id)
        with self._conn:
            self._write_masters(group_id, (), (), [self.part_map_key(*key)])

    def part_map(self, group_id: str, old_product: str, new_product: str) -> dict[str, str]:
        """元の製品と新しい製品の部品の対応（compare_structures の part_map に渡す形）。"""
        return {r.old_part: r.new_part for r in self.part_map_rows(group_id, old_product, new_product)}

    def part_map_rows(self, group_id: str, old_product: str, new_product: str) -> list[PartMapRow]:
        """part_map と同じ対応を、風船番号（空欄可）つきの行で返す。"""
        rows = self._conn.execute(
            "SELECT old_part, new_part, old_balloon, new_balloon FROM part_map "
            "WHERE group_id = ? AND old_product = ? AND new_product = ? ORDER BY rowid",
            (group_id, to_code(old_product), to_code(new_product)),
        ).fetchall()
        return [PartMapRow(*r) for r in rows]

    def part_map_pairs(self, group_id: str) -> list[tuple[str, str]]:
        """対応表がある（元の製品, 新しい製品）の組。足した順。"""
        rows = self._conn.execute(
            "SELECT old_product, new_product FROM part_map WHERE group_id = ? "
            "GROUP BY old_product, new_product ORDER BY MIN(rowid)",
            (group_id,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def part_map_keys(self, group_id: str) -> list[PartMapKey]:
        """グループの対応表の全部の行（足した順）。"""
        return [(old, new, r.old_part, r.new_part, r.old_balloon, r.new_balloon)
                for old, new in self.part_map_pairs(group_id) for r in self.part_map_rows(group_id, old, new)]

    def delete_part_map(self, group_id: str, *key: Any) -> int:
        """対応表の行を消し、消した行の数を返す（風船番号の空欄どうしも同じとみなす）。"""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM part_map WHERE group_id = ? AND old_product = ? AND new_product = ? AND old_part = ? "
                "AND new_part = ? AND old_balloon IS ? AND new_balloon IS ?",
                (group_id, *self.part_map_key(*key)),
            )
        return cursor.rowcount

    # 設定（全体で1つ） ------------------------------------------------------

    def save_scope_policy(self, policy: ScopePolicy) -> None:
        """費目の既定は品番 '*' の行、品目ごとの上書きはその品番の行として入れ替える。"""
        rows = [(ALL_ITEMS, h.name, s.name) for h, s in policy.defaults.items()]
        rows += [(code, h.name, s.name) for (code, h), s in policy.overrides.items()]
        with self._conn:
            self._conn.execute("DELETE FROM scope")
            self._conn.executemany("INSERT INTO scope VALUES (?, ?, ?)", rows)

    def load_scope_policy(self) -> ScopePolicy:
        defaults: dict[CostHead, Scope] = {}
        overrides: dict[tuple[str, CostHead], Scope] = {}
        for code, head, scope in self._conn.execute("SELECT code, head, scope FROM scope"):
            if code == ALL_ITEMS:
                defaults[CostHead[head]] = Scope[scope]
            else:
                overrides[(code, CostHead[head])] = Scope[scope]
        return ScopePolicy(defaults, overrides)

    def save_column_map(self, column_map: ColumnMap) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO column_map VALUES (?, ?, ?)",
                (
                    _headers_key(column_map.headers),
                    column_map.table,
                    json.dumps(dict(column_map.columns), ensure_ascii=False, sort_keys=True),
                ),
            )

    def find_column_map(self, headers: Iterable[Any], table: str | None = None) -> ColumnMap | None:
        """見出しの並びがそろうファイルに保存した対応づけ。並びが違えば None。"""
        key = _headers_key(normalize_headers(headers))
        sql = "SELECT table_name, columns FROM column_map WHERE headers = ?"
        params: tuple[str, ...] = (key,)
        if table is not None:
            sql += " AND table_name = ?"
            params += (table,)
        row = self._conn.execute(sql + " ORDER BY rowid", params).fetchone()
        if row is None:
            return None
        return ColumnMap(row[0], tuple(json.loads(key)), json.loads(row[1]))

    def set_label(self, column: str, symbol: str, meaning: str) -> None:
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO label VALUES (?, ?, ?)", (column, symbol, meaning))

    def labels(self, column: str) -> dict[str, str]:
        """列の記号 → 意味。登録が無ければ空。"""
        rows = self._conn.execute(
            "SELECT symbol, meaning FROM label WHERE column_name = ? ORDER BY rowid", (column,)
        ).fetchall()
        return dict(rows)

    # 試算（②の変更。グループ × 製品に1つ） --------------------------------------

    def save_scenario(self, scenario: Scenario) -> int:
        """試算と変更の履歴を保存し、試算の ID を返す（scenario.scenario_id にも入れる）。

        グループは scenario.version.version_id。同じグループの同じ製品（土台）の試算が別にあれば ValueError。
        """
        with self._conn:
            return self._write_scenario(scenario)

    def save_scenarios(self, scenarios: Iterable[Scenario]) -> list[int]:
        """いくつもの試算を1回の書き込み（1つの transaction）で保存し、試算の ID を並びの順で返す。"""
        with self._conn:
            return [self._write_scenario(s) for s in scenarios]

    def _write_scenario(self, scenario: Scenario, group_id: str | None = None) -> int:
        group_id = group_id or scenario.version.version_id
        try:
            if scenario.scenario_id is None:
                cursor = self._conn.execute(
                    "INSERT INTO scenario (group_id, base, product_code, head, note) VALUES (?, ?, ?, ?, ?)",
                    (group_id, scenario.base, scenario.product_code, scenario.head.name, scenario.note),
                )
                scenario.scenario_id = int(cursor.lastrowid)
            else:
                self._conn.execute(
                    "UPDATE scenario SET group_id = ?, base = ?, product_code = ?, head = ?, note = ? "
                    "WHERE scenario_id = ?",
                    (group_id, scenario.base, scenario.product_code, scenario.head.name, scenario.note,
                     scenario.scenario_id),
                )
                self._conn.execute("DELETE FROM change WHERE scenario_id = ?", (scenario.scenario_id,))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise ValueError(f"製品 {scenario.base} の変更はこのグループにもうある") from None
            raise ValueError(f"グループ {group_id} は無い") from None
        self._conn.executemany(
            f"INSERT INTO change ({CHANGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    scenario.scenario_id, r.sequence, r.recorded_at.isoformat(), r.change.kind.name,
                    r.change.part, r.change.replacement, str(r.change.quantity),
                    _text(r.cost_before), _text(r.cost_after), group_id, r.head.name,
                    _text(r.volume), None if r.allocation is None else r.allocation.name,
                    r.change.parent, r.change.balloon, r.batch, r.change.target_head,
                )
                for r in scenario.history
            ],
        )
        return scenario.scenario_id

    def load_scenario(self, scenario_id: int, version: Version | None = None) -> Scenario:
        """試算を読み戻す。版はグループ（マスタ登録を重ねる）、計上範囲は保存した設定を使う。

        version は読み済みの同じグループの版。多くの試算を続けて読むとき、版の読み直しを省く。
        """
        row = self._conn.execute(
            "SELECT group_id, base, product_code, head, note FROM scenario WHERE scenario_id = ?",
            (scenario_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"試算 {scenario_id} は無い")
        group_id, base, product_code, head, note = row
        if version is not None and version.version_id != group_id:
            raise ValueError(f"試算 {scenario_id} のグループは {group_id}（渡された版は {version.version_id}）")
        history = [
            ChangeRecord(
                sequence=seq,
                change=Change(ChangeKind[kind], part, Decimal(qty), replacement, parent, balloon, target_head),
                version_id=group,
                head=CostHead[record_head],
                cost_before=_decimal(before),
                cost_after=_decimal(after),
                recorded_at=datetime.fromisoformat(at),
                volume=_decimal(volume),
                allocation=None if allocation is None else Allocation[allocation],
                batch=batch,
            )
            for (seq, at, kind, part, replacement, qty, before, after, group, record_head, volume, allocation,
                 parent, balloon, batch, target_head) in self._conn.execute(
                "SELECT sequence, recorded_at, kind, part, replacement, quantity, cost_before, cost_after, "
                "group_id, head, volume, allocation, parent, balloon, batch, target_head FROM change "
                "WHERE scenario_id = ? ORDER BY sequence",
                (scenario_id,),
            )
        ]
        return Scenario(
            "",
            base,
            product_code,
            version if version is not None else self.load_group(group_id),
            CostHead[head],
            self.load_scope_policy(),
            note,
            history=history,
            scenario_id=scenario_id,
        )

    def list_scenarios(self, group_id: str | None = None) -> list[ScenarioInfo]:
        """試算の一覧（作った順）。group_id を渡せばそのグループの分だけ。"""
        sql = "SELECT scenario_id, base, group_id, product_code FROM scenario"
        params: tuple[str, ...] = ()
        if group_id is not None:
            sql += " WHERE group_id = ?"
            params = (group_id,)
        return [ScenarioInfo(*r) for r in self._conn.execute(sql + " ORDER BY scenario_id", params)]

    def delete_scenario(self, scenario_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM scenario WHERE scenario_id = ?", (scenario_id,))


def _group_info(row: tuple[Any, ...]) -> GroupInfo:
    (group_id, dev_number, dev_name, note, created_at, copied_from, imported_at, source_name, warnings,
     tables) = row
    return GroupInfo(group_id, dev_number, dev_name, note, _time(created_at), copied_from, _time(imported_at),
                     source_name, len(_warnings_from_json(warnings)), tuple(json.loads(tables)))


def retarget(scenario: Scenario, version: Version) -> Scenario:
    """試算の土台・変更の記録をそのまま、別のグループの版の上に写す（原価は計算し直さない・まだ保存しない）。"""
    return Scenario(scenario.name, scenario.base, scenario.product_code, version, scenario.head, scenario.policy,
                    scenario.note, history=[replace(r, version_id=version.version_id) for r in scenario.history])


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _headers_key(headers: tuple[str, ...]) -> str:
    return json.dumps(list(headers), ensure_ascii=False)
