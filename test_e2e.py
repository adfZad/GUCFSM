"""
End-to-end test for GUC Field Service Bot migration.
Tests everything against your local SQL Server.

Run:
    python test_e2e.py

Prerequisites:
    - Local SQL Server running with schema + seed data applied
    - pyodbc, python-telegram-bot installed
"""

import os
import sys

# ── Config ──────────────────────────────────────────────────────────
os.environ["DB_CONNECTION_STRING"] = (
    "Driver={ODBC Driver 18 for SQL Server};"
    "Server=DESKTOP-5MEJ09S;"
    "Database=GUCFSM;"
    "Uid=sa;Pwd=Nokia@7610;"
    "TrustServerCertificate=yes;"
)
os.environ["BOT_TOKEN"] = "test"
os.environ["AGENT_BOT_TOKEN"] = "test"
os.environ["NOTIFICATIONS_ENABLED"] = "false"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "field-service-bot", "app"))

passed = 0
failed = 0


def check(label, actual, expected=None, condition=None):
    global passed, failed
    if condition is not None:
        ok = condition
    else:
        ok = actual == expected
    if ok:
        print(f"  PASS: {label}")
        passed += 1
    else:
        print(f"  FAIL: {label} (got={actual!r}, expected={expected!r})")
        failed += 1


# ══════════════════════════════════════════════════════════════════════
# TEST 1 — Database Connection
# ══════════════════════════════════════════════════════════════════════
print("=" * 50)
print("TEST 1: Database Connection & Schema")
print("=" * 50)

from db import get_db, validate_schema, insert_and_get_id

conn = get_db()

# Verify tables exist
tables = conn.execute(
    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' ORDER BY TABLE_NAME"
).fetchall()
table_names = [t["TABLE_NAME"] for t in tables]
check("agents table exists", "agents" in table_names, True)
check("submissions table exists", "submissions" in table_names, True)
check("services table exists", "services" in table_names, True)
check("conversation_state table exists", "conversation_state" in table_names, True)

# Verify seed data
r = conn.execute("SELECT COUNT(*) AS cnt FROM dbo.agents").fetchone()
check("38 agents seeded", r[0], 38)

r = conn.execute("SELECT COUNT(*) AS cnt FROM dbo.services").fetchone()
check("49 services seeded", r[0], 49)

r = conn.execute("SELECT COUNT(*) AS cnt FROM dbo.master_units").fetchone()
check("7 master_units seeded", r[0], 7)

# Schema validation
ok = validate_schema()
check("validate_schema() passes", ok, True)

conn.close()


# ══════════════════════════════════════════════════════════════════════
# TEST 2 — Full Ticket Lifecycle (DB operations)
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 50)
print("TEST 2: Ticket Lifecycle (INSERT -> UPDATE -> Approval)")
print("=" * 50)

conn = get_db()

