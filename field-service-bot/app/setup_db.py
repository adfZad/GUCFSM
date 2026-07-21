#!/usr/bin/env python3
"""Initialize the field service bot database and load master data."""

import sqlite3
import csv
import json
import os
import re

DB_PATH = os.environ.get("DB_PATH", "/data/field_service.db")

def normalize_phone(phone):
    """Strip all non-digit characters for consistent lookup."""
    return re.sub(r'\D', '', phone) if phone else ""

# ── Expected column definitions for each table ─────────────────────
EXPECTED_SCHEMA = {
    "master_units": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "phone_number": "TEXT DEFAULT ''",
        "phone_display": "TEXT DEFAULT ''",
        "telegram_user_id": "TEXT DEFAULT ''",
        "owner_name": "TEXT",
        "units": "TEXT NOT NULL",
    },
    "submissions": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "telegram_user_id": "TEXT",
        "phone_number": "TEXT NOT NULL",
        "unit": "TEXT NOT NULL",
        "compound": "TEXT",
        "request_type": "TEXT NOT NULL",
        "category": "TEXT",
        "service": "TEXT",
        "sub_service": "TEXT",
        "issue_description": "TEXT",
        "photo_path": "TEXT",
        "photo_file_id": "TEXT",
        "status": "TEXT DEFAULT 'submitted'",
        "priority": "TEXT DEFAULT 'normal'",
        "submitted_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        # Workflow columns
        "required_approvals": "INTEGER DEFAULT 2",
        "work_done_by": "TEXT",
        "work_done_at": "TIMESTAMP",
        "work_done_note": "TEXT",
        "actual_cost": "TEXT",
        "completion_photo_path": "TEXT",
        "completion_photo_file_id": "TEXT",
        "closed_by": "TEXT",
        "closed_at": "TIMESTAMP",
        "close_note": "TEXT",
        "assigned_technician_id": "TEXT",
        "scheduled_date": "TEXT",
        "inspection_diagnosis": "TEXT",
        "repair_complexity": "TEXT",
        "boq_path": "TEXT",
        "boq_file_id": "TEXT",
        "quality_inspector_id": "TEXT",
        "resident_confirmed": "INTEGER DEFAULT 0",
    },
    "master_units_hierarchy": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "compound": "TEXT NOT NULL",
        "unit_type": "TEXT NOT NULL",
        "villa_number": "TEXT",
        "building_number": "TEXT",
        "flat_number": "TEXT",
        "full_label": "TEXT NOT NULL",
        "assigned_to": "TEXT",
    },
    "unit_agents": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "full_label": "TEXT NOT NULL",
        "telegram_user_id": "TEXT NOT NULL",
    },
    "services": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "main_category": "TEXT NOT NULL",
        "category": "TEXT NOT NULL",
        "sub_category": "TEXT NOT NULL",
    },
    "form_state": {
        "telegram_user_id": "TEXT PRIMARY KEY",
        "current_step": "TEXT NOT NULL",
        "data": "JSON NOT NULL DEFAULT '{}'",
        "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    },
    # agents: one row per (telegram_user_id, role, compound) combination.
    # field_agent rows have compound=NULL; approver rows are scoped per compound.
    "agents": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "telegram_user_id": "TEXT NOT NULL",
        "telegram_username": "TEXT",
        "name": "TEXT NOT NULL",
        "role": "TEXT NOT NULL DEFAULT 'field_agent'",
        "compound": "TEXT",
        "active": "INTEGER DEFAULT 1",
    },
    "approvals": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "submission_id": "INTEGER NOT NULL",
        "level": "INTEGER NOT NULL",
        "action": "TEXT NOT NULL",
        "actor_id": "TEXT NOT NULL",
        "actor_note": "TEXT",
        "acted_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    },
}

# ── Seed data ────────────────────────────────────────────────────────
_COMPOUNDS = [
    "Cascade I", "Cascade II", "Ruby Compound", "Sapphire Compound",
    "Diamond Compound", "Pearl Compound", "Twin Villa", "Ewan Compound", "Najma Flat",
]

SEED_AGENTS = (
    # Field agents (compound = None)
    # Ayaz Siraj covers Pearl Compound + Najma Flat (36 units) — TG ID pending
    [("8976446718", "Afsal Khan", "field_agent", None),
     ("8580506857", "Riaz",       "field_agent", None)]
    # Approver 1: Riaz + Fasil — all 9 compounds
    + [("8580506857", "Riaz",  "approver_1", c) for c in _COMPOUNDS]
    + [("7228949233", "Fasil", "approver_1", c) for c in _COMPOUNDS]
    # Approver 2: Riaz + Shahbaz — all 9 compounds
    + [("8580506857", "Riaz",    "approver_2", c) for c in _COMPOUNDS]
    + [("8767995042", "Shahbaz", "approver_2", c) for c in _COMPOUNDS]
)

