"""SQLite への保存。取り込みの版・手入力の版 manual・手入力のデータと設定・試算の履歴を持つ。

金額と数量は TEXT（Decimal の文字列）で持つ。SQLite に Decimal の型が無く、REAL では丸めが出るため。
空欄（未登録）は NULL で、0 と分ける。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .importer import ColumnMap, normalize_headers
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
    is_manual_version_id,
    manual_version_id,
    overlay_manual,
)
from .mold import Allocation, MoldCost, ReuseTarget
from .normalize import to_code, to_optional_code
from .rollup import Scope, ScopePolicy
from .scenario import Change, ChangeKind, ChangeRecord, Scenario

ALL_ITEMS = "*"

SCHEMA = """
CREATE TABLE IF NOT EXISTS version (
    version_id TEXT PRIMARY KEY,
    imported_at TEXT,
    source_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    warnings TEXT,
    dev_number TEXT,
    dev_name TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS item (
    version_id TEXT NOT NULL REFERENCES version(version_id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    has_children INTEGER NOT NULL,
    PRIMARY KEY (version_id, code)
);
CREATE TABLE IF NOT EXISTS bom_line (
    version_id TEXT NOT NULL REFERENCES version(version_id) ON DELETE CASCADE,
    parent TEXT NOT NULL,
    balloon TEXT,
    child TEXT NOT NULL,
    quantity TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bom_line_parent ON bom_line (version_id, parent);
CREATE TABLE IF NOT EXISTS bom_conflict (
    version_id TEXT NOT NULL REFERENCES version(version_id) ON DELETE CASCADE,
    parent TEXT NOT NULL,
    balloon TEXT,
    child TEXT NOT NULL,
    quantity TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bom_conflict_version ON bom_conflict (version_id);
CREATE TABLE IF NOT EXISTS cost (
    version_id TEXT NOT NULL REFERENCES version(version_id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    mbom_code TEXT,
    material TEXT,
    direct TEXT,
    indirect TEXT,
    adjustment TEXT,
    amortization TEXT,
    manufacturing TEXT,
    PRIMARY KEY (version_id, code)
);
CREATE TABLE IF NOT EXISTS volume (
    version_id TEXT NOT NULL REFERENCES version(version_id) ON DELETE CASCADE,
    product TEXT NOT NULL,
    annual TEXT NOT NULL,
    PRIMARY KEY (version_id, product)
);
CREATE TABLE IF NOT EXISTS mold_cost (
    part TEXT NOT NULL,
    amount TEXT NOT NULL,
    years TEXT NOT NULL,
    dev_number TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (dev_number, part)
);
CREATE TABLE IF NOT EXISTS reuse_target (
    part TEXT NOT NULL,
    product TEXT NOT NULL,
    quantity TEXT NOT NULL,
    replaced_part TEXT,
    dev_number TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS part_map (
    old_product TEXT NOT NULL,
    new_product TEXT NOT NULL,
    old_part TEXT NOT NULL,
    new_part TEXT NOT NULL,
    old_balloon TEXT,
    new_balloon TEXT,
    dev_number TEXT NOT NULL DEFAULT ''
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
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    base TEXT NOT NULL,
    product_code TEXT NOT NULL,
    head TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES version(version_id)
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
    version_id TEXT NOT NULL REFERENCES version(version_id),
    head TEXT NOT NULL,
    volume TEXT,
    allocation TEXT,
    parent TEXT,
    balloon TEXT,
    batch INTEGER,
    PRIMARY KEY (scenario_id, sequence)
);
"""

# 後から change に足した列（古い DB は開いたときに足す。今ある行は NULL のまま）
CHANGE_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (("parent", "TEXT"), ("balloon", "TEXT"), ("batch", "INTEGER"))
# 後から version に足した列。古い DB の版は開発番号＝版 ID・名称＝空・第1版にする
VERSION_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("dev_number", "TEXT"), ("dev_name", "TEXT NOT NULL DEFAULT ''"), ("revision", "INTEGER NOT NULL DEFAULT 1")
)
PART_MAP_COLUMNS = "old_product, new_product, old_part, new_part, old_balloon, new_balloon, dev_number"
CHANGE_COLUMNS = (
    "scenario_id, sequence, recorded_at, kind, part, replacement, quantity, cost_before, cost_after, "
    "version_id, head, volume, allocation, parent, balloon, batch"
)

COST_COLUMNS: tuple[tuple[str, CostHead], ...] = (
    ("material", CostHead.MATERIAL),
    ("direct", CostHead.DIRECT),
    ("indirect", CostHead.INDIRECT),
    ("adjustment", CostHead.ADJUSTMENT),
    ("amortization", CostHead.AMORTIZATION),
    ("manufacturing", CostHead.MANUFACTURING),
)


class VersionInUseError(ValueError):
    """試算が使っている版は消せない。scenarios は使っている試算の名前。"""

    def __init__(self, version_id: str, scenarios: Iterable[str]) -> None:
        self.version_id = version_id
        self.scenarios = tuple(scenarios)
        super().__init__(f"版 {version_id} は試算が使っているので消せない: {'・'.join(self.scenarios)}")


@dataclass(frozen=True)
class VersionInfo:
    version_id: str
    imported_at: datetime | None
    source_name: str
    note: str
    warning_count: int = 0  # 取り込みの検証の結果（警告）の件数
    dev_number: str = ""
    dev_name: str = ""
    revision: int = 1


@dataclass(frozen=True)
class PartMapRow:
    old_part: str
    new_part: str
    old_balloon: str | None
    new_balloon: str | None


@dataclass(frozen=True)
class ScenarioInfo:
    scenario_id: int
    name: str
    base: str
    version_id: str
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


class Store:
    """SQLite のファイル1つ（テストでは ":memory:"）。"""

    def __init__(self, path: str | Path) -> None:
        # ファイルの DB の場所（":memory:" は None）。マスタのブックを同じフォルダに置くのに使う
        self.path: Path | None = None if str(path) == ":memory:" else Path(path)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(change)")}
        for column, sql_type in CHANGE_ADDED_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE change ADD COLUMN {column} {sql_type}")
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(version)")}
        for column, sql_type in VERSION_ADDED_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE version ADD COLUMN {column} {sql_type}")
        self._conn.execute("UPDATE version SET dev_number = version_id WHERE dev_number IS NULL OR dev_number = ''")
        self._migrate_master_tables()
        self._conn.execute(
            "INSERT OR IGNORE INTO version (version_id, note) VALUES (?, ?)",
            (MANUAL_VERSION_ID, "手入力"),
        )
        self._conn.commit()

    def _migrate_master_tables(self) -> None:
        """古い DB の型費・流用先・対応表に開発番号の列を足す。今ある行は共通（空）になる。

        型費は主キーが部品だけだったので、（開発番号, 部品）の主キーの表に作り直す。
        """
        for table in ("reuse_target", "part_map"):
            if "dev_number" not in {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN dev_number TEXT NOT NULL DEFAULT ''")
        if "dev_number" not in {row[1] for row in self._conn.execute("PRAGMA table_info(mold_cost)")}:
            self._conn.execute("ALTER TABLE mold_cost RENAME TO mold_cost_old")
            self._conn.execute(
                "CREATE TABLE mold_cost (part TEXT NOT NULL, amount TEXT NOT NULL, years TEXT NOT NULL, "
                "dev_number TEXT NOT NULL DEFAULT '', PRIMARY KEY (dev_number, part))"
            )
            self._conn.execute(
                "INSERT INTO mold_cost (part, amount, years) SELECT part, amount, years FROM mold_cost_old ORDER BY rowid"
            )
            self._conn.execute("DROP TABLE mold_cost_old")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # 版 ------------------------------------------------------------------

    def save_version(
        self,
        version: Version,
        replace: bool = False,
        warnings: Iterable[ImportWarning] | None = None,
    ) -> None:
        """取り込みの版を保存する。同じ版 ID があれば replace=True のときだけ入れ替える。

        warnings は取り込みの検証の結果（VersionImport.warnings）。省けば version.warnings を保存する。
        """
        warnings_text = _warnings_json(version.warnings if warnings is None else warnings)
        if is_manual_version_id(version.version_id):
            raise ValueError("manual は save_manual で保存する")
        if _has_manual_rows(version):
            raise ValueError("手入力の行（manual を重ねた版）は取り込みの版として保存できない")
        exists = self._conn.execute(
            "SELECT 1 FROM version WHERE version_id = ?", (version.version_id,)
        ).fetchone()
        if exists and not replace:
            raise ValueError(f"版 {version.version_id} はもうある（入れ替えるなら replace=True）")
        dev_number = version.dev_number or version.version_id
        with self._conn:
            if exists:
                self._delete_rows(version.version_id)
                self._conn.execute(
                    "UPDATE version SET imported_at = ?, source_name = ?, note = ?, warnings = ?, "
                    "dev_number = ?, dev_name = ?, revision = ? WHERE version_id = ?",
                    (_iso(version.imported_at), version.source_name, version.note, warnings_text,
                     dev_number, version.dev_name, version.revision, version.version_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO version (version_id, imported_at, source_name, note, warnings, dev_number, dev_name, "
                    "revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (version.version_id, _iso(version.imported_at), version.source_name, version.note,
                     warnings_text, dev_number, version.dev_name, version.revision),
                )
            self._insert_rows(version.version_id, version)

    def save_manual(self, manual: Version, dev_number: str = "") -> None:
        """マスタ登録の品目・原価・構成・台数を、まるごと入れ替える。開発番号が空なら共通の登録。"""
        version_id = manual_version_id(dev_number)
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO version (version_id, note, dev_number) VALUES (?, ?, ?)",
                (version_id, "手入力", dev_number or version_id),
            )
            self._delete_rows(version_id)
            self._insert_rows(version_id, manual)

    def load_manual(self, dev_number: str = "") -> Version:
        """マスタ登録（その開発番号の分だけ。空なら共通）。登録が無ければ空の版。"""
        version_id = manual_version_id(dev_number)
        try:
            return self._load_rows(version_id, Source.MANUAL)
        except KeyError:
            return Version(version_id)

    def overlay_master(self, version: Version) -> Version:
        """取り込みの版に、その開発番号の登録と共通の登録を敷く（同じ品番は BOM ＞ 開発番号 ＞ 共通）。"""
        return overlay_manual(version, self.load_manual(version.dev_number), self.load_manual())

    def load_version(self, version_id: str, with_manual: bool = True) -> Version:
        """版を読み戻す。with_manual=True ならマスタ登録を敷く（overlay_master。BOM に隠れた登録は manual_overlaps）。"""
        if version_id == MANUAL_VERSION_ID:
            return self.load_manual()
        if is_manual_version_id(version_id):
            raise KeyError(f"版 {version_id} は無い")
        version = self._load_rows(version_id, Source.IMPORTED)
        return self.overlay_master(version) if with_manual else version

    def list_versions(self) -> list[VersionInfo]:
        """取り込みの版の一覧（マスタ登録の版は含めない）。取り込んだ日時の新しい順。"""
        rows = self._conn.execute(
            "SELECT version_id, imported_at, source_name, note, warnings, dev_number, dev_name, revision FROM version "
            "WHERE version_id <> ? AND substr(version_id, 1, ?) <> ? ORDER BY imported_at DESC, version_id DESC",
            (MANUAL_VERSION_ID, len(MANUAL_VERSION_ID) + 1, MANUAL_VERSION_ID + ":"),
        ).fetchall()
        return [VersionInfo(r[0], _time(r[1]), r[2], r[3], len(_warnings_from_json(r[4])), r[5], r[6], r[7])
                for r in rows]

    def latest_revision(self, dev_number: str) -> VersionInfo | None:
        """開発番号の一番新しい版（版の番号が最大のもの）。無ければ None。"""
        found = [v for v in self.list_versions() if v.dev_number == dev_number]
        return max(found, key=lambda v: v.revision) if found else None

    def new_version_id(self, dev_number: str, revision: int) -> str:
        """取り込みの版に振る版 ID（開発番号-r版の番号）。既にある ID・manual と重なれば後ろに番号を足す。"""
        base = f"{dev_number}-r{revision}"
        candidate, n = base, 1
        while is_manual_version_id(candidate) or self._conn.execute(
            "SELECT 1 FROM version WHERE version_id = ?", (candidate,)
        ).fetchone():
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def version_warnings(self, version_id: str) -> tuple[ImportWarning, ...]:
        """取り込みの検証の結果（保存した警告）。版が無ければ KeyError。"""
        row = self._conn.execute("SELECT warnings FROM version WHERE version_id = ?", (version_id,)).fetchone()
        if row is None:
            raise KeyError(f"版 {version_id} は無い")
        return _warnings_from_json(row[0])

    def set_version_note(self, version_id: str, note: str) -> None:
        """版のメモだけを書き換える。マスタ登録の版と無い版は KeyError。"""
        if is_manual_version_id(version_id):
            raise KeyError(f"版 {version_id} は無い")
        with self._conn:
            cursor = self._conn.execute("UPDATE version SET note = ? WHERE version_id = ?", (note, version_id))
        if cursor.rowcount == 0:
            raise KeyError(f"版 {version_id} は無い")

    def delete_version(self, version_id: str) -> None:
        """版を消す。マスタ登録の版と、試算が使っている版は消せない。"""
        if is_manual_version_id(version_id):
            raise ValueError("manual は消せない")
        users = self._conn.execute(
            "SELECT DISTINCT s.name FROM scenario s LEFT JOIN change c ON c.scenario_id = s.scenario_id "
            "WHERE s.version_id = ? OR c.version_id = ? ORDER BY s.scenario_id",
            (version_id, version_id),
        ).fetchall()
        if users:
            raise VersionInUseError(version_id, [u[0] for u in users])
        with self._conn:
            cursor = self._conn.execute("DELETE FROM version WHERE version_id = ?", (version_id,))
        if cursor.rowcount == 0:
            raise KeyError(f"版 {version_id} は無い")

    def _delete_rows(self, version_id: str) -> None:
        for table in ("item", "bom_line", "bom_conflict", "cost", "volume"):
            self._conn.execute(f"DELETE FROM {table} WHERE version_id = ?", (version_id,))

    def _insert_rows(self, version_id: str, version: Version) -> None:
        c = self._conn
        c.executemany(
            "INSERT INTO item VALUES (?, ?, ?, ?, ?, ?)",
            [(version_id, i.code, i.name, i.kind, i.lifecycle, int(i.has_children))
             for i in version.items.values()],
        )
        c.executemany(
            "INSERT INTO bom_line VALUES (?, ?, ?, ?, ?)",
            [(version_id, l.parent, l.balloon, l.child, str(l.quantity)) for l in version.lines],
        )
        c.executemany(
            "INSERT INTO bom_conflict VALUES (?, ?, ?, ?, ?)",
            [(version_id, *conflict.key, str(q)) for conflict in version.conflicts for q in conflict.quantities],
        )
        c.executemany(
            "INSERT INTO cost VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(version_id, r.code, r.mbom_code, *(_text(r.value(h)) for _, h in COST_COLUMNS))
             for r in version.cost_rows],
        )
        c.executemany(
            "INSERT INTO volume VALUES (?, ?, ?)",
            [(version_id, v.product, str(v.annual)) for v in version.volumes.values()],
        )

    def _load_rows(self, version_id: str, source: Source) -> Version:
        head = self._conn.execute(
            "SELECT imported_at, source_name, note, warnings, dev_number, dev_name, revision FROM version "
            "WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        if head is None:
            raise KeyError(f"版 {version_id} は無い")
        v = Version(version_id, _time(head[0]), head[2], head[1], warnings=_warnings_from_json(head[3]),
                    dev_number=head[4] or version_id, dev_name=head[5], revision=head[6])
        q = self._conn.execute
        for code, name, kind, lifecycle, has_children in q(
            "SELECT code, name, kind, lifecycle, has_children FROM item WHERE version_id = ? ORDER BY rowid",
            (version_id,),
        ):
            v.add_item(Item(code, name, kind, lifecycle, bool(has_children), source))
        v.add_bom_lines(
            BomLine(p, b, ch, Decimal(qty))
            for p, b, ch, qty in q(
                "SELECT parent, balloon, child, quantity FROM bom_line WHERE version_id = ? ORDER BY rowid",
                (version_id,),
            )
        )
        conflicts: dict[tuple[str, str | None, str], list[Decimal]] = {}
        for p, b, ch, qty in q(
            "SELECT parent, balloon, child, quantity FROM bom_conflict WHERE version_id = ? ORDER BY rowid",
            (version_id,),
        ):
            conflicts.setdefault((p, b, ch), []).append(Decimal(qty))
        for key, quantities in conflicts.items():
            v.add_conflict(key, quantities)
        columns = ", ".join(name for name, _ in COST_COLUMNS)
        for row in q(
            f"SELECT code, mbom_code, {columns} FROM cost WHERE version_id = ? ORDER BY rowid", (version_id,)
        ):
            values = {h: _decimal(t) for (_, h), t in zip(COST_COLUMNS, row[2:])}
            v.add_cost(CostRow(row[0], values, mbom_code=row[1], source=source))
        for product, annual in q(
            "SELECT product, annual FROM volume WHERE version_id = ? ORDER BY rowid", (version_id,)
        ):
            v.add_volume(Volume(product, Decimal(annual), source))
        return v

    def apply_master(
        self,
        manual: Version | None,
        molds: Iterable[MoldCost],
        reuse: Iterable[ReuseTarget],
        part_maps: Iterable[tuple[str, str, str, str, str | None, str | None]],
        dev_number: str = "",
    ) -> None:
        """マスタのブックの取り込みを1回の書き込みで入れる（途中で落ちたら何も入らない）。

        manual は入れ替えるその開発番号（空なら共通）の登録（None なら触らない）。型費と流用先は同じキーを入れ替え、
        対応表は足す。
        """
        version_id = manual_version_id(dev_number)
        with self._conn:
            if manual is not None:
                self._conn.execute(
                    "INSERT OR IGNORE INTO version (version_id, note, dev_number) VALUES (?, ?, ?)",
                    (version_id, "手入力", dev_number or version_id),
                )
                self._delete_rows(version_id)
                self._insert_rows(version_id, manual)
            self._conn.executemany(
                "INSERT OR REPLACE INTO mold_cost (part, amount, years, dev_number) VALUES (?, ?, ?, ?)",
                [(m.part, str(m.amount), str(m.years), dev_number) for m in molds],
            )
            for t in reuse:
                self._conn.execute("DELETE FROM reuse_target WHERE part = ? AND product = ? AND dev_number = ?",
                                   (t.part, t.product, dev_number))
                self._conn.execute(
                    "INSERT INTO reuse_target (part, product, quantity, replaced_part, dev_number) VALUES (?, ?, ?, ?, ?)",
                    (t.part, t.product, str(t.quantity_per_unit), t.replaced_part, dev_number),
                )
            self._conn.executemany(
                f"INSERT INTO part_map ({PART_MAP_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(*key, dev_number) for key in part_maps],
            )

    def master_dev_numbers(self) -> list[str]:
        """開発番号ごとの登録（品目・原価・構成・台数・型費・流用先・対応表）がある開発番号。"""
        found: set[str] = set()
        prefix = MANUAL_VERSION_ID + ":"
        for (version_id,) in self._conn.execute(
            "SELECT version_id FROM version WHERE substr(version_id, 1, ?) = ?", (len(prefix), prefix)
        ):
            if any(self._conn.execute(f"SELECT 1 FROM {t} WHERE version_id = ? LIMIT 1", (version_id,)).fetchone()
                   for t in ("item", "bom_line", "cost", "volume")):
                found.add(version_id[len(prefix):])
        for table in ("mold_cost", "reuse_target", "part_map"):
            found.update(r[0] for r in self._conn.execute(f"SELECT DISTINCT dev_number FROM {table} WHERE dev_number <> ''"))
        return sorted(found)

    def bom_entries(self, codes: Iterable[str], dev_number: str = "") -> dict[str, list[tuple[str, int]]]:
        """品番 → BOM（取り込みの版の品目・構成・原価・台数）に初めて入った（開発番号, 第何版）の並び。

        開発番号を渡せばその開発番号の版だけを見る。BOM に無い品番は入れない。
        """
        wanted = list(dict.fromkeys(to_code(c) for c in codes))
        found: dict[str, dict[str, int]] = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            marks = ", ".join("?" * len(chunk))
            sql = (
                "SELECT x.code, v.dev_number, MIN(v.revision) FROM ("
                "SELECT version_id, code FROM item UNION SELECT version_id, code FROM cost "
                "UNION SELECT version_id, parent FROM bom_line UNION SELECT version_id, child FROM bom_line "
                "UNION SELECT version_id, product FROM volume) x JOIN version v ON v.version_id = x.version_id "
                f"WHERE x.code IN ({marks}) AND v.version_id <> ? AND substr(v.version_id, 1, ?) <> ?"
            )
            params: list[Any] = [*chunk, MANUAL_VERSION_ID, len(MANUAL_VERSION_ID) + 1, MANUAL_VERSION_ID + ":"]
            if dev_number:
                sql += " AND v.dev_number = ?"
                params.append(dev_number)
            for code, dev, revision in self._conn.execute(sql + " GROUP BY x.code, v.dev_number", params):
                found.setdefault(code, {})[dev] = revision
        return {code: sorted(found[code].items()) for code in wanted if code in found}

    # 型費・流用先・部品の対応表（dev_number が空なら共通の登録。*_for は開発番号の分と共通を重ねて読む） ----

    def save_mold_cost(self, mold: MoldCost, dev_number: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO mold_cost (part, amount, years, dev_number) VALUES (?, ?, ?, ?)",
                (mold.part, str(mold.amount), str(mold.years), dev_number),
            )

    def mold_costs(self, dev_number: str = "") -> dict[str, MoldCost]:
        rows = self._conn.execute(
            "SELECT part, amount, years FROM mold_cost WHERE dev_number = ? ORDER BY rowid", (dev_number,)
        ).fetchall()
        return {p: MoldCost(p, Decimal(a), Decimal(y)) for p, a, y in rows}

    def mold_costs_for(self, dev_number: str) -> dict[str, MoldCost]:
        """その開発番号の版で使う型費（同じ部品は開発番号の登録 ＞ 共通）。"""
        molds = self.mold_costs()
        if dev_number:
            molds.update(self.mold_costs(dev_number))
        return molds

    def delete_mold_cost(self, part: str, dev_number: str = "") -> None:
        with self._conn:
            self._conn.execute("DELETE FROM mold_cost WHERE part = ? AND dev_number = ?", (to_code(part), dev_number))

    def add_reuse_target(self, target: ReuseTarget, dev_number: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO reuse_target (part, product, quantity, replaced_part, dev_number) VALUES (?, ?, ?, ?, ?)",
                (target.part, target.product, str(target.quantity_per_unit), target.replaced_part, dev_number),
            )

    def reuse_targets(self, part: str | None = None, dev_number: str = "") -> list[ReuseTarget]:
        rows = self._conn.execute(
            "SELECT part, product, quantity, replaced_part FROM reuse_target WHERE dev_number = ? ORDER BY rowid",
            (dev_number,),
        ).fetchall()
        targets = [ReuseTarget(p, pr, Decimal(qty), old) for p, pr, qty, old in rows]
        return targets if part is None else [t for t in targets if t.part == to_code(part)]

    def reuse_targets_for(self, dev_number: str, part: str | None = None) -> list[ReuseTarget]:
        """その開発番号の版で使う流用先（同じ部品と流用先の組は開発番号の登録 ＞ 共通）。"""
        own = self.reuse_targets(part, dev_number) if dev_number else []
        keys = {(t.part, t.product) for t in own}
        return own + [t for t in self.reuse_targets(part) if (t.part, t.product) not in keys]

    def delete_reuse_target(self, target: ReuseTarget, dev_number: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM reuse_target WHERE part = ? AND product = ? AND dev_number = ?",
                (target.part, target.product, dev_number),
            )

    def add_part_map(
        self,
        old_product: str,
        new_product: str,
        old_part: str,
        new_part: str,
        old_balloon: str | int | None = None,
        new_balloon: str | int | None = None,
        dev_number: str = "",
    ) -> None:
        with self._conn:
            self._conn.execute(
                f"INSERT INTO part_map ({PART_MAP_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*(to_code(c) for c in (old_product, new_product, old_part, new_part)),
                 to_optional_code(old_balloon, name="風船番号"), to_optional_code(new_balloon, name="風船番号"),
                 dev_number),
            )

    def part_map(self, old_product: str, new_product: str, dev_number: str = "") -> dict[str, str]:
        """元の製品と新しい製品の部品の対応（compare_structures の part_map に渡す形）。"""
        return {r.old_part: r.new_part for r in self.part_map_rows(old_product, new_product, dev_number)}

    def part_map_for(self, dev_number: str, old_product: str, new_product: str) -> dict[str, str]:
        """その開発番号の版で使う部品の対応（同じ元の部品は開発番号の登録 ＞ 共通）。"""
        mapping = self.part_map(old_product, new_product)
        if dev_number:
            mapping.update(self.part_map(old_product, new_product, dev_number))
        return mapping

    def part_map_rows(self, old_product: str, new_product: str, dev_number: str = "") -> list[PartMapRow]:
        """part_map と同じ対応を、風船番号（空欄可）つきの行で返す。"""
        rows = self._conn.execute(
            "SELECT old_part, new_part, old_balloon, new_balloon FROM part_map "
            "WHERE old_product = ? AND new_product = ? AND dev_number = ? ORDER BY rowid",
            (to_code(old_product), to_code(new_product), dev_number),
        ).fetchall()
        return [PartMapRow(*r) for r in rows]

    def part_map_pairs(self, dev_number: str = "") -> list[tuple[str, str]]:
        """対応表がある（元の製品, 新しい製品）の組。足した順。"""
        rows = self._conn.execute(
            "SELECT old_product, new_product FROM part_map WHERE dev_number = ? "
            "GROUP BY old_product, new_product ORDER BY MIN(rowid)",
            (dev_number,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def delete_part_map(
        self,
        old_product: str,
        new_product: str,
        old_part: str,
        new_part: str,
        old_balloon: str | int | None = None,
        new_balloon: str | int | None = None,
        dev_number: str = "",
    ) -> int:
        """対応表の行を消し、消した行の数を返す（風船番号の空欄どうしも同じとみなす）。"""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM part_map WHERE old_product = ? AND new_product = ? AND old_part = ? AND new_part = ? "
                "AND old_balloon IS ? AND new_balloon IS ? AND dev_number = ?",
                (*(to_code(c) for c in (old_product, new_product, old_part, new_part)),
                 to_optional_code(old_balloon, name="風船番号"), to_optional_code(new_balloon, name="風船番号"),
                 dev_number),
            )
        return cursor.rowcount

    # 設定 ----------------------------------------------------------------

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

    # 試算 ----------------------------------------------------------------

    def save_scenario(self, scenario: Scenario) -> int:
        """試算と変更の履歴を保存し、試算の ID を返す（scenario.scenario_id にも入れる）。

        試算の版は保存済みの版（または manual）であること。
        """
        with self._conn:
            if scenario.scenario_id is None:
                cursor = self._conn.execute(
                    "INSERT INTO scenario (name, note, base, product_code, head, version_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (scenario.name, scenario.note, scenario.base, scenario.product_code,
                     scenario.head.name, scenario.version.version_id),
                )
                scenario.scenario_id = int(cursor.lastrowid)
            else:
                self._conn.execute(
                    "UPDATE scenario SET name = ?, note = ?, base = ?, product_code = ?, head = ?, "
                    "version_id = ? WHERE scenario_id = ?",
                    (scenario.name, scenario.note, scenario.base, scenario.product_code,
                     scenario.head.name, scenario.version.version_id, scenario.scenario_id),
                )
                self._conn.execute("DELETE FROM change WHERE scenario_id = ?", (scenario.scenario_id,))
            self._conn.executemany(
                f"INSERT INTO change ({CHANGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        scenario.scenario_id, r.sequence, r.recorded_at.isoformat(), r.change.kind.name,
                        r.change.part, r.change.replacement, str(r.change.quantity),
                        _text(r.cost_before), _text(r.cost_after), r.version_id, r.head.name,
                        _text(r.volume), None if r.allocation is None else r.allocation.name,
                        r.change.parent, r.change.balloon, r.batch,
                    )
                    for r in scenario.history
                ],
            )
        return scenario.scenario_id

    def load_scenario(self, scenario_id: int, version: Version | None = None) -> Scenario:
        """試算を読み戻す。版は manual を重ねて読み、計上範囲は保存した設定を使う。

        version は読み済みの同じ版（manual を重ねたもの）。多くの試算を続けて読むとき、版の読み直しを省く。
        """
        row = self._conn.execute(
            "SELECT name, note, base, product_code, head, version_id FROM scenario WHERE scenario_id = ?",
            (scenario_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"試算 {scenario_id} は無い")
        name, note, base, product_code, head, version_id = row
        if version is not None and version.version_id != version_id:
            raise ValueError(f"試算 {scenario_id} の版は {version_id}（渡された版は {version.version_id}）")
        history = [
            ChangeRecord(
                sequence=seq,
                change=Change(ChangeKind[kind], part, Decimal(qty), replacement, parent, balloon),
                version_id=ver,
                head=CostHead[head],
                cost_before=_decimal(before),
                cost_after=_decimal(after),
                recorded_at=datetime.fromisoformat(at),
                volume=_decimal(volume),
                allocation=None if allocation is None else Allocation[allocation],
                batch=batch,
            )
            for (seq, at, kind, part, replacement, qty, before, after, ver, head, volume, allocation,
                 parent, balloon, batch) in self._conn.execute(
                "SELECT sequence, recorded_at, kind, part, replacement, quantity, cost_before, cost_after, "
                "version_id, head, volume, allocation, parent, balloon, batch FROM change "
                "WHERE scenario_id = ? ORDER BY sequence",
                (scenario_id,),
            )
        ]
        return Scenario(
            name,
            base,
            product_code,
            version if version is not None else self.load_version(version_id),
            CostHead[head],
            self.load_scope_policy(),
            note,
            history=history,
            scenario_id=scenario_id,
        )

    def list_scenarios(self) -> list[ScenarioInfo]:
        rows = self._conn.execute(
            "SELECT scenario_id, name, base, version_id, product_code FROM scenario ORDER BY scenario_id"
        ).fetchall()
        return [ScenarioInfo(*r) for r in rows]

    def delete_scenario(self, scenario_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM scenario WHERE scenario_id = ?", (scenario_id,))


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _headers_key(headers: tuple[str, ...]) -> str:
    return json.dumps(list(headers), ensure_ascii=False)


def _has_manual_rows(version: Version) -> bool:
    return (
        any(i.source is Source.MANUAL for i in version.items.values())
        or any(r.source is Source.MANUAL for r in version.cost_rows)
        or any(v.source is Source.MANUAL for v in version.volumes.values())
        or bool(version.manual_overlaps)
    )
