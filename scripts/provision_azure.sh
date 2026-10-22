#!/usr/bin/env bash
# Provision the demo database on Azure SQL Database (free offer) and load the fixture.
#
#   az login                      # once
#   cp .env.azure.example .env.azure   # then fill in the values (gitignored)
#   bash scripts/provision_azure.sh
#
# Creates (idempotently): resource group, logical SQL server (TLS 1.2+), a free-offer serverless database that
# auto-pauses when the monthly free allowance is used up (no charges), a firewall rule for this machine, then
# loads the unmodified schema.sql and the contained least-privilege users from sql/azure/security.sql.
# sqlcmd runs from the SQL Server container image, so nothing else needs installing.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env.azure; . ./.env; set +a

SQLCMD_IMAGE=mcr.microsoft.com/mssql/server:2022-latest
FQDN="$AZ_SQL_SERVER.database.windows.net"

echo "[azure] resource group $AZ_RG ($AZ_LOCATION)"
az group create -n "$AZ_RG" -l "$AZ_LOCATION" -o none

if ! az sql server show -g "$AZ_RG" -n "$AZ_SQL_SERVER" -o none 2>/dev/null; then
  echo "[azure] SQL server $AZ_SQL_SERVER"
  az sql server create -g "$AZ_RG" -n "$AZ_SQL_SERVER" -l "$AZ_LOCATION" \
    -u "$AZ_SQL_ADMIN" -p "$AZ_SQL_ADMIN_PASSWORD" --minimal-tls-version 1.2 -o none
fi

if ! az sql db show -g "$AZ_RG" -s "$AZ_SQL_SERVER" -n "$AZ_SQL_DB" -o none 2>/dev/null; then
  echo "[azure] free-offer database $AZ_SQL_DB (auto-pauses when the free allowance is used)"
  az sql db create -g "$AZ_RG" -s "$AZ_SQL_SERVER" -n "$AZ_SQL_DB" \
    --edition GeneralPurpose --family Gen5 --capacity 2 --compute-model Serverless \
    --use-free-limit true --free-limit-exhaustion-behavior AutoPause -o none
fi

MY_IP=$(curl -s https://api.ipify.org)
echo "[azure] firewall rule for this machine ($MY_IP)"
az sql server firewall-rule create -g "$AZ_RG" -s "$AZ_SQL_SERVER" -n provisioning-host \
  --start-ip-address "$MY_IP" --end-ip-address "$MY_IP" -o none

sqlcmd() {
  docker run --rm -i -v "$PWD:/work:ro" "$SQLCMD_IMAGE" /opt/mssql-tools18/bin/sqlcmd \
    -S "$FQDN" -d "$AZ_SQL_DB" -U "$AZ_SQL_ADMIN" -P "$AZ_SQL_ADMIN_PASSWORD" -N -b -I -l 60 "$@"
}

echo "[azure] resetting tables and loading schema.sql (unmodified)"
sqlcmd -Q "DROP TABLE IF EXISTS dbo.Invoices_Billing, dbo.GL_Journals, dbo.Sales_Contracts, dbo.Units;"
sqlcmd -i /work/schema.sql

echo "[azure] contained least-privilege users"
sqlcmd -v SALES_DB_PASSWORD="$SALES_DB_PASSWORD" MGMT_DB_PASSWORD="$MGMT_DB_PASSWORD" \
  -i /work/sql/azure/security.sql

echo "[azure] row counts"
sqlcmd -h -1 -W -Q "SET NOCOUNT ON; SELECT CONCAT('Units=', (SELECT COUNT(*) FROM dbo.Units),
  ' Sales_Contracts=', (SELECT COUNT(*) FROM dbo.Sales_Contracts),
  ' Invoices_Billing=', (SELECT COUNT(*) FROM dbo.Invoices_Billing),
  ' GL_Journals=', (SELECT COUNT(*) FROM dbo.GL_Journals));"
echo "[azure] done: $FQDN / $AZ_SQL_DB"
