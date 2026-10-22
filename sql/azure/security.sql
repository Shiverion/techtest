-- Least-privilege runtime principals for Azure SQL Database.
-- Same permissions as sql/security.sql, but as contained database users: Azure SQL Database has no
-- server-level USE master / DENY VIEW ANY DATABASE, and contained users keep the database portable.
--   southcity_sales : SELECT on Units, Sales_Contracts, Invoices_Billing; explicit DENY on GL_Journals
--   southcity_mgmt  : SELECT on the four application tables only
-- Neither can write, execute, or change schema. Run while connected to the application database.

IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'southcity_sales') DROP USER [southcity_sales];
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'southcity_mgmt') DROP USER [southcity_mgmt];
GO
CREATE USER [southcity_sales] WITH PASSWORD = N'$(SALES_DB_PASSWORD)', DEFAULT_SCHEMA = [dbo];
CREATE USER [southcity_mgmt] WITH PASSWORD = N'$(MGMT_DB_PASSWORD)', DEFAULT_SCHEMA = [dbo];
GO

GRANT SELECT ON OBJECT::dbo.Units            TO [southcity_sales], [southcity_mgmt];
GRANT SELECT ON OBJECT::dbo.Sales_Contracts  TO [southcity_sales], [southcity_mgmt];
GRANT SELECT ON OBJECT::dbo.Invoices_Billing TO [southcity_sales], [southcity_mgmt];
GRANT SELECT ON OBJECT::dbo.GL_Journals      TO [southcity_mgmt];
DENY  SELECT ON OBJECT::dbo.GL_Journals      TO [southcity_sales];

DENY INSERT, UPDATE, DELETE, EXECUTE, ALTER, REFERENCES,
     CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE FUNCTION, CREATE SCHEMA
    TO [southcity_sales], [southcity_mgmt];
GO
