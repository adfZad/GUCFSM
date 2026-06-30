# GitHub CI/CD Setup Guide

## Step 1 — Push code to GitHub

```powershell
cd "D:\Gravity\GUC Fields\GUC Field Service App"
git init
git add .
git commit -m "Initial commit - GUC Field Service Bot"
git branch -M main
git remote add origin https://github.com/YOUR-ORG/YOUR-REPO.git
git push -u origin main
```

## Step 2 — Create Azure Service Principal (one-time)

Run this in PowerShell (must be logged in to Azure):

```powershell
az ad sp create-for-rbac `
  --name "guc-field-service-deploy" `
  --role contributor `
  --scopes /subscriptions/{your-subscription-id}/resourceGroups/guc-field-service-rg `
  --sdk-auth
```

This outputs JSON like:
```json
{
  "clientId": "xxx",
  "clientSecret": "xxx",
  "subscriptionId": "xxx",
  "tenantId": "xxx"
}
```

To find your subscription ID:
```powershell
az account show --query id -o tsv
```

## Step 3 — Add GitHub Secrets

Go to GitHub repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret Name | Value |
|-------------|-------|
| `AZURE_CREDENTIALS` | Paste the full JSON from Step 2 |

## Step 4 — Push to trigger deployment

```powershell
git add .
git commit -m "Update bot code"
git push
```

Monitor the run at: GitHub → Actions → Deploy to Azure Function App

## What Runs on Every Push

```
Push to main
    │
    ▼
├── Checkout code
├── azure/login (RBAC auth)
├── Setup Python 3.11
├── Verify all .py files compile
├── Package 8 files → deploy_package/
└── Azure/functions-action → deploy to guc-field-service-func
```

## Manual Trigger

Go to GitHub → Actions → "Deploy to Azure Function App" → Run workflow
