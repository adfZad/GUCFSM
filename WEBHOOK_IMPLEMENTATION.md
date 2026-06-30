# Webhook Migration — Implementation Subtasks

**Goal:** Replace long-polling with webhook architecture — Azure Functions + Azure SQL
**Total estimated effort:** 20–23 working days (1 team, single-track)

---

## Effort Summary

| Phase | Title | Days | Cumulative |
|-------|-------|------|------------|
| 0 | Foundation & Setup + Resolve Open Questions | 2 | 2 |
| 1 | Migration to Azure SQL Database | 4 | 6 |
| 2 | State Persistence Layer | 3 | 9 |
| 3 | Webhook Entry Point (Azure Function) | 5 | 14 |
| 4 | Photo Storage (Blob Migration) | 2 | 16 |
| 5 | Notification Refactor | 2 | 18 |
| 6 | Infrastructure Cleanup + Deployment Scripts | 1 | 19 |
| 7 | Testing + Go-Live | 3–4 | 22–23 |


---

> **Prerequisite:** Resolve all open questions in `WEBHOOK_OPEN_QUESTIONS.md` before starting implementation.

## Phase 0 — Foundation & Setup ⏱️ 2 days

### 0.1 Resolve Open Questions (Day 1 AM)
- [ ] Finalize all 20 decisions in `WEBHOOK_OPEN_QUESTIONS.md`
- [ ] Confirm Azure SQL tier, Function plan, photo storage strategy
- [ ] Decide data migration vs fresh start

### 0.2 Provision Azure Resources (Day 1 PM – Day 2)
- [ ] Create Azure Resource Group
- [ ] Create Azure Resource Group
- [ ] Provision Azure SQL Database (Basic or Standard tier)
- [ ] Provision Azure Function App (Consumption or Premium plan)
- [ ] Create Azure Blob Storage account (for photos)
- [ ] Create Application Insights instance (for logging)
- [ ] Note: all connection strings and endpoints

### 0.2 Resolve Open Questions
_(see WEBHOOK_OPEN_QUESTIONS.md for details)_
- [ ] Select Function runtime (Python model v1 vs v2)
- [ ] Choose single vs split function endpoints
- [ ] Decide on secret token for webhook verification
- [ ] Confirm Azure SQL tier and connection approach
- [ ] Decide photo storage strategy (Blob vs DB)
- [ ] Decide: migrate existing data or start fresh?
- [ ] Confirm deployment method (CI/CD vs manual)

---

## Phase 1 — Migration to Azure SQL Database ⏱️ 4 days

### 1.1 Create Schema Conversion Script (Day 1) ✅ Done
- [x] Convert all 8 tables + 1 new from SQLite to T-SQL syntax (`migration/sql/schema.sql`)
  - [x] `master_units`, `submissions`, `master_units_hierarchy`, `services`, `agents`, `approvals`, `form_state`, `unit_agents`
  - [x] New: `conversation_state` (for Phase 2 persistence)
- [x] Replace `INTEGER PRIMARY KEY AUTOINCREMENT` → `INT IDENTITY(1,1) PRIMARY KEY`
- [x] Replace `datetime('now')` defaults → `SYSUTCDATETIME()`
- [x] Create 6 indexes for hot query paths
- [x] Idempotent: `IF OBJECT_ID(...) IS NULL CREATE TABLE` — safe to re-run
- [x] Type mapping: `TEXT` → `NVARCHAR(n)`, `JSON` → `NVARCHAR(MAX)`, `active` → `BIT`

### 1.2 Create Seed Data Script (Day 2 AM) ✅ Done
- [x] Port `seed_agents()` → T-SQL with `IF NOT EXISTS` guard (`migration/sql/seed.sql`)
- [x] Port `load_master_data()` → 7 rows from master_data.csv
- [x] Port `load_services()` → 49 rows from services.csv
- [x] Validate: 38 agent rows, 4 users across 9 compounds

### 1.3 Port All SQL Queries — Bot Code (Day 2 PM – Day 3) ✅ Done
- [x] Replaced `import sqlite3` → `from db import get_db, validate_schema` in both bots
- [x] Replaced `def db()` helper → returns `get_db()` (Connection wrapper)
- [x] Removed `conn.row_factory = sqlite3.Row` (DictRow handles this now)
- [x] Replaced `SELECT last_insert_rowid()` → `SELECT SCOPE_IDENTITY() AS id` in both `write_submission()` functions
- [x] Replaced `datetime('now')` → `SYSUTCDATETIME()` in agent_bot's work-done UPDATE
- [x] Replaced `PRAGMA table_info(submissions)` → `validate_schema()` using INFORMATION_SCHEMA
- [x] Replaced `sqlite3.OperationalError` → `pyodbc.Error` with `validate_schema()` wrapper
- [x] Verified: zero `sqlite3` references remain in `bot.py` and `agent_bot.py`
- [ ] Pending: live test against local SQL Server instance

