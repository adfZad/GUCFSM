# GUC Field Service App — Project Details

**Date:** 2026-06-18
**Location:** `field-service-bot/`

---

## 1. Project Summary

A **field service management system** for a residential compound community (GUC) with three compounds: **Diamond, Pearl, and Sapphire**. The system enables residents to submit maintenance/facilities requests, field agents to complete work, and approvers (two-level chain) to sign off — all through Telegram bots with no external services.

---

## 2. Business Domain

**Industry:** Property Management / Facility Management

**Use Case:** End-to-end service request lifecycle for a multi-compound residential community:

| Role | Actor | Actions |
|------|-------|---------|
| **Resident** | Property owner/tenant | Submit maintenance/facilities requests, report emergencies, follow up on tickets |
| **Field Agent** | Maintenance staff | View assigned tickets, mark work done (with cost + completion photo) |
| **Approver 1** | Supervisor/Team lead | First-level approval: confirm cost, approve or reject work |
| **Approver 2** | Management | Final sign-off: approve or reject |

**Ticket Lifecycle:**
```
submitted → approved_1 → approved → closed (work completed)
                       ↓
                    rejected → re-submitted → restarts chain
```

---

## 3. Technology Stack

| Technology | Version/Package | Purpose |
|------------|----------------|---------|
| **Python** | 3.11 (slim Docker image) | Application runtime |
| **python-telegram-bot** | 22.6 | Telegram Bot API wrapper — deterministic state-machine bots |
| **SQLite 3** | (bundled with Python) | Relational database — single source of truth |
| **Docker** | Dockerfile + docker-compose.yml | Containerization, restart policy, bind mounts |
| **Supervisor (supervisord)** | apt package | Process manager inside container — runs both bots |
| **Telegram Bot API** | HTTP long-polling | User interface and push notifications |

**Deployment:** Single Docker container (`field-service-bot`) with `restart: unless-stopped`. Bind-mounted host directories for code, data, and logs.

**Key design:** Zero LLM, pure deterministic state machine. Both bots share one SQLite DB. Coordination via DB state changes + Telegram push notifications.

---

## 4. Architecture

```
┌──────────────────────────────────────────┐
│  Docker Container: field-service-bot     │
│                                          │
│  supervisord                             │
│  ├── resident-bot  → /app/bot.py         │
│  └── agent-bot     → /app/agent_bot.py   │
│                                          │
│  Shared SQLite DB: /data/field_service.db│
│  Photos:            /data/photos/        │
│  Logs:              /logs/               │
└──────────────────────────────────────────┘
         ↕ long-polling
   Telegram Bot API
```

**Two Telegram Bots:**

| Bot Handle | Display Name | File | Users |
|------------|-------------|------|-------|
| `@GUCMain1bot` | GUCMaintenance | `bot.py` | Residents |
| `@GUCMain2bot` | GUCMaintService | `agent_bot.py` | Field agents, Approver 1, Approver 2 |

---

## 5. Source Code

### Core Python Files

| File | Lines (est.) | Description |
|------|-------------|-------------|
| `app/bot.py` | ~1200+ | Resident bot — 16 conversation states, phone/TG lookup, service drill-down, submissions |
| `app/agent_bot.py` | ~2200+ | Agent bot — 35 conversation states, role-based menu, hierarchy drill-down, approvals, notifications |
| `app/setup_db.py` | ~300+ | Schema migration (idempotent), CSV data loading, agent seeding |
| `app/test_flow.py` | ~100+ | DB flow test script (incomplete) |

### Data Files

| File | Description |
|------|-------------|
| `app/master_data.csv` | Resident phone/TG → unit mapping |
| `app/services.csv` | 3-level service hierarchy (49 rows: 7 maint categories, 7 facility categories) |
| `data/field_service.db` | SQLite runtime database |

### Infrastructure Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Python 3.11-slim, supervisord, pip install |
| `docker-compose.yml` | Single service, bind mounts, env_file |
| `supervisord.conf` | Two program definitions, auto-restart |
| `requirements.txt` | `python-telegram-bot==22.6` |
| `.env` | Tokens + path overrides (not committed) |

---

## 6. Database Schema (7 tables)

| Table | Rows | Purpose |
|-------|------|---------|
| `master_units` | Residents × units | Phone/TG → unit list lookup (resident bot) |
| `master_units_hierarchy` | Units × compounds | Compound/type/villa/building/flat + field agent assignment |
| `submissions` | All tickets | Central workflow table, 25+ columns, written by both bots |
| `services` | 49 rows | 3-level category hierarchy (main → category → sub) |
| `agents` | 14 rows (seeded) | Role registry: field_agent, approver_1, approver_2 per compound |
| `approvals` | Per approval action | Immutable audit trail (who did what, when, with what note) |
| `form_state` | 0 (unused) | Reserved table, not used at runtime |

---

## 7. Key Users (Seeded)

| Name | TG ID | Role(s) |
|------|-------|---------|
| Afsal Khan | 8976446718 | field_agent |
| Riaz | 8580506857 | field_agent + approver_1 (all) + approver_2 (all) |
| Fasil | 7228949233 | approver_1 (Diamond, Pearl, Sapphire) |
| Shahbaz | 8767995042 | approver_2 (Diamond, Pearl, Sapphire) |

---

## 8. Project Status

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Resident Bot — Core Form | Done |
| 2 | Agent Bot — Hierarchy + New Ticket | Done |
| 3 | Agent Bot — Existing Tickets + Mark Done | Done |
| 4 | Docker Container Setup | Done |
| 5 | Approver Workflow (2-level) | Done |
| 6 | Push Notifications | Done |
| 7 | Resident Bot — Ticket Status Lookup | Pending |
| 8 | Reporting & Admin | Pending |

**Completed phases:** 6 of 8

---

## 9. Documentation

The project is extensively documented with three manuals:

| Document | Target Audience | Coverage |
|----------|----------------|---------|
| `CLAUDE.md` | Developers | Architecture, daily commands, code patterns, DB schema, how to add users |
| `TECHNICAL_MANUAL.md` | DevOps / Admin | Full API reference, all tables/columns, all handlers, deployment ops, error handling |
| `USER_MANUAL.md` | End users + Admins | Step-by-step walkthrough for all roles, admin SQL recipes |
| `PROJECT_PLAN.md` | Project managers | Phase status, feature backlog, technical debt tracker |

Also includes `Copy of GUC Maintenance Master.xlsx` at the parent level (likely the original master data spreadsheet).
