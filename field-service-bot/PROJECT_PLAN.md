# Field Service Bot — Project Plan

## Status Overview

| Phase | Title | Status |
|-------|-------|--------|
| 1 | Resident Bot — Core Form | ✅ Done |
| 2 | Agent Bot — Hierarchical Unit Selection + New Ticket | ✅ Done |
| 3 | Agent Bot — Existing Tickets + Mark Work Done | ✅ Done |
| 4 | Dedicated Docker Container (out of Hermes) | ✅ Done |
| 5 | Approver Workflow | ✅ Done |
| 6 | Notifications | ✅ Done |
| 7 | Resident Bot — Ticket Status Lookup | 🔲 Pending |
| 8 | Reporting & Admin | 🔲 Pending |

---

## ✅ Phase 1 — Resident Bot (Core Form)

**What was built:**
- `bot.py` — deterministic state-machine bot using `python-telegram-bot` 22.x
- TG ID auto-lookup: authorized users skip phone entry entirely
- Phone → unit lookup fallback for walk-ins
- Three request types: New Request, Follow Up, Emergency
- Category → Service drill-down (Maintenance 9 services, Facilities 10 services)
- Free-text description (5–500 chars)
- Optional photo upload with graceful fallback if download fails
- Summary confirm screen before DB write
- Follow-up ownership check (resident can only follow up on their own units)
- Start clears stale `context.user_data` to prevent cross-session contamination

**Key files:** `app/bot.py`, `app/setup_db.py`, `app/master_data.csv`

**DB tables:** `master_units`, `submissions`

---

## ✅ Phase 2 — Agent Bot (New Ticket + Hierarchy)

**What was built:**
- `agent_bot.py` — separate bot, same DB, agent-specific token
- Compound → Unit Type → Villa# (or Building → Flat#) drill-down unit selection
- Key-based callback encoding throughout (no positional index lookups)
- Same New Request / Follow Up / Emergency form as resident bot
- Follow-up ownership check: agent can only follow up on units assigned to them
- Authorization gate: agents without `assigned_to` records see an access-denied message

**DB tables:** `master_units_hierarchy` (compound, unit_type, villa_number, building_number, flat_number, full_label, assigned_to)

---

## ✅ Phase 3 — Agent Bot (Existing Tickets + Mark Work Done)

**What was built:**
- Main menu: 🆕 New Ticket | 📋 Existing Tickets
- Existing Tickets filter: compound → unit type → villa/building/flat drill-down
- **"Show All" at every filter level** via `ex_show:now` callback from any EX_* screen
- Paginated ticket list (8 per page, prev/next nav)
- Ticket detail view (full info including work notes and status)
- Mark Work Done flow: note entry (min 5 chars) → confirm → sets `status = work_done`
- `_build_ticket_query()` helper: builds scoped SQL from filter keys in `context.user_data`
- `_filter_label()` helper: human-readable scope header for the list screen

**DB workflow columns added:** `work_done_by`, `work_done_at`, `work_done_note`, `required_approvals`
**DB tables added:** `agents`, `approvals`

**Conversation states:** 29 total

---

## ✅ Phase 4 — Dedicated Docker Container

**What was built:**
- `/opt/field-service-bot/` — standalone project, fully independent of Hermes
- `Dockerfile` + `docker-compose.yml` — single container, `restart: unless-stopped`
- `supervisord.conf` — manages `resident-bot` and `agent-bot` processes inside container
- `requirements.txt` — pinned `python-telegram-bot==22.6`
- `.env` — tokens and path overrides (`BOT_TOKEN`, `AGENT_BOT_TOKEN`, `DB_PATH`, `PHOTO_DIR`, `LOG_DIR`)
- App code is bind-mounted (`./app:/app`) — edit on host, `supervisorctl restart` to apply
- DB and photos are bind-mounted (`./data:/data`) — survive container rebuilds

---

## ✅ Phase 5 — Approver Workflow

### What was built

**DB changes:**
- `agents` table recreated with new schema: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `compound TEXT` column added
  - One row per (telegram_user_id, role, compound) — a person covering 3 compounds has 3 rows
  - `field_agent` rows have `compound = NULL`; approver rows are scoped per compound
- `submissions` table: added `compound TEXT`, `cost_estimate TEXT`, `cost_confirmed TEXT`
- `required_approvals` hardcoded to `2` at ticket creation (INSERT explicitly sets it)
- `setup_db.py` gains `seed_agents()` — idempotent seed of 4 users × their role/compound combinations (14 rows total)

**Seeded agents:**

| Name | TG ID | Roles |
|------|-------|-------|
| Afsal Khan | 8976446718 | field_agent |
| Riaz | 8580506857 | field_agent + approver_1 (all) + approver_2 (all) |
| Fasil | 7228949233 | approver_1 (Diamond, Pearl, Sapphire) |
| Shahbaz | 8767995042 | approver_2 (Diamond, Pearl, Sapphire) |

**Role-based menu:**
- `start()` calls `_get_user_roles(uid)` — checks `agents` table, falls back to `master_units_hierarchy`
- Field agents see: 🆕 New Ticket | 📋 Existing Tickets
- Approvers see: ✅ Pending Approvals | 🗂️ All Tickets
- Multi-role users (Riaz) see all four

