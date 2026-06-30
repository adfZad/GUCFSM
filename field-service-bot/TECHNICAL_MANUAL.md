# GUC Field Service Bot — Technical Manual

**Last updated:** 2026-06-04  
**Stack:** Python 3, python-telegram-bot 22.x, SQLite 3, Docker, supervisord  
**Bots:** @GUCMain1bot (resident bot) · @GUCMain2bot (agent bot)

---

## 1. Architecture Overview

```
Host: /opt/field-service-bot/
│
├── Docker container: field-service-bot
│   ├── supervisord
│   │   ├── resident-bot  →  python3 /app/bot.py
│   │   └── agent-bot     →  python3 /app/agent_bot.py
│   ├── /app/   (bind-mount ← ./app/)
│   ├── /data/  (bind-mount ← ./data/)   SQLite DB + photos
│   └── /logs/  (bind-mount ← ./logs/)
│
├── Telegram API  ←→  both bots poll via long-polling
└── No external services, no LLM, no queues
```

Both bots are pure deterministic state machines. They share one SQLite database. Neither bot knows about the other; coordination happens entirely through DB state changes and Telegram push notifications sent inline during request handling.

### Key design principle

Every conversation is a `ConversationHandler` with explicit integer states. A handler receives a callback or message, writes to `context.user_data` (in-memory, per-user), and returns the next state integer. DB writes happen only at submission/approval/closure — never during navigation.

---

## 2. Infrastructure

### docker-compose.yml

```yaml
services:
  field-service-bot:
    build: .
    container_name: field-service-bot
    env_file: .env
    volumes:
      - ./app:/app      # code — edit on host, restart to apply
      - ./data:/data    # DB + photos — persists across rebuilds
      - ./logs:/logs    # log files
    restart: unless-stopped
```

### supervisord.conf

Two programs inside one container. Both auto-restart on crash (5 s grace period before considering it started).

```ini
[program:resident-bot]
command=python3 /app/bot.py
autorestart=true
startsecs=5

[program:agent-bot]
command=python3 /app/agent_bot.py
autorestart=true
startsecs=5
```

### Environment variables (.env)

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | — | Resident bot Telegram token |
| `AGENT_BOT_TOKEN` | — | Agent bot Telegram token |
| `DB_PATH` | `/data/field_service.db` | SQLite path |
| `PHOTO_DIR` | `/data/photos` | Uploaded image storage |
| `LOG_DIR` | `/logs` | Log file directory |

Token fallback: if env var is empty, bots read `.bot_token` / `.agent_bot_token` from the script directory.

### Logging

Both bots use `RotatingFileHandler` — 5 MB per file, 5 backups. Logs also stream to stdout (captured by supervisord). Log paths: `/logs/bot.log`, `/logs/agent_bot.log`.

---

## 3. Database Schema

Single SQLite file at `$DB_PATH`. All schema management is in `setup_db.py::ensure_schema()` which is idempotent — safe to re-run at any time without data loss.

### 3.1 `master_units`

Resident bot lookup table. Maps phone numbers and Telegram user IDs to unit lists.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto |
| phone_number | TEXT | Digits-only (normalized from display format) |
| phone_display | TEXT | Original format as entered in CSV |
| telegram_user_id | TEXT | Optional — enables instant lookup without phone |
| owner_name | TEXT | Display name |
| units | TEXT | JSON array of unit label strings, e.g. `["Villa 12", "Villa 13"]` |

**Populated by:** `load_master_data()` reading `master_data.csv`. Additive — re-running appends rows.  
**Used by:** `bot.py` only.

### 3.2 `master_units_hierarchy`

Agent bot unit structure. Each row is one unit (villa or apartment flat) with its compound and field agent assignment.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto |
| compound | TEXT NOT NULL | e.g. "Diamond Compound" |
| unit_type | TEXT NOT NULL | "Villa" or "Apartment" |
| villa_number | TEXT | Villas only (e.g. "V-101") |
| building_number | TEXT | Apartments only (e.g. "Building A") |
| flat_number | TEXT | Apartments only (e.g. "F-201") |
| full_label | TEXT NOT NULL | Display label used as FK in submissions (e.g. "Diamond Villa 101") |
| assigned_to | TEXT | Telegram user ID of the assigned field agent |

**Populated by:** Manual SQL INSERT or bulk CSV import (not yet automated).  
**Used by:** `agent_bot.py` for unit drill-down, compound detection, and field-agent-to-unit mapping.  
**Critical:** `full_label` is stored verbatim in `submissions.unit`. Notification lookups (`WHERE full_label=?`) depend on exact string match.

### 3.3 `submissions`