# SEED_AGENTS_EXTENDED (with username support)
# Format: (telegram_user_id, telegram_username, name, role, compound)
SEED_AGENTS_EXTENDED = (
    [("", "Edz3399", "Edmondo", "senior_engineer", c) for c in _COMPOUNDS] +
    [("", "Fareed_Mohammed_Farhan", "Farhan", "technician", c) for c in _COMPOUNDS] +
    [("", "karimSubhani", "Karim", "technician", c) for c in _COMPOUNDS] +
    [("", "AP_GUC", "Ayaz", "supervisor", c) for c in _COMPOUNDS] +
    [("", "Afsal9186", "Afsal", "supervisor", c) for c in _COMPOUNDS] +
    [("", "Tauseefahmadguc", "Tauseef", "facility_manager", c) for c in _COMPOUNDS]
)


def _migrate_agents_table(conn):
    """
    If agents table exists with old schema (telegram_user_id as PRIMARY KEY,
    no id column), drop and recreate it. Safe only when the table is empty,
    which is always the case before first use of the approver workflow.
    """
    rows = conn.execute("PRAGMA table_info(agents)").fetchall()
    if not rows:
        return  # Table doesn't exist yet — ensure_schema will create it
    col_names = {r[1] for r in rows}
    if "id" in col_names:
        return  # Already new schema
    count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    if count > 0:
        print("  ⚠️  agents table has old schema but is not empty — skipping migration")
        return
    conn.execute("DROP TABLE agents")
    conn.commit()
    print("  ⚡ Dropped old agents table (will recreate with new schema)")


def ensure_schema(conn):
    """
    Idempotently ensure all tables and columns exist.
    Creates missing tables, adds missing columns via ALTER TABLE.
    Safe to run on every startup — never drops data.
    """
    _migrate_agents_table(conn)

    for table, columns in EXPECTED_SCHEMA.items():
        col_defs = ", ".join(f"{name} {defn}" for name, defn in columns.items())
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({col_defs})")

        existing = {
            row[1] for row in
            conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for col_name, col_def in columns.items():
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
                print(f"  ⚡ Migrated: {table}.{col_name} {col_def}")

    conn.commit()


def create_tables(conn):
    """Create all tables (backward-compat wrapper — calls ensure_schema)."""
    ensure_schema(conn)


def seed_agents(conn):
    """Insert seed agents idempotently."""
    for tg_id, name, role, compound in SEED_AGENTS:
        count = conn.execute("SELECT COUNT(*) FROM agents WHERE telegram_user_id=? AND role=? AND (compound=? OR (compound IS NULL AND ? IS NULL))", (tg_id, role, compound, compound)).fetchone()[0]
        if count == 0:
            conn.execute(
                "INSERT INTO agents (telegram_user_id, name, role, compound, active) VALUES (?,?,?,?,1)",
                (tg_id, name, role, compound)
            )
    for tg_id, username, name, role, compound in SEED_AGENTS_EXTENDED:
        count = conn.execute("SELECT COUNT(*) FROM agents WHERE telegram_username=? AND role=? AND (compound=? OR (compound IS NULL AND ? IS NULL))", (username, role, compound, compound)).fetchone()[0]
        if count == 0:
            conn.execute(
                "INSERT INTO agents (telegram_user_id, telegram_username, name, role, compound, active) VALUES (?,?,?,?,?,1)",
                (tg_id, username, name, role, compound)
            )
    conn.commit()
    print("  ✓ Seeded agents")


def load_services(conn, csv_path):
    """Load 3-level service hierarchy from CSV. Idempotent — skips if table already has rows."""
    count = conn.execute("SELECT COUNT(*) FROM services").fetchone()[0]
    if count > 0:
        print(f"  ℹ️  services table already has {count} rows — skipping seed")
        return
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            conn.execute(
                "INSERT INTO services (main_category, category, sub_category) VALUES (?,?,?)",
                (row["main_category"], row["category"], row["sub_category"])
            )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM services").fetchone()[0]
    print(f"  ✓ Seeded {count} service rows from services.csv")


def load_master_data(conn, csv_path):
    """Load phone→units mapping from CSV. Normalizes phone numbers."""
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_phone = row.get("phone_number", "")
            digits = normalize_phone(raw_phone)
            tg_id = row.get("telegram_user_id", "").strip()
            conn.execute(
                "INSERT INTO master_units (phone_number, phone_display, telegram_user_id, owner_name, units) VALUES (?, ?, ?, ?, ?)",
                (digits, raw_phone, tg_id, row.get("owner_name", ""), row["units"])
            )
    conn.commit()


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    create_tables(conn)
    seed_agents(conn)

    csv_path = os.path.join(os.path.dirname(__file__), "master_data.csv")
    if os.path.exists(csv_path):
        load_master_data(conn, csv_path)
        count = conn.execute("SELECT COUNT(*) FROM master_units").fetchone()[0]
        print(f"✓ Database initialized at {DB_PATH}")
        print(f"✓ Loaded {count} master records from master_data.csv")
    else:
        print(f"✓ Database initialized at {DB_PATH}")
        print(f"⚠ No master_data.csv found")

    svc_csv = os.path.join(os.path.dirname(__file__), "services.csv")
    if os.path.exists(svc_csv):
        load_services(conn, svc_csv)
    else:
        print(f"⚠ No services.csv found — service hierarchy will be empty")

    conn.close()
