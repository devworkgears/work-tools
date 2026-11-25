"""SQLite のファイル1つ。設計検討（study の StudyStore）が、この接続に自分の表を持つ。"""

import sqlite3
from pathlib import Path


class Store:
    """SQLite のファイル（テストでは ":memory:"）。ほかの表が入っているファイルでも、その表には触らない。"""

    def __init__(self, path: str | Path) -> None:
        # ファイルの DB の場所（":memory:" は None）。設定のファイルを同じフォルダに置くのに使う
        self.path: Path | None = None if str(path) == ":memory:" else Path(path)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA foreign_keys = ON")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
