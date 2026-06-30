# Field Service Bot

## What This Is

Two deterministic Telegram bots sharing one SQLite DB. Zero LLM — pure state-machine `python-telegram-bot` 22.x. Handles maintenance/facility requests for a residential compound (Diamond, Pearl, Sapphire).

- **Resident bot** (`bot.py`) — for unit owners: phone/TG lookup → submit request, follow up, report emergency
- **Agent bot** (`agent_bot.py`) — for field agents and approvers: role-based menu, hierarchical unit drill-down, new ticket, view all assigned tickets, mark work done, approve/reject

## Container

```
Container name:  field-service-bot
Compose file:    /opt/field-service-bot/docker-compose.yml
Process manager: supervisord (manages both bots inside one container)
Restart policy:  unless-stopped (survives reboots automatically)
```

Inside the container:
- `/app/`   → code (bind-mounted from `./app/`)
- `/data/`  → DB + photos (bind-mounted from `./data/`)
- `/logs/`  → log files (bind-mounted from `./logs/`)

## Directory Layout

```
/opt/field-service-bot/
├── app/
│   ├── bot.py               ← Resident bot
│   ├── agent_bot.py         ← Agent bot (field agents + approvers)
│   ├── setup_db.py          ← Schema migrations + seed data (safe to re-run)
│   ├── test_flow.py         ← DB flow tester
│   └── master_data.csv      ← Phone/TG → unit mappings for resident bot
├── data/
│   ├── field_service.db     ← SQLite (the source of truth)
│   └── photos/              ← Uploaded images
├── logs/
│   ├── bot.log              ← Resident bot (RotatingFileHandler, 5 MB × 5)
│   └── agent_bot.log        ← Agent bot
├── .env                     ← Tokens + path overrides — never commit
├── Dockerfile
├── docker-compose.yml
├── supervisord.conf
├── requirements.txt
├── CLAUDE.md                ← this file
└── PROJECT_PLAN.md          ← roadmap and feature backlog
```

## Daily Commands

```bash
# Check both bots are running
docker exec field-service-bot supervisorctl status

# Restart one bot after a code change (app/ is bind-mounted — no rebuild needed)
docker exec field-service-bot supervisorctl restart agent-bot
docker exec field-service-bot supervisorctl restart resident-bot

# Follow logs live
tail -f /opt/field-service-bot/logs/agent_bot.log
tail -f /opt/field-service-bot/logs/bot.log

# Full container restart (both bots)
cd /opt/field-service-bot && docker compose restart

# Rebuild image (only needed when Dockerfile or requirements.txt changes)
cd /opt/field-service-bot && docker compose down && docker compose build && docker compose up -d

# Run DB migrations + re-seed agents (safe to re-run — never drops data, skips seed if agents exist)
docker exec field-service-bot python3 /app/setup_db.py

# Open SQLite directly (via Python — no sqlite3 CLI in container)
docker exec field-service-bot python3 -c "
import sqlite3; conn = sqlite3.connect('/data/field_service.db')
conn.row_factory = sqlite3.Row
# ... your query here
conn.close()
"
```

## Environment Variables (`.env`)

| Variable         | Purpose                              |
|------------------|--------------------------------------|
| `BOT_TOKEN`      | Resident bot Telegram token          |
| `AGENT_BOT_TOKEN`| Agent bot Telegram token             |
| `DB_PATH`        | Default: `/data/field_service.db`    |
| `PHOTO_DIR`      | Default: `/data/photos`              |
| `LOG_DIR`        | Default: `/logs`                     |

Token fallback: if the env var is empty, bots look for `.bot_token` / `.agent_bot_token` next to the script (backward compat).

## Database Tables

| Table | Purpose |
|-------|---------|
| `master_units` | Resident bot: phone/TG → JSON unit list |
| `master_units_hierarchy` | Agent bot: compound/type/villa/building/flat + `assigned_to` (TG user ID) |
| `submissions` | All requests — both bots write here |
| `agents` | Agent/approver registry — see schema below |
| `approvals` | Approval audit trail: submission_id, level, action, actor_id, note, timestamp |
| `form_state` | Reserved — not used at runtime |

### agents table schema

One row per (telegram_user_id, role, compound) combination.

```sql
id                INTEGER PRIMARY KEY AUTOINCREMENT
telegram_user_id  TEXT NOT NULL
name              TEXT NOT NULL
role              TEXT NOT NULL  -- 'field_agent' | 'approver_1' | 'approver_2'
compound          TEXT           -- NULL for field_agent; 'Diamond'/'Pearl'/'Sapphire' for approvers
active            INTEGER DEFAULT 1
```

**Approval routing:** when a ticket for compound X reaches `work_done`, all agents with
`role='approver_1' AND compound='X' AND active=1` see it in their Pending list. First to act wins.

### Seeded agents (setup_db.py → seed_agents)

| Name | TG ID | Roles |
|------|-------|-------|
| Afsal Khan | 8976446718 | field_agent |
| Riaz | 8580506857 | field_agent + approver_1 (all) + approver_2 (all) |
| Fasil | 7228949233 | approver_1 (all compounds) |
| Shahbaz | 8767995042 | approver_2 (all compounds) |

