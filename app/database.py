"""SQLite access with deterministic connection cleanup on Windows."""

import sqlite3

from app.config import get_settings
from app import database_legacy as _legacy
from app.database_legacy import *  # noqa: F403


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(get_settings().database_path, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


_legacy.connect = connect