The central workflow table. Written by both bots.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Ticket number shown to users |
| telegram_user_id | TEXT | Submitter's TG user ID |
| phone_number | TEXT | Resident's phone (digits-only) |
| unit | TEXT | Selected unit `full_label` |
| compound | TEXT | Set by agent bot from `master_units_hierarchy`; NULL for resident bot tickets |
| request_type | TEXT | "New Request" / "Follow Up" / "Emergency" |
| category | TEXT | "Maintenance" / "Facilities" / "Emergency" / "Follow Up" |
| service | TEXT | Level-2 category (e.g. "Air Conditioning") |
| sub_service | TEXT | Level-3 sub-category (e.g. "AC Not Working") |
| issue_description | TEXT | Free-text from user (5–500 chars) |
| photo_path | TEXT | Filesystem path to initial photo |
| photo_file_id | TEXT | Telegram file ID for initial photo |
| status | TEXT | Workflow state (see below) |
| priority | TEXT | "normal" / "high" (high = emergency) |
| submitted_at | TIMESTAMP | Auto-set by SQLite |
| required_approvals | INTEGER | Always 2 (set at INSERT) |
| work_done_by | TEXT | Agent TG user ID who completed the work |
| work_done_at | TIMESTAMP | Completion timestamp |
| work_done_note | TEXT | Agent's completion note (unused in current flow — superseded by actual_cost/photo) |
| actual_cost | TEXT | Cost entered by field agent at work completion |
| completion_photo_path | TEXT | Filesystem path to completion photo |
| completion_photo_file_id | TEXT | Telegram file ID for completion photo |
| closed_by | TEXT | Legacy — not written in current flow |
| closed_at | TIMESTAMP | Legacy — not written in current flow |
| close_note | TEXT | Legacy — not written in current flow |

**Status lifecycle:**

```
submitted
    → [Approver 1 approves]  → approved_1
    → [Approver 2 approves]  → approved      (field agent notified, work can begin)
    → [Field agent completes, cost + photo] → closed

At any point:
    → [Any approver rejects] → rejected
    → [Field agent re-submits] → submitted   (re-enters chain)
```

### 3.4 `services`

3-level service hierarchy. Loaded once from `services.csv`.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto |
| main_category | TEXT NOT NULL | "Maintenance" or "Facilities" |
| category | TEXT NOT NULL | Level-2 (e.g. "Air Conditioning", "Carpentry") |
| sub_category | TEXT NOT NULL | Level-3 (e.g. "AC Not Working", "Curtain") |

**Current data:** 49 rows — Maintenance: 7 categories / 31 sub-categories; Facilities: 7 categories / 18 sub-categories.  
**Populated by:** `load_services()` reading `services.csv` — skips if table already has rows (idempotent).  
**To add/change services:** Edit `services.csv`, then `DELETE FROM services;` in SQLite, then re-run `setup_db.py`.

### 3.5 `agents`

Role registry. One row per (telegram_user_id, role, compound) combination. A person holding multiple roles or covering multiple compounds has multiple rows.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto |
| telegram_user_id | TEXT NOT NULL | TG user ID |
| name | TEXT NOT NULL | Display name |
| role | TEXT NOT NULL | "field_agent" / "approver_1" / "approver_2" |
| compound | TEXT | NULL for field_agent; compound name for approvers |
| active | INTEGER | 1 = active, 0 = suspended |

**field_agent** rows: `compound = NULL`. The agent can submit tickets for any unit assigned to them in `master_units_hierarchy`.  
**approver_1 / approver_2** rows: one row per compound. Approver routing queries `WHERE role=? AND compound=? AND active=1`.

**Populated by:** `seed_agents()` on first run (only if table is empty). Subsequent management is direct SQL.

**Current people:**

| Name | Role(s) |
|---|---|
| Afsal Khan | field_agent |
| Riaz | field_agent + approver_1 (all compounds) + approver_2 (all compounds) |
| Fasil | approver_1 (all compounds) |
| Shahbaz | approver_2 (all compounds) |

### 3.6 `approvals`

Immutable audit trail. One row per approval/rejection action.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto |
| submission_id | INTEGER NOT NULL | FK → submissions.id |
| level | INTEGER NOT NULL | 1 or 2 |
| action | TEXT NOT NULL | "approve" or "reject" |
| actor_id | TEXT NOT NULL | Approver's TG user ID |
| actor_note | TEXT | Optional note (required for rejections, min 5 chars) |
| acted_at | TIMESTAMP | Auto-set by SQLite |

### 3.7 `form_state`

Reserved. Exists in schema; never written at runtime. Both bots use `context.user_data` (in-memory) for conversation state.

---

## 4. setup_db.py — Functions

### `normalize_phone(phone: str) → str`
Strips all non-digit characters. Used to normalize phone input for consistent lookup.

### `ensure_schema(conn) → None`
Idempotent schema manager. For each table in `EXPECTED_SCHEMA`:
1. `CREATE TABLE IF NOT EXISTS` with all columns
2. `PRAGMA table_info()` to find existing columns
3. `ALTER TABLE ADD COLUMN` for any missing column

Never drops columns or tables. Safe to run on every startup.