**Mark Work Done — cost estimate step:**
- After work note: optional cost estimate screen (text input or Skip)
- Cost stored as `submissions.cost_estimate`
- Confirm screen shows both note and cost before final submission

**Approver workflow:**
- **Pending Approvals** query: compound-scoped, combines L1 (`status=work_done`) and L2 (`status=approved_1`) for multi-role approvers
- **Approver 1 approve path:** cost confirmation screen (required) → shows agent's estimate if present, accepts custom value or "Use Estimate" button → optional note → writes `approved_1` + `cost_confirmed`
- **Approver 2 approve path:** optional note → writes `closed`
- **Reject (any level):** mandatory note (min 5 chars) → writes `rejected`
- **Re-submit:** `rejected` status added to `AGENT_EDITABLE_STATUSES` — agents see "🔄 Re-submit Work Done" button and can re-enter the mark-done flow
- **All Tickets view:** approvers can browse all tickets in their compound(s), read-only

**Bug fix:**
- Unit type buttons (Villa/Apartment) on compound selection now query the DB and only show types the agent actually has assigned in that compound

**Conversation states:** 34 total (added APPROVAL_LIST, APPROVAL_DETAIL, APPROVAL_COST, APPROVAL_NOTE, MARK_DONE_COST)

### Status flow

```
submitted
    → [field agent: Mark Work Done + optional cost] → work_done
    → [Approver 1: confirm cost + optional note]    → approved_1  (or rejected)
    → [Approver 2: optional note]                   → closed      (or rejected)

rejected → [field agent: Re-submit Work Done] → work_done  (re-enters approval chain)
```

---

## ✅ Phase 6 — Notifications

**Push notifications via Telegram `bot.send_message()`** — no polling required, just a direct API call when status changes.

### Status flow (corrected)

```
submitted
    → [Approver 1 reviews request]       → approved_1  (or rejected → notify submitter)
    → [Approver 2 reviews request]       → approved    (or rejected → notify submitter)
    → [Field agent: Complete Work        → closed
       mandatory photo + actual cost]
```

### Notification events

| Event | Who to notify | Message |
|-------|--------------|---------|
| status → `submitted` | All active approver_1 for that compound | "New Request #N submitted: {unit} / {service}" |
| status → `approved_1` | All active approver_2 for that compound | "Ticket #N approved (L1), needs your review" |
| status → `approved` | All field agents assigned to that unit | "Ticket #N approved — ready for work: {unit}" |
| status → `rejected` | Original submitter (telegram_user_id) | "Ticket #N rejected at Level {N}: {reason}" |
| status → `closed` | All approver_1 + approver_2 for compound | Full summary: unit, service, actual cost, photo attached |

### Work completion flow (agent side)

Agents see "✅ Complete Work" button only on `approved` tickets. Tapping it requires:
1. Actual cost (text, required)
2. Completion photo (mandatory, no skip)
3. Confirm → ticket closes, summary notification sent

### DB columns added (Phase 6)

`submissions`: `actual_cost TEXT`, `completion_photo_path TEXT`, `completion_photo_file_id TEXT`

### Known gap

Resident bot unit labels ("Villa 12") don't match hierarchy `full_label` ("Diamond Villa 101"), so resident-submitted tickets silently find no field agent to notify at the `approved` stage. Data alignment fix needed.

---

## 🔲 Phase 7 — Resident Bot: Ticket Status Lookup

**What to build:**
- Add "📋 My Tickets" to the resident bot's initial menu
- Show all tickets for the resident's units, newest first
- Status emoji + date + service type per row
- Tap a ticket → full detail (read-only)

**Decisions needed:**
- [ ] All units or only tickets the resident submitted?
- [ ] Show `work_done_note` and approver notes, or only status?

---

## 🔲 Phase 8 — Reporting & Admin

Low priority. Consider only after Phase 6 is stable.

**Candidate features:**
- [ ] Open ticket count by compound / status (for supervisors)
- [ ] Average resolution time per service category
- [ ] CSV export of submissions within a date range
- [ ] `setup_db.py` reload: safely reload `master_data.csv` without wiping submissions
- [ ] Bulk-assign units to an agent from CSV

**Implementation options:**
- A: Add a `/report` command or "📊 Reports" menu to `agent_bot.py` (approver_2 / supervisor role only)
- B: Separate lightweight web dashboard (Flask or FastAPI, same SQLite mount)
- C: Export to Google Sheets via Drive MCP

---

## Technical Debt / Known Gaps

| Item | Priority | Notes |
|------|----------|-------|
| `form_state` table is unused | Low | Bots use `context.user_data`; table exists but is never written. |
| `test_flow.py` incomplete | Low | Missing `photo_file_id` in INSERT; uses positional row access instead of named columns. |
| No `.dockerignore` | Low | `__pycache__` and `.env` get sent to build context (harmless since app is bind-mounted). |
| `master_data.csv` reload is additive | Medium | Running `setup_db.py` twice inserts duplicate `master_units` rows. Should `DELETE FROM master_units` first, or use `INSERT OR REPLACE`. |
| Photos not cleaned up | Low | Orphaned photos accumulate if submissions are deleted. |
| Existing tickets lack `compound` | Low | Tickets submitted before Phase 5 have `compound = NULL` and won't appear in approver pending lists. Acceptable — only new tickets need the full workflow. |
