# Azure Function Deployment — Complete Manual

**Your deploy package is ready at:** `D:\Gravity\GUC Fields\GUC Field Service App\deploy\`

---

## How It Works

```
Telegram
  │  POST /api/webhook/resident       POST /api/webhook/agent
  ▼                                      ▼
┌──────────────────────────────────────────────────┐
│              Azure Function App                   │
│  function_app.py                                 │
│  ├── webhook_resident() → bot.create_application │
│  └── webhook_agent()    → agent_bot.create_app   │
│                       │                          │
│              persistence.py → Azure SQL          │
│              blob_storage.py → Azure Blob        │
└──────────────────────────────────────────────────┘
```

3 webhook endpoints in one Function App:
- `POST /api/webhook/resident` — @GUCMain1bot
- `POST /api/webhook/agent` — @GUCMain2bot
- `POST /api/webhook/setup` — registers webhooks with Telegram (run once)

---

## PART A — Provision Azure Resources (Azure Portal)

### A1. Open Azure Portal
- Go to https://portal.azure.com
- Login with your organization account

### A2. Create Resource Group
1. Search "Resource groups" → + Create
2. Name: `guc-field-service-rg`
3. Region: West Europe
4. Review + Create

### A3. Create Storage Account
1. Search "Storage accounts" → + Create
2. Resource group: `guc-field-service-rg`
3. Name: `gucfieldstorage` (add numbers if taken — must be globally unique)
4. Region: West Europe
5. Performance: Standard
6. Redundancy: LRS
7. Review + Create

### A4. Create Function App
1. Search "Function App" → + Create
2. **Basics:**
   - Publish: Code
   - Runtime stack: Python
   - Version: 3.11
   - Region: West Europe
   - OS: Linux
3. **Hosting:**
   - Storage account: select `gucfieldstorage`
   - Plan type: Consumption (Serverless)
4. Name: `guc-field-service-func`
5. Review + Create → wait for deployment

### A5. Create Azure SQL Server
1. Search "SQL servers" → + Create
2. Resource group: `guc-field-service-rg`
3. Server name: `guc-field-sql`
4. Location: West Europe
5. Authentication: SQL
6. Admin: `gucadmin`
7. Password: `YourStrongP@ssw0rd1` (save this!)
8. Review + Create

### A6. Create SQL Database
1. Go to your SQL server → + Create database
2. Name: `field_service`
3. Tier: Basic (5 DTU, 2 GB)
4. Create

### A7. Allow Azure access to SQL
1. Go to SQL server → Networking
2. Enable: "Allow Azure services and resources to access this server" → Save
3. Also add your own IP (for SSMS access)

### A8. Create Blob Storage Container
1. Go to Storage account `gucfieldstorage`
2. Data storage → Containers → + Container
3. Name: `photos`
4. Public access: Private

---

## PART B — Create the Database Schema (SSMS)

### B1. Connect to Azure SQL
1. Open SQL Server Management Studio (SSMS)
2. Server name: `guc-field-sql.database.windows.net`
3. Login: `gucadmin`
4. Password: `YourStrongP@ssw0rd1`
5. Connect

### B2. Run the schema + seed
Open the file: `D:\Gravity\GUC Fields\GUC Field Service App\migration\sql\schema.sql`
- Copy ALL content
- Paste into New Query window (make sure database = `field_service`)
- Execute

Then open: `D:\Gravity\GUC Fields\GUC Field Service App\migration\sql\seed.sql`
- Copy ALL content
- Paste into New Query
- Execute

Verify:
```sql
SELECT 'agents', COUNT(*) FROM agents UNION ALL
SELECT 'services', COUNT(*) FROM services UNION ALL
SELECT 'master_units', COUNT(*) FROM master_units;
-- Should show: agents=38, services=49, master_units=7
```

---

## PART C — Configure Function App Settings

### C1. Go to Function App → Environment variables
1. Open `guc-field-service-func` in portal
2. Settings → Environment variables

### C2. Add these app settings:

| Name | Value |
|------|-------|
| `BOT_TOKEN` | `8957910574:AAGha7WF02Jd6QAjkRQ4AgOuVSVwp_bYgtU` |
| `AGENT_BOT_TOKEN` | `8692964459:AAHFKIXnlNz-VpRKb-i-5WYYEC5Ht7fGoP8` |
| `DB_CONNECTION_STRING` | `Driver={ODBC Driver 18 for SQL Server};Server=guc-field-sql.database.windows.net;Database=GUCFSM;Uid=gucadmin;Pwd=YourStrongP@ssw0rd1;Encrypt=yes;TrustServerCertificate=no;` |
| `WEBHOOK_SECRET` | (generate: open PowerShell → `python -c "import secrets; print(secrets.token_hex(16))"`) |
| `NOTIFICATIONS_ENABLED` | `true` |

Click Apply → Confirm.

---

## PART D — Deploy Code to Azure

### D1. Install Azure CLI (if not done)
Already installed — verified.

### D2. Create deployment ZIP

In PowerShell:
```powershell
cd "D:\Gravity\GUC Fields\GUC Field Service App\deploy"
Compress-Archive -Path * -DestinationPath deploy.zip -Force
```

### D3. Deploy

```powershell
az functionapp deployment source config-zip `
  --resource-group guc-field-service-rg `
  --name guc-field-service-func `
  --src deploy.zip
```