### `_migrate_agents_table(conn) → None`
One-time migration: if `agents` table exists without an `id` column (old schema), drops and recreates it — but only if empty. Logs a warning and skips if the table has data.

### `seed_agents(conn) → None`
Inserts `SEED_AGENTS` tuples if `agents` table is empty. `SEED_AGENTS` is a hardcoded list of (tg_id, name, role, compound) entries covering the initial 4 people. Idempotent — does nothing if any rows exist.

### `load_master_data(conn, csv_path) → None`
Reads `master_data.csv` (columns: `phone_number`, `telegram_user_id`, `owner_name`, `units`). Normalizes phone via `normalize_phone()`. Inserts one row per CSV line into `master_units`. **Additive** — calling twice produces duplicates; run only on fresh DB or after clearing `master_units`.

### `load_services(conn, csv_path) → None`
Reads `services.csv` (columns: `main_category`, `category`, `sub_category`). Inserts all rows into `services`. Skips entirely if `services` already has rows.

### `__main__` block
Execution order:
1. `ensure_schema()` — create/migrate all tables
2. `seed_agents()` — seed agents if empty
3. `load_master_data()` — from `master_data.csv` if present
4. `load_services()` — from `services.csv` if present

---

## 5. Resident Bot (bot.py)

**Bot:** @GUCMain1bot  
**Token env:** `BOT_TOKEN`  
**Users:** Unit owners / residents

### 5.1 Conversation States

```python
PHONE = 0           # Phone number text entry
UNITS = 1           # Unit selection (inline keyboard)
REQUEST_TYPE = 2    # New / Follow Up / Emergency
CATEGORY = 3        # Maintenance / Facilities
SERVICE = 4         # Level-2 category (DB-driven)
FACILITY_SERVICE = 5  # Unused — kept for index stability
DESCRIPTION = 6     # Free-text issue description
PHOTO = 7           # Optional photo upload
CONFIRM = 8         # Summary + submit
FOLLOWUP_ID = 9     # Text entry: existing ticket number
FOLLOWUP_STATUS = 10  # Status selection from FOLLOWUP_STATUSES
FOLLOWUP_NOTE = 11  # Text: follow-up note
EMERGENCY_DESC = 12   # Text: emergency description
EMERGENCY_PHOTO = 13  # Optional emergency photo
EMERGENCY_CONFIRM = 14  # Emergency confirm + submit
SUB_SERVICE = 15    # Level-3 sub-category (DB-driven)
```

### 5.2 Entry

**Handler:** `start(update, context)`  
**Triggers:** `/start`, any text message (entry_points)  
**Logic:**
1. Clears `context.user_data` and `form_state` table for this user
2. Queries `master_units WHERE telegram_user_id=?`
3. If found → loads units into `user_data`, shows unit selection → returns `UNITS`
4. If not found → prompts for phone number → returns `PHONE`

### 5.3 Phone Validation

**Handler:** `phone_handler(update, context)`  
**State:** `PHONE`  
**DB call:** `SELECT FROM master_units WHERE phone_number=?`  
- Validates 8–15 digit length
- Looks up normalized digits in `master_units`
- On match: loads `owner_name`, `units` into `user_data` → `UNITS`
- On miss: re-prompts

### 5.4 Unit Selection

**Handler:** `unit_handler(update, context)`  
**State:** `UNITS`  
**Callback:** `unit:N` (index into `user_data["units"]` list)  
Stores selected unit label → shows request type keyboard → `REQUEST_TYPE`

### 5.5 Request Type

**Handler:** `request_type_handler(update, context)`  
**State:** `REQUEST_TYPE`  
**Callbacks:** `type:new` / `type:followup` / `type:emergency`

| Choice | Next state | Sets |
|---|---|---|
| New Request | `CATEGORY` | `request_type = "New Request"` |
| Follow Up | `FOLLOWUP_ID` | `request_type = "Follow Up"` |
| Emergency | `EMERGENCY_DESC` | `request_type = "Report Emergency"` |

### 5.6 New Request Flow

**`category_handler`** — State `CATEGORY`  
Callback: `cat:maintenance` or `cat:facilities`  
- Sets `category` and `main_category` in `user_data`
- Queries `_get_categories(main_category)` → DB: `SELECT DISTINCT category FROM services WHERE main_category=? ORDER BY category`
- Builds inline keyboard with `svc:{category_name}` callbacks → `SERVICE`

**`service_handler`** — State `SERVICE`  
Callback: `svc:{category_name}` or `back:cat`  
- Stores `service = category_name`
- Queries `_get_sub_categories(main_category, category)` → DB: `SELECT sub_category FROM services WHERE main_category=? AND category=? ORDER BY sub_category`
- Builds keyboard with `subsvc:{sub}` callbacks → `SUB_SERVICE`

**`sub_service_handler`** — State `SUB_SERVICE`  
Callback: `subsvc:{sub_category}` or `back:service`  
- Stores `sub_service = sub_category`
- Sets `_back_target = "sub_service"` for text-back nav
- Shows description prompt with back button → `DESCRIPTION`

