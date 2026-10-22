-- Least-privilege runtime principals. The API never connects as sa.
--   southcity_sales : SELECT on Units, Sales_Contracts, Invoices_Billing; explicit DENY on GL_Journals
--   southcity_mgmt  : SELECT on the four application tables only (no db_datareader, so future
--                     tables/views are not readable by default)
-- Neither principal can write, execute, or change schema. This is the last line of defence: it holds
-- even if every application-level check were bypassed.

USE [master];
GO
IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'southcity_sales') DROP LOGIN [southcity_sales];
IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'southcity_mgmt') DROP LOGIN [southcity_mgmt];
GO
CREATE LOGIN [southcity_sales] WITH PASSWORD = N'$(SALES_DB_PASSWORD)', CHECK_POLICY = ON,
    DEFAULT_DATABASE = [$(DB_NAME)];
CREATE LOGIN [southcity_mgmt] WITH PASSWORD = N'$(MGMT_DB_PASSWORD)', CHECK_POLICY = ON,
    DEFAULT_DATABASE = [$(DB_NAME)];
-- Do not let runtime logins enumerate other databases on the instance.
DENY VIEW ANY DATABASE TO [southcity_sales];
DENY VIEW ANY DATABASE TO [southcity_mgmt];
GO

USE [$(DB_NAME)];
GO
CREATE USER [southcity_sales] FOR LOGIN [southcity_sales] WITH DEFAULT_SCHEMA = [dbo];
CREATE USER [southcity_mgmt] FOR LOGIN [southcity_mgmt] WITH DEFAULT_SCHEMA = [dbo];
GO

GRANT SELECT ON OBJECT::dbo.Units            TO [southcity_sales], [southcity_mgmt];
GRANT SELECT ON OBJECT::dbo.Sales_Contracts  TO [southcity_sales], [southcity_mgmt];
GRANT SELECT ON OBJECT::dbo.Invoices_Billing TO [southcity_sales], [southcity_mgmt];
GRANT SELECT ON OBJECT::dbo.GL_Journals      TO [southcity_mgmt];
DENY  SELECT ON OBJECT::dbo.GL_Journals      TO [southcity_sales];

-- Database-scope denials: DENY wins over any grant inherited later through roles.
DENY INSERT, UPDATE, DELETE, EXECUTE, ALTER, REFERENCES,
     CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE FUNCTION, CREATE SCHEMA
    TO [southcity_sales], [southcity_mgmt];
GO
