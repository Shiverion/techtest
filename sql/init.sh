#!/bin/bash
# Provision the SouthCity database. Fails fast on any SQL error (-b).
set -euo pipefail

SQLCMD=(/opt/mssql-tools18/bin/sqlcmd -S db -U sa -P "$MSSQL_SA_PASSWORD" -C -b -I)

echo "[init] recreating database ${DB_NAME}"
"${SQLCMD[@]}" -v DB_NAME="$DB_NAME" -i /sql/bootstrap.sql

echo "[init] loading fixture schema.sql (unmodified)"
"${SQLCMD[@]}" -d "$DB_NAME" -i /fixture/schema.sql

echo "[init] creating least-privilege runtime logins"
"${SQLCMD[@]}" -d "$DB_NAME" \
  -v DB_NAME="$DB_NAME" SALES_DB_PASSWORD="$SALES_DB_PASSWORD" MGMT_DB_PASSWORD="$MGMT_DB_PASSWORD" \
  -i /sql/security.sql

echo "[init] verifying row counts"
"${SQLCMD[@]}" -d "$DB_NAME" -h -1 -W -Q "SET NOCOUNT ON;
SELECT CONCAT('Units=', (SELECT COUNT(*) FROM dbo.Units),
              ' Sales_Contracts=', (SELECT COUNT(*) FROM dbo.Sales_Contracts),
              ' Invoices_Billing=', (SELECT COUNT(*) FROM dbo.Invoices_Billing),
              ' GL_Journals=', (SELECT COUNT(*) FROM dbo.GL_Journals));"
echo "[init] done"