**`description_handler`** — State `DESCRIPTION`  
Text message handler (min 5 chars). Stores description → shows photo prompt → `PHOTO`

**`photo_handler`** / **`photo_skip_handler`** — State `PHOTO`  
Photo: downloads to `$PHOTO_DIR/{uid}_{ts}_{hex}.jpg`, stores path + file_id.  
Skip: sets path/file_id to None.  
Both → `show_summary()` → `CONFIRM`

**`confirm_handler`** — State `CONFIRM`  
Callback `confirm:yes` → calls `write_submission(is_emergency=False)`  
Callback `confirm:no` → clears user_data, ends conversation  
Callback `confirm:back_photo` → back to description

### 5.7 `write_submission(update, context, is_emergency)`

**DB write:**
```sql
INSERT INTO submissions
  (telegram_user_id, phone_number, unit, request_type, category,
   service, sub_service, issue_description, photo_path, photo_file_id, priority)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

**Notification:** Calls `_get_field_agents_for_unit(unit)` — `SELECT DISTINCT assigned_to FROM master_units_hierarchy WHERE full_label=? AND assigned_to IS NOT NULL` — then `_notify()` each agent.  

Note: Resident bot unit labels (e.g. "Villa 12") may not match `full_label` values in `master_units_hierarchy` (e.g. "Diamond Villa 101") — notifications silently find no agents in this case.

### 5.8 Follow-Up Flow

**`followup_id_handler`** — State `FOLLOWUP_ID`  
Text: numeric ticket ID.  
DB: `SELECT FROM submissions WHERE id=?`  
Validates: unit must be in `user_data["units"]` (resident can only follow up on their own units).  
→ `FOLLOWUP_STATUS`

**`followup_status_handler`** — State `FOLLOWUP_STATUS`  
Callback from `FOLLOWUP_STATUSES` list (index-based). → `FOLLOWUP_NOTE`

**`followup_note_handler`** — State `FOLLOWUP_NOTE`  
Text min 5 chars. Builds a compound `description` string:  
`"[Follow-up #{id}] Status: {status} — {note}"`  
→ `PHOTO`

### 5.9 Emergency Flow

**`emergency_desc_handler`** → **`emergency_photo_handler`** / **`emergency_photo_skip`** → **`show_emergency_confirm`** → **`emergency_confirm_handler`** → `write_submission(is_emergency=True)`

Sets `category = "Emergency"`, `service = "Emergency"`, `priority = "high"`.

### 5.10 Back Navigation

Bot.py has a rich back-navigation system. Key helpers:

| Function | Goes back to |
|---|---|
| `back_to_units(query, context)` | Unit selection (UNITS) |
| `back_to_request_type(query, context)` | Request type (REQUEST_TYPE) |
| `back_to_category(query, context)` | Category selection (CATEGORY) |
| `back_to_sub_service(query, context)` | Sub-category selection (SUB_SERVICE) |
| `back_to_description(query, context)` | Description prompt (DESCRIPTION) |
| `back_to_followup_id(query, context)` | Follow-up ID entry (FOLLOWUP_ID) |
| `back_to_emergency_desc(query, context)` | Emergency description (EMERGENCY_DESC) |

`text_back_handler` handles when users type the word "back". Uses `_back_target` key in `user_data` to dispatch:
- `"sub_service"` → `back_to_sub_service_via_message()`
- `"description"` → `back_to_category_via_message()`
- `"followup_id"` → `back_to_request_type_via_message()`
- `"followup_note"` → `back_to_followup_id_via_message()`
- `"emergency_desc"` → `back_to_request_type_via_message()`

### 5.11 Notification Helpers

**`_notify(app, recipient_uid, message)`**  
Async. Calls `app.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")`. Catches and logs all exceptions — never blocks the main flow.

**`_get_field_agents_for_unit(unit) → list`**  
`SELECT DISTINCT assigned_to FROM master_units_hierarchy WHERE full_label=? AND assigned_to IS NOT NULL`

### 5.12 Schema Validation

`validate_db_schema(db_path)` runs at startup. Checks `submissions` has all expected columns including `sub_service`. Exits with error if missing — fail fast, not mid-submission.

---

## 6. Agent Bot (agent_bot.py)

**Bot:** @GUCMain2bot  
**Token env:** `AGENT_BOT_TOKEN`  
**Users:** Field agents and approvers

### 6.1 Conversation States