# 2a. Submit a new ticket
tid = insert_and_get_id(conn,
    """INSERT INTO dbo.submissions
       (telegram_user_id, phone_number, unit, compound, request_type, category,
        service, sub_service, issue_description, status, priority, required_approvals)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
    ("8580506857", "97450123456", "Diamond Villa 101", "Diamond Compound",
     "New Request", "Maintenance", "Air Conditioning", "AC Not Working",
     "AC not cooling in living room", "submitted", "normal", 2)
)
check("ticket created with ID", tid is not None and tid > 0, True)
print(f"     (Ticket #{tid} created)")

# 2b. Read it back
row = conn.execute("SELECT * FROM dbo.submissions WHERE id=?", (tid,)).fetchone()
check("status is submitted", row["status"], "submitted")
check("unit matches", row["unit"], "Diamond Villa 101")
check("compound matches", row["compound"], "Diamond Compound")
check("service is Air Conditioning", row["service"], "Air Conditioning")
check("sub_service is AC Not Working", row["sub_service"], "AC Not Working")

# 2c. Approver 1 approves
conn.execute(
    "UPDATE dbo.submissions SET status='approved_1' WHERE id=?",
    (tid,)
)
conn.commit()
row = conn.execute("SELECT status FROM dbo.submissions WHERE id=?", (tid,)).fetchone()
check("status -> approved_1", row["status"], "approved_1")

# 2d. Approver 2 approves
conn.execute(
    "UPDATE dbo.submissions SET status='approved' WHERE id=?",
    (tid,)
)
conn.commit()
row = conn.execute("SELECT status FROM dbo.submissions WHERE id=?", (tid,)).fetchone()
check("status -> approved", row["status"], "approved")

# 2e. Field agent marks work done
conn.execute(
    """UPDATE dbo.submissions SET status='closed', work_done_by=?,
       work_done_at=SYSUTCDATETIME(), actual_cost=?, close_note=?
       WHERE id=?""",
    ("8580506857", "350 AED", "Replaced capacitor, re-gassed unit", tid)
)
conn.commit()
row = conn.execute("SELECT status, actual_cost, work_done_by FROM dbo.submissions WHERE id=?", (tid,)).fetchone()
check("status -> closed", row["status"], "closed")
check("actual_cost stored", row["actual_cost"], "350 AED")
check("work_done_by stored", row["work_done_by"], "8580506857")

# 2f. Approval audit trail
conn.execute(
    "INSERT INTO dbo.approvals (submission_id, level, action, actor_id, actor_note) VALUES (?,?,?,?,?)",
    (tid, 1, "approve", "7228949233", "Looks good")
)
conn.execute(
    "INSERT INTO dbo.approvals (submission_id, level, action, actor_id, actor_note) VALUES (?,?,?,?,?)",
    (tid, 2, "approve", "8767995042", "Approved")
)
conn.commit()
rows = conn.execute(
    "SELECT COUNT(*) AS cnt FROM dbo.approvals WHERE submission_id=?", (tid,)
).fetchone()
check("2 approvals recorded", rows[0], 2)

# Cleanup test ticket
conn.execute("DELETE FROM dbo.approvals WHERE submission_id=?", (tid,))
conn.execute("DELETE FROM dbo.submissions WHERE id=?", (tid,))
conn.commit()
print("     (test ticket cleaned up)")

conn.close()


# ══════════════════════════════════════════════════════════════════════
# TEST 3 — State Persistence Layer
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 50)
print("TEST 3: State Persistence (AzureSqlPersistence)")
print("=" * 50)

import asyncio
from persistence import AzureSqlPersistence


async def test_persistence():
    p = AzureSqlPersistence()

    # Store conversation state (simulate partial form fill)
    uid = 8580506857
    await p.update_user_data(uid, {
        "agent_uid": str(uid),
        "compound": "Diamond Compound",
        "unit": "Diamond Villa 101",
        "is_field_agent": True,
        "is_approver": True,
        "approver_roles": ["approver_1", "approver_2"]
    })
    await p.update_conversation("agent_bot", (uid, uid), 7)  # state SERVICE

    # Reload (simulate new Function invocation)
    user_data = await p.get_user_data()
    check("persistence: compound survives", user_data[uid].get("compound"), "Diamond Compound")
    check("persistence: unit survives", user_data[uid].get("unit"), "Diamond Villa 101")

    conv = await p.get_conversations("agent_bot")
    check("persistence: conversation state survives", conv.get((uid, uid)), 7)

    # Simulate progressing the form
    user_data[uid]["service"] = "Air Conditioning"
    await p.update_user_data(uid, user_data[uid])
    await p.update_conversation("agent_bot", (uid, uid), 8)  # state SUB_SERVICE

    # Reload again
    user_data2 = await p.get_user_data()
    check("persistence: service field survives", user_data2[uid].get("service"), "Air Conditioning")
    check("persistence: compound still there", user_data2[uid].get("compound"), "Diamond Compound")

    # Cleanup
    await p.drop_user_data(uid)
    await p.update_conversation("agent_bot", (uid, uid), None)
    await p.flush()

asyncio.run(test_persistence())


# ══════════════════════════════════════════════════════════════════════
# TEST 4 — Bot Code Integrity
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 50)
print("TEST 4: Bot Code Imports & create_application()")
print("=" * 50)

from bot import create_application as create_resident_app
from agent_bot import create_application as create_agent_app

resident_app = create_resident_app()
check("resident create_application() returns Application", "Application" in str(type(resident_app)), True)
check("resident bot has handlers", len(resident_app.handlers) > 0, True)

agent_app = create_agent_app()
check("agent create_application() returns Application", "Application" in str(type(agent_app)), True)
check("agent bot has handlers", len(agent_app.handlers) > 0, True)


# ══════════════════════════════════════════════════════════════════════
# TEST 5 — Webhook Entry Point
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 50)
print("TEST 5: function_app.py Syntax & Structure")
print("=" * 50)

import function_app
check("function_app module loaded", function_app is not None, True)
check("webhook_resident endpoint", "webhook_resident" in dir(function_app), True)
check("webhook_agent endpoint", "webhook_agent" in dir(function_app), True)
check("set_webhooks endpoint", "set_webhooks" in dir(function_app), True)


# ══════════════════════════════════════════════════════════════════════
# TEST 6 — Blob Storage Module
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 50)
print("TEST 6: Blob Storage (local fallback — no Azure Blob configured)")
print("=" * 50)

from blob_storage import upload_photo, get_photo_bytes, delete_photo

# Upload to local fallback
test_data = b"fake-photo-bytes-for-testing"
url = upload_photo(test_data, "test_photo.jpg")
check("photo upload returns path/url", url is not None and len(url) > 0, True)
check("local fallback — saved to disk", os.path.exists(url) if not url.startswith("http") else True, True)

# Read it back
downloaded = get_photo_bytes(url)
check("photo download/read", downloaded, test_data)

# Delete
delete_photo(url)
if not url.startswith("http"):
    check("photo deleted from disk", not os.path.exists(url), True)


# ══════════════════════════════════════════════════════════════════════
# TEST 7 — Service Lookup Queries
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 50)
print("TEST 7: Service Category Queries (bot runtime queries)")
print("=" * 50)

conn = get_db()

# _get_categories equivalent
cats = conn.execute(
    "SELECT DISTINCT category FROM dbo.services WHERE main_category=? ORDER BY category",
    ("Maintenance",)
).fetchall()
cat_names = [c["category"] for c in cats]
check("categories for Maintenance", len(cat_names), 7)
check("Plumbing exists", "Plumbing" in cat_names, True)

# _get_sub_categories equivalent
subs = conn.execute(
    "SELECT sub_category FROM dbo.services WHERE main_category=? AND category=? ORDER BY sub_category",
    ("Maintenance", "Electrical")
).fetchall()
sub_names = [s["sub_category"] for s in subs]
check("sub-categories for Electrical", len(sub_names), 7)
check("Tube light exists", "Tube light" in sub_names, True)

# Facilities categories
fac_cats = conn.execute(
    "SELECT DISTINCT category FROM dbo.services WHERE main_category=? ORDER BY category",
    ("Facilities",)
).fetchall()
check("categories for Facilities", len(fac_cats), 7)

conn.close()


# ══════════════════════════════════════════════════════════════════════
# TEST 8 — Agent Lookup Queries
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 50)
print("TEST 8: Agent & Approver Lookup Queries")
print("=" * 50)

conn = get_db()

# Field agents
agents = conn.execute(
    "SELECT telegram_user_id, name FROM dbo.agents WHERE role='field_agent' AND active=1"
).fetchall()
check("2 active field agents", len(agents), 2)

# Approver_1 for Diamond
a1 = conn.execute(
    "SELECT telegram_user_id FROM dbo.agents WHERE role=? AND compound=? AND active=1",
    ("approver_1", "Diamond Compound")
).fetchall()
check("approver_1 for Diamond Compound", len(a1), 2)  # Riaz + Fasil

# Approver_2 for Sapphire
a2 = conn.execute(
    "SELECT telegram_user_id FROM dbo.agents WHERE role=? AND compound=? AND active=1",
    ("approver_2", "Sapphire Compound")
).fetchall()
check("approver_2 for Sapphire Compound", len(a2), 2)  # Riaz + Shahbaz

conn.close()


# ══════════════════════════════════════════════════════════════════════
# FINAL
# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 50)
print(f"RESULTS: {passed}/{passed + failed} passed")
if failed:
    print(f"FAILED: {failed} test(s)")
    sys.exit(1)
else:
    print("ALL TESTS PASSED!")
    print("")
    print("Your local environment is fully functional.")
    print("Next steps:")
    print("  1. Set real BOT_TOKEN and AGENT_BOT_TOKEN in .env")
    print("  2. Run: cd field-service-bot\\app && python bot.py")
    print("  3. Test with real Telegram bot interactions")
    print("  4. When ready, deploy to Azure (Phase 6+7)")