To add a new approver for a specific compound:
```sql
INSERT INTO agents (telegram_user_id, name, role, compound, active)
VALUES ('123456789', 'New Person', 'approver_1', 'Diamond', 1);
```

### Submissions workflow columns

```
compound:           TEXT — compound of the unit (set at submission; drives approver routing)
status:             submitted → work_done → approved_1 → closed / rejected
required_approvals: INTEGER DEFAULT 2 (hardcoded at submission — 2-level chain for all tickets)
work_done_by:       agent TG user ID
work_done_at:       timestamp
work_done_note:     agent's completion note
cost_estimate:      TEXT — optional cost entered by field agent at mark-done
cost_confirmed:     TEXT — cost confirmed by Approver 1 (required before ticket advances to L2)
closed_by:          final approver TG user ID
closed_at:          timestamp
close_note:         final approver's note
```

**Re-submit after rejection:** agents can tap "Mark Work Done" again on a `rejected` ticket to re-enter the approval chain.

## Key Code Patterns

### Role-based menu (agent bot)

`start()` calls `_get_user_roles(uid)` which checks the `agents` table:
- `field_agent` role → shows New Ticket + Existing Tickets
- `approver_1` / `approver_2` role → shows Pending Approvals + All Tickets
- Both → shows all four options (e.g. Riaz in testing)
- Not in agents table → fallback check against `master_units_hierarchy.assigned_to`

### ConversationHandler states (agent bot — 34 total)

```python
(COMPOUND, UNIT_TYPE, VILLA_NUMBER, BUILDING, FLAT,         # new-ticket unit selection
 REQUEST_TYPE, CATEGORY, SERVICE, FACILITY_SERVICE,          # new-ticket form
 DESCRIPTION, PHOTO, CONFIRM,
 FOLLOWUP_ID, FOLLOWUP_STATUS, FOLLOWUP_NOTE,               # follow-up
 EMERGENCY_DESC, EMERGENCY_PHOTO, EMERGENCY_CONFIRM,         # emergency
 MAIN_MENU, EX_FILTER, EX_COMPOUND, EX_UNIT_TYPE,           # existing-tickets filter
 EX_VILLA, EX_BUILDING, EX_FLAT,
 TICKET_LIST, TICKET_DETAIL,
 MARK_DONE_NOTE, MARK_DONE_CONFIRM,                          # mark work done
 APPROVAL_LIST, APPROVAL_DETAIL,                             # approver workflow
 APPROVAL_COST, APPROVAL_NOTE,
 MARK_DONE_COST) = range(34)                                 # cost estimate at mark-done
```

### Compound-filtered unit type selection

`compound_handler` queries the DB before building the unit type keyboard — only shows Villa/Apartment buttons for types the agent actually has assigned in that compound. Same logic in `back_to_unit_type`.

### Approval flow summary

```
Field agent: Mark Work Done → optional cost estimate → confirmed
Approver 1:  Pending list (status=work_done, same compound) → must enter confirmed cost → optional note → approved_1
Approver 2:  Pending list (status=approved_1, same compound) → optional note → closed
Either:      Reject (mandatory note) → status=rejected → agent can re-submit
```

### "Show All at any filter level" pattern

`ex_show:now` callback fires from any EX_* screen. Handler:
```python
async def ex_show_now_handler(update, context):
    query = update.callback_query; await query.answer()
    context.user_data["tkt_page"] = 0
    return await _render_ticket_list(query, context)
```

### Key-based callback encoding (never positional index)

```python
InlineKeyboardButton(r["villa_number"], callback_data=f"villa:{r['villa_number']}")
villa = query.data.split(":", 1)[1]
conn.execute("... WHERE villa_number=?", (villa,))
```

### Schema migration pattern

`ensure_schema()` in `setup_db.py` — idempotent, only ADDs missing columns, never drops.
Special case: if `agents` table has old schema (no `id` column), it is dropped and recreated
(safe because it was always empty before Phase 5).

## Adding Users

**Resident bot** — edit `master_data.csv`, then:
```bash
docker exec field-service-bot python3 /app/setup_db.py
```

**Field agent** — insert into `master_units_hierarchy` with `assigned_to = '<TG user ID>'`
AND insert into `agents`:
```sql
INSERT INTO master_units_hierarchy (compound, unit_type, villa_number, full_label, assigned_to)
VALUES ('Diamond', 'Villa', 'V-201', 'Diamond Villa 201', '123456789');

INSERT INTO agents (telegram_user_id, name, role, compound, active)
VALUES ('123456789', 'New Agent', 'field_agent', NULL, 1);
```

**Approver** — insert into `agents` (one row per compound they cover):
```sql
INSERT INTO agents (telegram_user_id, name, role, compound, active)
VALUES ('123456789', 'Ahmed Al-Rashid', 'approver_1', 'Diamond', 1);
```

## Telegram Bot Identities

| Bot handle    | Display name    | Code file    | Role |
|---------------|-----------------|--------------|------|
| @GUCMain1bot  | GUCMaintenance  | `bot.py`     | Resident bot |
| @GUCMain2bot  | GUCMaintService | `agent_bot.py` | Agent bot |

## What's Pending

See `PROJECT_PLAN.md` for the full roadmap. The next phase is **Phase 6 — Notifications**:
push messages to approvers when a ticket reaches their level, and to agents when their ticket is closed or rejected.