```python
COMPOUND = 0          # New ticket: select compound
UNIT_TYPE = 1         # Villa or Apartment
VILLA_NUMBER = 2      # Villa number selection
BUILDING = 3          # Building selection (apartments)
FLAT = 4              # Flat number selection
REQUEST_TYPE = 5      # New / Follow Up / Emergency
CATEGORY = 6          # Maintenance / Facilities
SERVICE = 7           # Level-2 category (DB-driven)
FACILITY_SERVICE = 8  # Unused — kept for index stability
DESCRIPTION = 9       # Free-text description
PHOTO = 10            # Optional photo
CONFIRM = 11          # Summary + submit
FOLLOWUP_ID = 12      # Text: ticket number
FOLLOWUP_STATUS = 13  # Status selection
FOLLOWUP_NOTE = 14    # Text: follow-up note
EMERGENCY_DESC = 15   # Text: emergency description
EMERGENCY_PHOTO = 16  # Emergency photo
EMERGENCY_CONFIRM = 17  # Emergency confirm
MAIN_MENU = 18        # Role-based main menu
EX_FILTER = 19        # Existing tickets filter entry
EX_COMPOUND = 20      # Existing tickets: compound filter
EX_UNIT_TYPE = 21     # Existing tickets: unit type filter
EX_VILLA = 22         # Existing tickets: villa filter
EX_BUILDING = 23      # Existing tickets: building filter
EX_FLAT = 24          # Existing tickets: flat filter
TICKET_LIST = 25      # Paginated ticket list
TICKET_DETAIL = 26    # Single ticket view + actions
COMPLETE_COST = 27    # Work completion: actual cost input
COMPLETE_CONFIRM = 28 # Work completion: confirm close
APPROVAL_LIST = 29    # Approver: pending approvals list
APPROVAL_DETAIL = 30  # Approver: single ticket review
APPROVAL_NOTE = 31    # Approver: note input (approve or reject)
_UNUSED_31 = 32       # Index placeholder — do not reuse
COMPLETE_PHOTO = 33   # Work completion: mandatory photo
SUB_SERVICE = 34      # Level-3 sub-category (DB-driven)
```
Total: 35 states (range(35)).

### 6.2 Entry & Role Detection

**`start(update, context)`**  
1. Clears `context.user_data`
2. Calls `_get_user_roles(uid)` → stores result in `user_data`
3. Unauthorized (no roles) → access denied message, END
4. Authorized → shows `_dynamic_main_menu_keyboard()` → `MAIN_MENU`

**`_get_user_roles(uid) → dict`**  
```sql
SELECT DISTINCT role FROM agents WHERE telegram_user_id=? AND active=1
```
Returns `{is_field_agent: bool, is_approver: bool, approver_roles: list}`.  
Fallback: if not in agents at all, checks `master_units_hierarchy WHERE assigned_to=?` and treats as `field_agent`.

**`_dynamic_main_menu_keyboard(context) → InlineKeyboardMarkup`**  
Shows buttons conditionally:
- field_agent → "🆕 New Ticket", "📋 Existing Tickets"
- approver → "✅ Pending Approvals", "🗂️ All Tickets"
- both → all four

### 6.3 New Ticket — Unit Selection

**`main_menu_handler`** — State `MAIN_MENU`  
Callback `main:new` → builds `_compound_keyboard(uid)` → `COMPOUND`

**`_compound_keyboard(uid, back_cb) → InlineKeyboardMarkup`**  
```sql
SELECT DISTINCT compound FROM master_units_hierarchy WHERE assigned_to=? AND compound IS NOT NULL ORDER BY compound
```
Builds one button per compound. Emoji lookup from `COMPOUND_EMOJI` dict (fallback: 🏘️).

**`compound_handler`** — State `COMPOUND`  
Callback `compound:{name}`  
Stores compound. Queries available unit types:
```sql
SELECT DISTINCT unit_type FROM master_units_hierarchy WHERE assigned_to=? AND compound=?
```
Shows only types the agent actually has → `UNIT_TYPE`

**`unit_type_handler`** — State `UNIT_TYPE`  
Callback `unit_type:Villa` or `unit_type:Apartment` or `unit_type:back`  
- Villa → queries villas → `VILLA_NUMBER`
- Apartment → queries buildings → `BUILDING`
- Back → rebuilds compound keyboard (same query as above) → `COMPOUND`

**`villa_handler`** — State `VILLA_NUMBER`  
Callback `villa:{number}` or `back:unit_type`  
```sql
SELECT villa_number, full_label FROM master_units_hierarchy
WHERE assigned_to=? AND compound=? AND unit_type='Villa' AND villa_number=?
```
Stores `unit = full_label` → calls `show_request_type_callback()` → `REQUEST_TYPE`

**`building_handler`** — State `BUILDING`  
Callback `bldg:{building}` or `back:unit_type`  
Queries flats for the selected building → `FLAT`

**`flat_handler`** — State `FLAT`  
Callback `flat:{number}` or `back:building`  
```sql
SELECT flat_number, full_label FROM master_units_hierarchy
WHERE assigned_to=? AND compound=? AND building_number=? AND flat_number=?
```
Stores `unit = full_label` → `REQUEST_TYPE`

### 6.4 New Ticket — Service Selection

Same 3-level DB-driven flow as resident bot:

