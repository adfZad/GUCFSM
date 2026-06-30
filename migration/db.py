"""
Database connection module for GUC Field Service Bot.
Supports local SQL Server and Azure SQL via pyodbc.
Drop-in replacement for sqlite3 — compatible interface.

Usage:
    from migration.db import get_db, validate_schema

    conn = get_db()
    rows = conn.execute("SELECT * FROM submissions WHERE id=?", (tid,)).fetchall()
    conn.execute("INSERT INTO submissions (...) VALUES (...)", params)
    new_id = conn.execute("SELECT SCOPE_IDENTITY() AS id").fetchone()["id"]
    conn.commit()
    conn.close()
"""

import os
import pyodbc
from collections import OrderedDict

# ── Connection string from environment ──────────────────────────────────
DB_CONNECTION_STRING = os.environ.get(
    "DB_CONNECTION_STRING",
    # Default: local SQL Server with Windows auth
    "Driver={ODBC Driver 18 for SQL Server};"
    "Server=localhost;"
    "Database=GUCFSM;"
    "Trusted_Connection=yes;"
    "TrustServerCertificate=yes;"
)

# ── Row factory — sqlite3.Row compatible ────────────────────────────────

class DictRow(OrderedDict):
    """
    sqlite3.Row compatible row object.
    Supports: row["col"], row.col, row[0] (index), iteration, len().
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._index_map = {col: i for i, col in enumerate(self.keys())}

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(f"DictRow has no attribute or key '{key}'")

    def __getitem__(self, key):
        # Support both string key and integer index
        if isinstance(key, int):
            # Convert integer index to column name
            col_name = list(self.keys())[key]
            return super().__getitem__(col_name)
        return super().__getitem__(key)

    def keys(self):
        return list(super().keys())


# ── Connection (sqlite3-compatible) ─────────────────────────────────────

class Connection:
    """
    sqlite3.Connection-compatible wrapper around pyodbc.
    Mimics: conn.execute(), conn.commit(), conn.close().
    Does NOT auto-commit — caller must call conn.commit() (like sqlite3).
    """
    def __init__(self):
        self._conn = pyodbc.connect(DB_CONNECTION_STRING, autocommit=False, timeout=30)

    def execute(self, sql, params=None):
        """
        Execute a parameterized statement. Returns a CursorWrapper.
        Supports sqlite3 pattern: conn.execute(sql, params).fetchall()
        """
        cursor = self._conn.cursor()
        if params is not None:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        # Convert '?' placeholders for pyodbc (already compatible)
        return CursorWrapper(cursor)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        return False


class CursorWrapper:
    """Wraps pyodbc cursor for sqlite3.Row-compatible dict access."""
    def __init__(self, cursor):
        self._cursor = cursor
        self._description = cursor.description

    def fetchall(self):
        if self._description is None:
            return []
        cols = [col[0] for col in self._description]
        rows = self._cursor.fetchall()
        return [DictRow(zip(cols, row)) for row in rows]

    def fetchone(self):
        if self._description is None:
            return None
        cols = [col[0] for col in self._description]
        row = self._cursor.fetchone()
        if row is None:
            return None
        return DictRow(zip(cols, row))

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid


# ── Connection factory (replaces sqlite3.connect) ───────────────────────

def get_db():
    """Create a new Connection (replaces sqlite3.connect + row_factory)."""
    return Connection()


# ── Schema validation (replaces PRAGMA table_info) ─────────────────────

EXPECTED_SUBMISSIONS_COLUMNS = {
    "id", "telegram_user_id", "phone_number", "unit", "compound",
    "request_type", "category", "service", "sub_service",
    "issue_description", "photo_path", "photo_file_id",
    "status", "priority", "submitted_at",
    "required_approvals", "work_done_by", "work_done_at",
    "work_done_note", "actual_cost",
    "completion_photo_path", "completion_photo_file_id",
    "closed_by", "closed_at", "close_note",
    "cost_estimate", "cost_confirmed",
}


def validate_schema(db_path=None):
    """
    Validate that the submissions table has all expected columns.
    Uses INFORMATION_SCHEMA (not SQLite PRAGMA).
    Prints errors to stderr and returns False if validation fails.
    """
    import sys
    try:
        conn = get_db()
        cursor = conn._conn.cursor()
        cursor.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'submissions'
            ORDER BY ORDINAL_POSITION
        """)
        existing = {row[0] for row in cursor.fetchall()}
        conn.close()

        missing = EXPECTED_SUBMISSIONS_COLUMNS - existing
        if missing:
            print(f"  ✗ DB schema validation FAILED — missing columns: {missing}", file=sys.stderr)
            return False
        print("  ✓ Schema validation passed")
        return True
    except pyodbc.Error as e:
        print(f"  ✗ DB schema validation FAILED: {e}", file=sys.stderr)
        return False


# ── Schema / migration helpers ──────────────────────────────────────────

def execute_sql_file(conn_str, filepath):
    """Execute a .sql file against the database (for schema/seed scripts)."""
    conn = pyodbc.connect(conn_str, autocommit=True, timeout=30)
    cursor = conn.cursor()
    with open(filepath, 'r', encoding='utf-8') as f:
        sql = f.read()
    # Split on GO statements (SQL Server batch separator)
    batches = sql.split('\nGO\n')
    for batch in batches:
        batch = batch.strip()
        if batch:
            cursor.execute(batch)
    conn.close()


def run_migrations():
    """Run schema.sql and seed.sql idempotently."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    sql_dir = os.path.join(base_dir, 'sql')

    print("Running schema migration...")
    execute_sql_file(DB_CONNECTION_STRING, os.path.join(sql_dir, 'schema.sql'))

    print("Running seed data...")
    execute_sql_file(DB_CONNECTION_STRING, os.path.join(sql_dir, 'seed.sql'))

    print("✓ Migration complete.")
