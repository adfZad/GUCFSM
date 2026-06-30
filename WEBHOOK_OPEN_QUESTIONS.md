# Webhook Migration — Architecture Decisions

**Status:** All 20 decisions finalized. ✅

---

## Azure Infrastructure

| # | Question | Decision |
|---|----------|----------|
| 1 | Python Azure Functions model? | **v2** (decorators in function_app.py) |
| 2 | Function App plan? | **Organization-provided** |
| 3 | Single Function App or two? | **One** (two webhook paths) |
| 4 | Azure SQL tier? | **Basic** (5 DTU, 2 GB) |
| 5 | Photo storage? | **Azure Blob Storage** with local fallback |
| 6 | Migration source data? | **Migrate from SQLite** (clone history) |
| 7 | Deployment method? | **GitHub Actions** |

## Bot Configuration

| # | Question | Decision |
|---|----------|----------|
| 8 | Webhook secret token? | **Yes** — validate `X-Telegram-Bot-Api-Secret-Token` |
| 9 | Custom domain or default? | **Azure default** `.azurewebsites.net` |
| 10 | Webhook URL paths? | `/api/webhook/resident` + `/api/webhook/agent` |
| 11 | Drop Docker + supervisord? | **Yes** — keep Docker 48h as rollback |

## Code

| # | Question | Decision |
|---|----------|----------|
| 12 | Serialization format? | **JSON** (in persistence.py) |
| 13 | DB driver for Azure SQL? | **pyodbc** (in db.py) |
| 14 | Schema migration on startup? | **Idempotent** script (schema.sql + seed.sql) |
| 15 | Persistence table? | New **conversation_state** table |
| 16 | Both bots in one codebase? | **Yes** (function_app.py handles both) |

## Operations

| # | Question | Decision |
|---|----------|----------|
| 17 | Secrets storage? | **Function App Settings** |
| 18 | Monitoring/logging? | **Application Insights** |
| 19 | Non-production env? | **Yes** — separate Function App + DB |
| 20 | Rollback window? | **48 hours** on old Docker |