**`category_handler`** — State `CATEGORY`  
Callback `cat:0` (Maintenance) or `cat:1` (Facilities) or `back:category`  
Queries `_get_categories(main_cat)` → `SERVICE`

**`service_handler`** — State `SERVICE`  
Callback `svc:{category_name}` or `back:category`  
Queries `_get_sub_categories(main_cat, category)` → `SUB_SERVICE`

**`sub_service_handler`** — State `SUB_SERVICE`  
Callback `subsvc:{sub}` or `back:service`  
Stores `sub_service` → `DESCRIPTION`

### 6.5 New Ticket — Description, Photo, Submit

**`description_handler`** — State `DESCRIPTION`  
Text min 5 chars → `PHOTO`

**`photo_handler`** — State `PHOTO`  
Photo: `AGENT_{uid}_{ts}_{hex}.jpg`. Falls back gracefully on download failure.

**`photo_skip_handler`** — Callback `photo:skip` in `PHOTO`  
Sets photo to None.

**`build_summary(data) → str`**  
Builds the confirmation text from `user_data`. Formats service line as `"Air Conditioning — AC Not Working"` when sub_service present.

**`confirm_handler`** → **`write_submission()`** — State `CONFIRM`

**`write_submission(update, context, is_emergency=False)`**  
```sql
INSERT INTO submissions
  (telegram_user_id, phone_number, unit, compound, request_type, category,
   service, sub_service, issue_description, photo_path, photo_file_id, priority, required_approvals)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2)
```
`required_approvals` is always hardcoded to `2`.  
`compound` comes from `user_data["compound"]` (set during unit drill-down).

**Post-write notification:** Queries `_get_role_uids_for_compound(compound, "approver_1")`, notifies each with:
```
🆕 New Request #{sid} submitted
🏠 {unit} / {service}
```

### 6.6 Existing Tickets

**`_show_compound_screen(query, context)`**  
Clears filters from `user_data`. Queries `_get_agent_compounds(uid)`. Shows compound buttons → `EX_COMPOUND`

**`ex_compound_handler`** — State `EX_COMPOUND`  
Callback `compound:{name}` → stores `f_compound` → `EX_UNIT_TYPE`  
Callback `ex_show:now` → shows all tickets immediately

**`ex_unit_type_handler`** — State `EX_UNIT_TYPE`  
Callback `ex_utype:Villa` / `ex_utype:Apartment` / `ex_utype:back`  
Queries villas or buildings for the selected compound.

Villa → `EX_VILLA`, Building → `EX_BUILDING`

**`ex_villa_handler`** / **`ex_building_handler`** / **`ex_flat_handler`**  
Progressive drill-down. Each level queries the DB for available options and stores the filter.

**`ex_show_now_handler`**  
Callback `ex_show:now` — works from any EX_* state. Sets `tkt_page = 0` → calls `_render_ticket_list()`.

**`_build_ticket_query(uid, context) → (sql, params)`**  
Builds a scoped SQL query based on active filters (`f_compound`, `f_unit_type`, `f_building`, `f_unit`):
- `f_unit` set → `WHERE unit=?` (most specific)
- Otherwise → subquery: `WHERE unit IN (SELECT full_label FROM master_units_hierarchy WHERE assigned_to=? [AND compound=?] [AND unit_type=?] [AND building_number=?])`

**`_render_ticket_list(query, context)`**  
Paginates (8 per page). Fetches from `submissions` using `_build_ticket_query()`. Shows status emoji + ticket ID + service + date. Pagination buttons: `tkt_nav:prev`, `tkt_nav:next`.

**`ticket_handler`** — State `TICKET_LIST`  
Callback `tkt:{id}` → calls `_render_agent_ticket_detail()`

**`_render_agent_ticket_detail(query, context, tid)`**  
`SELECT * FROM submissions WHERE id=?`  
Displays full ticket detail. Shows action buttons based on status:
- `approved` → "✅ Complete Work" (`tkt_action:complete`)
- `rejected` → "🔄 Re-submit for Approval" (`tkt_action:resubmit`)
- Other → back button only

### 6.7 Work Completion Flow

Triggered when field agent taps "✅ Complete Work" on an `approved` ticket.

**`complete_cost_handler`** — State `COMPLETE_COST`  
Text input (required, any length). Stores `complete_cost` → `COMPLETE_PHOTO`

**`complete_photo_handler`** — State `COMPLETE_PHOTO`  
Mandatory photo. `COMPLETE_{uid}_{ts}_{hex}.jpg`. No skip option.  
Stores `complete_photo_path` and `complete_photo_file_id` → `COMPLETE_CONFIRM`

**`complete_confirm_handler`** — State `COMPLETE_CONFIRM`  
Callback `complete_confirm:yes`  
```sql
UPDATE submissions
SET status='closed', work_done_by=?, work_done_at=datetime('now'),
    actual_cost=?, completion_photo_path=?, completion_photo_file_id=?
WHERE id=?
```
**Post-close notification:** Queries both `approver_1` and `approver_2` for the compound. De-duplicates via `notified` set. Sends to each:
```
🔒 Ticket #{tid} Closed
🏠 {unit} / {service}
💰 Actual cost: {cost}
📸 Completion photo: Attached
```

