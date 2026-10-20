-- DUMMY SCHEMA FOR AST PROPERTY PLUS ERP TEST (SOUTHCITY)

CREATE TABLE Units (
    UnitID INT PRIMARY KEY,
    UnitNumber VARCHAR(20),
    Tower VARCHAR(20),
    Status VARCHAR(20) -- Occupied, Vacant, Sold
);

CREATE TABLE Sales_Contracts (
    ContractID INT PRIMARY KEY,
    UnitID INT FOREIGN KEY REFERENCES Units(UnitID),
    CustomerName VARCHAR(100),
    SalesAmount DECIMAL(18,2),
    ContractDate DATE
);

CREATE TABLE Invoices_Billing (
    InvoiceID INT PRIMARY KEY,
    ContractID INT FOREIGN KEY REFERENCES Sales_Contracts(ContractID),
    PeriodMonth INT,
    PeriodYear INT,
    AmountBilled DECIMAL(18,2),
    AmountPaid DECIMAL(18,2),
    DueDate DATE
);

CREATE TABLE GL_Journals (
    JournalID INT PRIMARY KEY,
    AccountCode VARCHAR(20),
    AccountName VARCHAR(100),
    Debit DECIMAL(18,2),
    Credit DECIMAL(18,2),
    TransactionDate DATE
);

-- =========================================================
-- DUMMY DATA INSERTIONS
-- =========================================================

-- 1. UNITS DATA (10 Units: Summer Tower & Spring Tower)
INSERT INTO Units VALUES 
    (1, 'SM 1111', 'Summer Tower', 'Sold'), 
    (2, 'SM 1112', 'Summer Tower', 'Sold'),
    (3, 'SM 1115', 'Summer Tower', 'Sold'),
    (4, 'SM 1201', 'Summer Tower', 'Vacant'),
    (5, 'SM 1202', 'Summer Tower', 'Sold'),
    (6, 'SP 1112', 'Spring Tower', 'Sold'),
    (7, 'SP 1113', 'Spring Tower', 'Sold'),
    (8, 'SP 1114', 'Spring Tower', 'Sold'),
    (9, 'SP 1201', 'Spring Tower', 'Vacant'),
    (10, 'SP 1202', 'Spring Tower', 'Sold');

-- 2. SALES CONTRACTS DATA (5 Customers across 8 Sold Units)
INSERT INTO Sales_Contracts VALUES 
    (101, 1, 'PT Sukses Bersama', 1500000000, '2026-01-15'),
    (102, 2, 'Budi Santoso', 1250000000, '2026-02-10'),
    (103, 3, 'Siti Rahmawati', 1300000000, '2026-02-20'),
    (104, 5, 'CV Megah Karya', 1800000000, '2026-03-05'),
    (105, 6, 'PT Sukses Bersama', 1600000000, '2026-03-12'),
    (106, 7, 'Hendrik Wijaya', 1400000000, '2026-04-01'),
    (107, 8, 'Budi Santoso', 1450000000, '2026-04-18'),
    (108, 10, 'Siti Rahmawati', 1550000000, '2026-05-02');

-- 3. INVOICES & BILLING DATA (Paid & Arrears/Unpaid Scenarios)
INSERT INTO Invoices_Billing VALUES 
    -- PT Sukses Bersama (SM 1111 & SP 1112)
    (501, 101, 10, 2026, 15000000, 0, '2026-10-10'),        -- Unpaid (Arrears)
    (502, 105, 10, 2026, 16000000, 16000000, '2026-10-10'), -- Fully Paid

    -- Budi Santoso (SM 1112 & SP 1114)
    (503, 102, 9, 2026, 12500000, 12500000, '2026-09-10'),  -- Fully Paid
    (504, 102, 10, 2026, 12500000, 0, '2026-10-10'),       -- Unpaid (Arrears)
    (505, 107, 10, 2026, 14500000, 0, '2026-10-10'),       -- Unpaid (Arrears)

    -- Siti Rahmawati (SM 1115 & SP 1202)
    (506, 103, 10, 2026, 13000000, 13000000, '2026-10-10'), -- Fully Paid
    (507, 108, 10, 2026, 15500000, 5000000, '2026-10-10'),  -- Partially Paid (Arrears Rp 10.5M)

    -- CV Megah Karya (SM 1202)
    (508, 104, 10, 2026, 18000000, 0, '2026-10-10'),       -- Unpaid (Arrears)

    -- Hendrik Wijaya (SP 1113)
    (509, 106, 10, 2026, 14000000, 14000000, '2026-10-10'); -- Fully Paid

-- 4. GL JOURNALS DATA (Financial Accounting Ledger)
INSERT INTO GL_Journals VALUES 
    (901, '1101-01', 'Kas & Bank', 1500000000, 0, '2026-01-15'),
    (902, '4101-01', 'Pendapatan Sales Unit', 0, 1500000000, '2026-01-15'),
    (903, '1101-01', 'Kas & Bank', 1250000000, 0, '2026-02-10'),
    (904, '4101-01', 'Pendapatan Sales Unit', 0, 1250000000, '2026-02-10'),
    (905, '1101-01', 'Kas & Bank', 1300000000, 0, '2026-02-20'),
    (906, '4101-01', 'Pendapatan Sales Unit', 0, 1300000000, '2026-02-20'),
    (907, '1101-01', 'Kas & Bank', 1800000000, 0, '2026-03-05'),
    (908, '4101-01', 'Pendapatan Sales Unit', 0, 1800000000, '2026-03-05'),
    (909, '1101-01', 'Kas & Bank', 1600000000, 0, '2026-03-12'),
    (910, '4101-01', 'Pendapatan Sales Unit', 0, 1600000000, '2026-03-12');
