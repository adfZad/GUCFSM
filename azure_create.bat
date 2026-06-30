@echo off
REM ============================================================
REM Create Azure resources for GUC Field Service Bot
REM Run from project root
REM ============================================================

echo Creating Resource Group...
az group create --name guc-field-service-rg --location westeurope

echo Creating App Service Plan (Linux, Consumption)...
az functionapp plan create --resource-group guc-field-service-rg --name guc-field-service-plan --location westeurope --sku Y1 --is-linux

echo Creating Function App...
az functionapp create --resource-group guc-field-service-rg --plan guc-field-service-plan --name guc-field-service-bot --runtime python --runtime-version 3.11 --os-type Linux --storage-account gucfieldsastorage

echo Creating SQL Server...
az sql server create --resource-group guc-field-service-rg --name guc-field-service-sql --admin-user gucadmin --admin-password "YourStrongP@ssw0rd1" --location westeurope

echo Creating SQL Database...
az sql db create --resource-group guc-field-service-rg --server guc-field-service-sql --name field_service --edition Basic --capacity 5

echo Allowing Azure services to access SQL...
az sql server firewall-rule create --resource-group guc-field-service-rg --server guc-field-service-sql --name AllowAzure --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0

echo Done! Resources created.
echo.
echo Next steps:
echo   1. Run T-SQL schema+seed script on Azure SQL (via SSMS or sqlcmd)
echo   2. Configure Function App settings (tokens, connection strings)
echo   3. Deploy code with: az functionapp deployment source config-zip
pause