### 6.8 Re-submit After Rejection

**`ticket_detail_handler`** — Callback `tkt_action:resubmit` → shows confirm screen  
**`ticket_detail_handler`** — Callback `tkt_action:resubmit_confirm`  
```sql
UPDATE submissions SET status='submitted' WHERE id=?
```
Notifies `approver_1` for the compound.

### 6.9 Approver Workflow

**`_render_approval_list(query, context)`**  
Builds combined pending query via `_build_pending_approvals_query()`:
- approver_1 roles → `status='submitted'` tickets in their compounds
- approver_2 roles → `status='approved_1'` tickets in their compounds
- Combined with OR for multi-role users (Riaz)

Paginates same as ticket list.

**`approval_ticket_handler`** → **`_render_approval_detail(query, context, tid)`**  
`SELECT * FROM submissions WHERE id=?`  
Determines whether this is L1 or L2 review based on status + user roles.  
Shows Approve / Reject buttons.

**`approval_detail_handler`** — State `APPROVAL_DETAIL`  
Callback `approval:approve` → `APPROVAL_NOTE` (optional note)  
Callback `approval:reject` → `APPROVAL_NOTE` (required note, min 5 chars)

**`approval_note_handler`** — State `APPROVAL_NOTE`  
Text or `approval_note:skip` (approve only).  
Calls `_do_write_approval(context, reply_fn)`.

**`_do_write_approval(context, reply_fn)`**  
```sql
INSERT INTO approvals (submission_id, level, action, actor_id, actor_note)
VALUES (?, ?, ?, ?, ?)
```
Then:
```sql
-- L1 approve:
UPDATE submissions SET status='approved_1' WHERE id=?

-- L2 approve:
UPDATE submissions SET status='approved' WHERE id=?

-- Any reject:
UPDATE submissions SET status='rejected' WHERE id=?
```

**Notifications dispatched from `_do_write_approval`:**

| Trigger | Recipients | Message |
|---|---|---|
| L1 approve | All `approver_2` for compound | "1️⃣ Ticket #{tid} approved (L1), needs your review" |
| L2 approve | Submitter | "✅ Ticket #{tid} approved — ready for work" |
| L2 approve | All `approver_1` for compound (except submitter) | "✅ Ticket #{tid} fully approved (L2)" |
| L1 reject | Submitter | "❌ Ticket #{tid} rejected at Level 1: {reason}" |
| L2 reject | All `approver_1` for compound | "❌ Ticket #{tid} rejected at Level 2: {reason}" |
| L2 reject | Submitter | "❌ Ticket #{tid} rejected at Level 2: {reason}" |

### 6.10 Approver: All Tickets View

**`_render_approver_all_tickets(query, context)`**  
Queries the agent's compounds from `agents` table:
```sql
SELECT DISTINCT compound FROM agents
WHERE telegram_user_id=? AND compound IS NOT NULL AND active=1
```
Then fetches all submissions for those compounds:
```sql
SELECT ... FROM submissions WHERE compound IN (?, ...) ORDER BY submitted_at DESC
```
Read-only view — no action buttons.

### 6.11 Notification Helpers

**`_notify(app, recipient_uid, message)`**  
Same as resident bot — async, catches all exceptions.

**`_get_field_agents_for_unit(unit, exclude_uid=None) → list`**  
```sql
SELECT DISTINCT assigned_to FROM master_units_hierarchy
WHERE full_label=? AND assigned_to IS NOT NULL [AND assigned_to != ?]
```

**`_get_role_uids_for_compound(compound, role) → list`**  
```sql
SELECT DISTINCT telegram_user_id FROM agents
WHERE role=? AND compound=? AND active=1
```

**`_get_agent_compounds(uid) → list`**  
```sql
SELECT DISTINCT compound FROM master_units_hierarchy
WHERE assigned_to=? AND compound IS NOT NULL ORDER BY compound
```

**`_get_categories(main_category) → list`**  
```sql
SELECT DISTINCT category FROM services WHERE main_category=? ORDER BY category
```

**`_get_sub_categories(main_category, category) → list`**  
```sql
SELECT sub_category FROM services WHERE main_category=? AND category=? ORDER BY sub_category
```

### 6.12 Callback Data Encoding

The agent bot uses **key-based** callbacks throughout (never positional index, with the exception of FOLLOWUP_STATUSES which is a fixed list):

```python
# Unit drill-down
callback_data=f"compound:{compound_name}"
callback_data=f"unit_type:Villa"
callback_data=f"villa:{villa_number}"
callback_data=f"bldg:{building_number}"
callback_data=f"flat:{flat_number}"

# Service selection
callback_data=f"svc:{category_name}"
callback_data=f"subsvc:{sub_category_name}"

# Tickets
callback_data=f"tkt:{ticket_id}"
callback_data=f"appr_tkt:{ticket_id}"
callback_data=f"all_tkt:{ticket_id}"
```

