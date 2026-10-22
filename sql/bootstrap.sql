-- Recreate the application database from scratch so every run starts from the same fixture.
USE [master];
GO
IF DB_ID(N'$(DB_NAME)') IS NOT NULL
BEGIN
    ALTER DATABASE [$(DB_NAME)] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE [$(DB_NAME)];
END
GO
CREATE DATABASE [$(DB_NAME)];
GO