### 1.4 Create Database Connection Module (Day 4) ✅ Done
- [x] Write `migration/db.py` — pyodbc-based connection factory + query helpers
- [x] `Connection` class mimics `sqlite3` interface: `.execute()`, `.commit()`, `.close()`
- [x] `DictRow` class provides `sqlite3.Row`-compatible dict+attribute access
- [x] `insert_and_get_id()` replaces `cursor.lastrowid` (uses `SCOPE_IDENTITY()`)
- [x] `run_migrations()` — executes `schema.sql` + `seed.sql` idempotently
- [x] `migration/validate.py` — verifies tables, row counts, indexes, services
- [x] `migration/requirements.txt` — `pyodbc>=5.0`

---

## Phase 2 — State Persistence Layer ⏱️ 3 days

### 2.1 Implement Custom `BasePersistence` (Days 1–2)
- [ ] Create `persistence.py` with subclass of `BasePersistence`
- [ ] `store_user_data(user_id, data)` → upsert into `form_state` (or new `conversation_state` table)
- [ ] `load_user_data(user_id)` → fetch from DB
- [ ] `store_chat_data(chat_id, data)` → same pattern
- [ ] `load_chat_data(chat_id)` → same
- [ ] `store_bot_data(data)` → same
- [ ] `load_bot_data()` → same
- [ ] `store_conversations(data)` → same
- [ ] `load_conversations()` → same
- [ ] Use JSON serialization for all state blobs
- [ ] Add `refresh_user_data`, `refresh_chat_data` stubs
- [ ] Implement `store_callback` (call-on-write sync to DB)

### 2.2 Create State Storage Table (Day 2 PM)
- [ ] Script to create `conversation_state` table:
  ```sql
  CREATE TABLE conversation_state (
      entity_type NVARCHAR(10) NOT NULL,  -- 'user', 'chat', 'bot', 'conv'
      entity_id NVARCHAR(255) NOT NULL,
      data NVARCHAR(MAX) NOT NULL,  -- JSON
      updated_at DATETIME2 DEFAULT SYSUTCDATETIME(),
      PRIMARY KEY (entity_type, entity_id)
  )
  ```
- [ ] Add TTL / cleanup logic (optional, configurable)