This means callback data is human-readable and robust — adding a new compound or category never shifts existing callback mappings.

---

## 7. Adding / Modifying Data

### 7.1 Add a resident

Edit `app/master_data.csv` (columns: `phone_number`, `telegram_user_id`, `owner_name`, `units`).  
`units` is a JSON array: `["Diamond Villa 101"]`

Then run:
```bash
docker exec field-service-bot python3 /app/setup_db.py
```
Note: this is additive. If the same phone exists, a duplicate row is created. For clean reload, first:
```bash
docker exec field-service-bot python3 -c "
import sqlite3; conn = sqlite3.connect('/data/field_service.db')
conn.execute('DELETE FROM master_units'); conn.commit(); conn.close()
"
```

### 7.2 Add a unit / compound

```sql
INSERT INTO master_units_hierarchy
  (compound, unit_type, villa_number, full_label, assigned_to)
VALUES ('Diamond Compound', 'Villa', 'V-201', 'Diamond Villa 201', '123456789');
```
No restart needed — queried at runtime.

### 7.3 Add a field agent

Two inserts required:

```sql
-- Grant field agent role
INSERT INTO agents (telegram_user_id, name, role, compound, active)
VALUES ('TG_USER_ID', 'Agent Name', 'field_agent', NULL, 1);

-- Assign to units
UPDATE master_units_hierarchy SET assigned_to = 'TG_USER_ID'
WHERE compound = 'Diamond Compound' AND unit_type = 'Villa' AND villa_number IN ('V-101', 'V-102');
```

### 7.4 Add an approver

One row per compound they cover:
```sql
INSERT INTO agents (telegram_user_id, name, role, compound, active)
VALUES ('TG_USER_ID', 'Approver Name', 'approver_1', 'Diamond Compound', 1);

INSERT INTO agents (telegram_user_id, name, role, compound, active)
VALUES ('TG_USER_ID', 'Approver Name', 'approver_1', 'Pearl Compound', 1);
```

### 7.5 Suspend a user

```sql
UPDATE agents SET active = 0 WHERE telegram_user_id = 'TG_USER_ID';
```

### 7.6 Add or change services

1. Edit `app/services.csv`
2. Clear the services table:
```bash
docker exec field-service-bot python3 -c "
import sqlite3; conn = sqlite3.connect('/data/field_service.db')
conn.execute('DELETE FROM services'); conn.commit(); conn.close()
"
```
3. Re-run `setup_db.py`:
```bash
docker exec field-service-bot python3 /app/setup_db.py
```
No restart needed — services are queried at runtime.

---

## 8. Deployment Operations

### Code changes (no Dockerfile change)
```bash
# Edit file on host, then:
docker exec field-service-bot supervisorctl restart agent-bot
# or
docker exec field-service-bot supervisorctl restart resident-bot
```

### Requirements / Dockerfile change
```bash
cd /opt/field-service-bot
docker compose down
docker compose build
docker compose up -d
```

### Check status
```bash
docker exec field-service-bot supervisorctl status
```

### Live logs
```bash
tail -f /opt/field-service-bot/logs/agent_bot.log
tail -f /opt/field-service-bot/logs/bot.log
```

### Direct SQLite access
```bash
docker exec field-service-bot python3 -c "
import sqlite3
conn = sqlite3.connect('/data/field_service.db')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id, unit, status, submitted_at FROM submissions ORDER BY id DESC LIMIT 10').fetchall()
for r in rows: print(dict(r))
conn.close()
"
```

---

## 9. Error Handling

- All notification calls wrapped in `try/except` — a failed Telegram send never crashes the main flow.
- DB failures in `write_submission()` are caught, logged, and shown to the user as "Submission failed. Please try again."
- Approval writes follow the same pattern.
- Both bots have a global `error_handler` registered with the Application that logs the full traceback and sends "Something went wrong. Say hi to start over." to the user.
- `validate_db_schema()` runs at startup. If expected columns are missing, the bot prints the error and calls `sys.exit(1)` — this forces supervisord to restart it (and the logs will show the cause).

---

## 10. Known Limitations

| Issue | Impact | Notes |
|---|---|---|
| Resident bot unit labels don't match `full_label` | Notifications to field agents silently fail for resident-submitted tickets | Data alignment needed |
| `master_data.csv` reload is additive | Running setup_db.py twice duplicates resident rows | Clear table first |
| `form_state` table unused | Cosmetic | Bots use `context.user_data` |
| Photos never cleaned up | Disk accumulates orphaned photos | No deletion on ticket delete |
| Tickets before Phase 5 have `compound = NULL` | Don't appear in approver pending lists | Acceptable — only new tickets need workflow |
