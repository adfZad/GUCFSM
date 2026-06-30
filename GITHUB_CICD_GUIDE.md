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

## Step 2 — Get Publish Profile from Azure

1. Go to Azure Portal → Function App `guc-field-service-func`
2. Overview → **Get publish profile** → Download
3. Open the downloaded `.PublishSettings` file
4. Copy its full content

## Step 3 — Add GitHub Secret

1. GitHub repo → Settings → Secrets and variables → Actions
2. New repository secret
3. Name: `AZURE_FUNCTIONAPP_PUBLISH_PROFILE`
4. Value: paste the publish profile content
5. Add secret

## Step 4 — Push to trigger deployment

Any push to `main` branch that changes files in `field-service-bot/app/` will auto-deploy:

```powershell
git add .
git commit -m "Update bot code"
git push
```

## Workflow Steps (automated)

```
Push to GitHub
    │
    ▼
GitHub Actions runner
    ├── Checkout code
    ├── Setup Python 3.11
    ├── Install requirements (python-telegram-bot, pyodbc, etc.)
    ├── Verify all .py files compile
    ├── Package: copy 8 files → deploy.zip
    └── Deploy to Azure Function App
```