### 2.3 Test Persistence (Day 3)
- [ ] Simulate multi-step conversation — confirm state survives round-trips
- [ ] Test concurrent user isolation (user A and user B don't interfere)

---

## Phase 3 — Webhook Entry Point (Azure Function) ⏱️ 5 days

### 3.1 Create Azure Function App Skeleton (Day 1)
- [ ] `function_app.py` — `app = func.FunctionApp()`
- [ ] HTTP trigger: `POST /api/webhook/resident`
- [ ] HTTP trigger: `POST /api/webhook/agent`
- [ ] Register `setWebhook` admin endpoint (or deploy script)

### 3.2 Refactor Bot Boot Sequence (Day 2)
- [ ] Extract `main()` from `bot.py` and `agent_bot.py`
- [ ] New pattern: `create_application(token, persistence) → Application`
- [ ] Add `async def handle_webhook(update_json: dict) → None`:
  ```python
  # Inside Azure Function handler:
  app = create_application(token, persistence)
  async with app:
      await app.process_update(
          Update.de_json(json.loads(webhook_body), app.bot)
      )
  ```
- [ ] Wire up `setWebhook` on start:
  ```python
  await app.bot.set_webhook(
      url=f"{AZURE_FUNC_URL}/api/webhook/{bot_name}",
      secret_token=WEBHOOK_SECRET,
      allowed_updates=Update.ALL_TYPES
  )
  ```

### 3.3 Handle Function Lifecycle (Day 3)
- [ ] Warm-up: create application once, cache in global/static variable
- [ ] Cold start: lazy-init the application on first invocation
- [ ] Graceful shutdown: delete webhook on function stop

### 3.4 Webhook Security (Day 4 AM)
- [ ] Validate `X-Telegram-Bot-Api-Secret-Token` header on every request
- [ ] Reject requests without valid secret (return 403)
- [ ] Return 200 OK promptly — Telegram expects fast ack

### 3.5 Wire Everything Together & Test End-to-End (Day 4 PM – Day 5)
- [ ] Connect function handler → bot application → persistence → Azure SQL
- [ ] Run live webhook test from Telegram (use test bot or setWebhook to dev URL)
- [ ] Replace `sqlite3.connect(db_path)` → `get_connection()` from `db.py`
- [ ] Replace `?` placeholders → `?` (both work for pyodbc, but if using pymssql, use `%(name)s`)
- [ ] Replace SQLite-specific functions:
  - `datetime('now')` → `SYSUTCDATETIME()`
  - `COALESCE` → same (works in T-SQL)
  - `||` string concat → `CONCAT()` or `+`
- [ ] Test every `conn.execute()` path in both bots

---

## Phase 4 — Photo Storage (Blob Migration) ⏱️ 2 days

### 4.0 Prerequisite: Azure Blob Storage container created in Phase 0

### 4.1 Create Blob Utility Module (Day 1 AM)
- [ ] `photo_storage.py` with:
  - `upload_photo(file_bytes, blob_name) → url`
  - `download_photo(blob_name) → bytes` (if needed)
  - `delete_photo(blob_name)` (for cleanup)

### 4.2 Update Photo Handlers (Day 1 PM – Day 2)
- [ ] `resident bot`: download photo from Telegram → upload to Blob → store Blob URL
- [ ] `agent bot`: same flow
- [ ] Store Blob URL in `submissions.photo_path` / `completion_photo_path`
- [ ] Fallback: if Blob upload fails, store `photo_file_id` only (retry later)

### 4.3 Update View/Display Logic (Day 2)
- [ ] All photo display code: replace file-path send with URL-based send
- [ ] `completion_photo_handler`: download from Telegram → Blob → confirm screen

---

## Phase 5 — Notification Refactor ⏱️ 2 days

### 5.1 Inline Notifications — Verify (Day 1 AM)
- [ ] Confirm `bot.send_message()` works inside Azure Function context
- [ ] Test with a live notification trigger (e.g., new submission)
- [ ] `_notify()` calls `bot.send_message()` — works within Azure Function context
- [ ] No change needed for synchronous notifications

### 5.2 Queue-Based Notifications — If Required (Days 1 PM – 2)
- [ ] Decision gate: skip if inline works reliably
- [ ] Create Azure Queue Storage client
- [ ] Replace direct `_notify()` calls with queue message enqueue
- [ ] Create Queue-triggered Function: dequeue → `send_message()`
- [ ] Enables retry, throttling, and background processing

---

## Phase 6 — Infrastructure Cleanup ⏱️ 1 day

### 6.1 Remove Old Deployment Artifacts
- [ ] Remove `Dockerfile` (no longer needed)
- [ ] Remove `docker-compose.yml`
- [ ] Remove `supervisord.conf`
- [ ] Remove `logs/` directory
- [ ] Retain `app/` code — refactored into Azure Functions
- [ ] Archive `data/` (migration source only)

### 6.2 Create Deployment Scripts
- [ ] `deploy.ps1` / `deploy.sh` — publish Function App
- [ ] `deploy-infra.ps1` — ARM/Bicep template for Azure resources
- [ ] GitHub Actions / AzDO pipeline YAML for CI/CD

---

## Phase 7 — Testing + Go-Live ⏱️ 4 days

### 7.1 Unit Tests (Day 1)
- [ ] Test persistence layer (round-trip store/load)
- [ ] Test webhook signature validation
- [ ] Test database connection failure handling
- [ ] Test photo upload failure fallback
- [ ] Test conversation state isolation

### 7.2 Integration Tests (Day 2)
- [ ] Test full ticket lifecycle via webhook (submit → approve → work → close)
- [ ] Test emergency request flow
- [ ] Test follow-up flow
- [ ] Test reject → re-submit flow
- [ ] Test cold start behavior (no pre-warmed instances)
- [ ] Test concurrent users

### 7.3 Data Migration Validation & Staging Deploy (Day 3)
- [ ] Run migration script against existing SQLite dump
- [ ] Verify row counts match between SQLite and Azure SQL
- [ ] Verify conversation state not lost for in-progress tickets
- [ ] Rollback test: confirm Azure SQL can be re-seeded from scratch

---

### 7.4 Production Go-Live (Day 4)

- [ ] Deploy to production Azure environment
- [ ] Run `setWebhook` (flips from polling to webhook atomically)
- [ ] Monitor logs for 15 min — verify no dropped updates
- [ ] Rollback plan ready: `deleteWebhook` → revert to `run_polling()` on old infra
- [ ] Keep old Docker container running for 48 h after cutover
- [ ] Keep old SQLite DB backup for 7 days
- [ ] Set up Application Insights alerts for Function failures
- [ ] Set up Azure SQL DTU usage alert
- [ ] Set up webhook 4xx/5xx rate alert