Wait for "Deployment successful."

---

## PART E — Register Webhooks with Telegram

### E1. Find your Function URL
1. Portal → Function App → Overview
2. Copy the URL: `https://guc-field-service-func.azurewebsites.net`

### E2. Run setup endpoint

```powershell
$base = "https://guc-field-service-func.azurewebsites.net"
curl -X POST "$base/api/webhook/setup"
```

You should see:
```json
{"resident": {"url": "...", "ok": true}, "agent": {"url": "...", "ok": true}}
```

From this point, Telegram sends all updates to Azure. The `run_polling()` bots in your local terminals become inactive for these tokens.

---

## PART F — Verify

### F1. Quick health check
```powershell
curl "https://guc-field-service-func.azurewebsites.net/api/webhook/setup"
```

### F2. Test on Telegram
1. Send `/start` to @GUCMain2bot
2. Should work identically to local version

### F3. Check Function logs
1. Portal → Function App → Log stream
2. You'll see each webhook invocation in real-time

---

## Rollback (if needed)

```powershell
# Run this Python to revert to polling:
python -c "
import asyncio
from telegram import Bot
async def main():
    bot = Bot('8957910574:AAGha7WF02Jd6QAjkRQ4AgOuVSVwp_bYgtU')
    await bot.delete_webhook()
    bot2 = Bot('8692964459:AAHFKIXnlNz-VpRKb-i-5WYYEC5Ht7fGoP8')
    await bot2.delete_webhook()
    print('Webhooks deleted. Local polling bots will work again.')
asyncio.run(main())
"
# Then restart run_agent.bat and run_resident.bat
```

---

## Architecture Summary

```
                        Internet
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
     POST /api/webhook/resident   POST /api/webhook/agent
              │                         │
    ┌─────────┴─────────────────────────┴──────────┐
    │         Azure Function App                    │
    │         guc-field-service-func                │
    │                                              │
    │  function_app.py                             │
    │  ├── webhook_resident() → bot.py             │
    │  └── webhook_agent()    → agent_bot.py       │
    │                                              │
    │  persistence.py → conversation_state table   │
    │  blob_storage.py → photos container          │
    └──────────────────┬───────────────────────────┘
                       │
       ┌───────────────┼───────────────┐
       ▼               ▼               ▼
    Azure SQL       Azure Blob      App Settings
    field_service   photos          (tokens, secrets)
```

## Important: Copy your Excel data too

After deploying, run the Excel import again pointing to Azure SQL:

```powershell
# Update DB_CONNECTION_STRING to point to Azure SQL, then:
python import_master.py
```
