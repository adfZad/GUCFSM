"""
Validate the T-SQL schema and seed data against the local SQL Server instance.

Usage:
    # Set connection string, then run:
    $env:DB_CONNECTION_STRING = "Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=field_service;Trusted_Connection=yes;TrustServerCertificate=yes;"
    python migration/validate.py

    # Or for Azure SQL:
    $env:DB_CONNECTION_STRING = "Driver=...;Server=tcp:yourserver.database.windows.net,1433;..."
    python migration/validate.py
"""

import os
import sys

# Add parent to path so we can import db.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import run_migrations, get_connection, query


def validate():
    print("=" * 60)
    print("  GUC Field Service — Schema Validation")
    print("=" * 60)

    # ── 1. Run migrations ─────────────────────────────────────────────
    print("\n>>> Step 1: Schema + Seed")
    run_migrations()

    # ── 2. Verify tables exist ────────────────────────────────────────
    print("\n>>> Step 2: Table Verification")
    with get_connection() as conn:
        tables = query(conn,
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = 'dbo' ORDER BY TABLE_NAME"
        )
        expected = [
            'agents', 'approvals', 'conversation_state', 'form_state',
            'master_units', 'master_units_hierarchy', 'services',
            'submissions', 'unit_agents'
        ]
        found = [t['TABLE_NAME'] for t in tables]
        for tbl in expected:
            status = "✓" if tbl in found else "✗ MISSING"
            print(f"  {status} {tbl}")
        missing = [t for t in expected if t not in found]
        if missing:
            print(f"\n  ERROR: Missing tables: {missing}")
            return False

    # ── 3. Verify row counts ──────────────────────────────────────────
    print("\n>>> Step 3: Row Counts")
    with get_connection() as conn:
        checks = [
            ("agents",       38, "4 users × role/compound combos"),
            ("services",     49, "from services.csv"),
            ("master_units",  7, "from master_data.csv"),
            ("submissions",   0, "empty (fresh DB)"),
            ("approvals",     0, "empty (fresh DB)"),
        ]
        for table, expected_count, desc in checks:
            row = query(conn, f"SELECT COUNT(*) AS cnt FROM dbo.{table}")
            count = row[0]['cnt']
            status = "✓" if count == expected_count else "✗"
            print(f"  {status} {table}: {count} rows (expected {expected_count}) — {desc}")

    # ── 4. Verify seeded agents detail ────────────────────────────────
    print("\n>>> Step 4: Agent Detail Check")
    with get_connection() as conn:
        by_role = query(conn,
            "SELECT role, COUNT(*) AS cnt FROM dbo.agents WHERE active=1 "
            "GROUP BY role ORDER BY role"
        )
        for r in by_role:
            print(f"  {r['role']}: {r['cnt']} active rows")

        # Verify specific users exist
        users = query(conn,
            "SELECT DISTINCT telegram_user_id, name FROM dbo.agents ORDER BY name"
        )
        for u in users:
            print(f"  {u['name']} → TG ID {u['telegram_user_id']}")

    # ── 5. Verify services ────────────────────────────────────────────
    print("\n>>> Step 5: Service Hierarchy")
    with get_connection() as conn:
        categories = query(conn,
            "SELECT main_category, COUNT(DISTINCT category) AS cats, "
            "COUNT(*) AS subcats FROM dbo.services "
            "GROUP BY main_category ORDER BY main_category"
        )
        for c in categories:
            print(f"  {c['main_category']}: {c['cats']} categories, {c['subcats']} sub-categories")

    # ── 6. Verify indexes ─────────────────────────────────────────────
    print("\n>>> Step 6: Indexes")
    expected_idx = [
        'IX_submissions_user_status',
        'IX_submissions_unit_status',
        'IX_agents_user_role',
        'IX_hierarchy_full_label',
        'IX_hierarchy_assigned',
        'IX_approvals_submission',
    ]
    with get_connection() as conn:
        indexes = query(conn,
            "SELECT name FROM sys.indexes WHERE type_desc = 'NONCLUSTERED' ORDER BY name"
        )
        found_idx = [i['name'] for i in indexes]
        for idx in expected_idx:
            status = "✓" if idx in found_idx else "✗ MISSING"
            print(f"  {status} {idx}")

    print("\n" + "=" * 60)
    print("  Validation complete.")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = validate()
    sys.exit(0 if success else 1)
