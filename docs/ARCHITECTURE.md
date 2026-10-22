# System design

How the SouthCity AI Data Query Assistant is put together, from the deployment down to a single request.
The security reasoning behind these choices is in [`SECURITY.md`](SECURITY.md).

## 1. System context

```mermaid
flowchart LR
    U(["Reviewer / user<br/>browser"])
    subgraph Vercel["Vercel · Python function · sin1 Singapore"]
        UI["Chat UI<br/>/ui (static HTML/CSS/JS)"]
        API["FastAPI<br/>/api/v1/ask · /api/v1/ask/stream"]
    end
    LLM["Moonshot Kimi k2.6<br/>OpenAI-compatible API"]
    subgraph Azure["Azure SQL Database · free offer · Southeast Asia"]
        DB[("SouthCity<br/>schema.sql, unmodified")]
    end
    GH["GitHub<br/>Shiverion/techtest"]

    U -- HTTPS --> UI
    UI -- "fetch + Server-Sent Events" --> API
    API -- "question + role-scoped schema" --> LLM
    LLM -- "JSON plan: SELECTs + answer template" --> API
    API -- "TLS · per-role read-only user" --> DB
    GH -- "push to main = production deploy" --> Vercel
```

Locally the same code runs with `uvicorn` and SQL Server 2022 in Docker instead of Vercel and Azure; nothing in
the application changes, only the environment variables.

## 2. Components

```mermaid
flowchart TB
    subgraph app["src/southcity_assistant"]
        A["app.py<br/>routes, role resolution,<br/>rate limit, concurrency, CSP"]
        P["pipeline.py<br/>orchestration, repair round,<br/>deadline, progress events"]
        subgraph sec["security/"]
            G["input_guard.py<br/>write intent, injection flag"]
            R["roles.py<br/>roles, table allowlists,<br/>restricted topics"]
            S["sql_policy.py<br/>AST allowlist, CTE scope,<br/>joins, limits"]
        end
        E["entities.py<br/>name/unit hints,<br/>filter existence checks"]
        L["llm.py<br/>prompt, strict JSON plan"]
        D["db.py<br/>per-role connections,<br/>execution, error classes"]
        RE["rendering.py<br/>Rupiah, tables,<br/>template answers"]
        SC["schema.py<br/>schema + business rules"]
    end
    A --> P
    P --> G & R & E & L & S & D & RE
    L --> SC
    E --> D
```

## 3. Request workflow

One question, end to end. Each `stage` arrow is a progress event the UI shows live; the request stops at the first
layer that refuses (see section 4).

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant UI as Chat UI
    participant API as FastAPI app
    participant Guard as Input guard + role scope
    participant DB as SQL Server (role login)
    participant LLM as Kimi k2.6
    participant Policy as SQL policy (AST)

    User->>UI: question + role (Sales_Staff / Management)
    UI->>API: POST /api/v1/ask/stream
    API->>API: resolve role, rate limit, concurrency slot
    API-->>UI: stage: guard
    API->>Guard: write intent? ledger topic for Sales_Staff?
    Guard-->>API: allowed
    API-->>UI: stage: scope, entities
    API->>DB: parameterized lookups (customer, unit, account names)
    DB-->>API: exact matches / unknown unit
    API-->>UI: stage: planning
    API->>LLM: role-scoped schema + business rules + question as data
    LLM-->>API: JSON plan (1-3 SELECTs + answer template)
    API-->>UI: stage: policy
    API->>Policy: parse T-SQL, allowlist, tables vs role, joins, limits
    Policy-->>API: validated SQL (re-rendered, re-validated)
    API-->>UI: sql (shown before it runs)
    API-->>UI: stage: checking
    API->>DB: re-run entity filters (not found / ambiguous?)
    API-->>UI: stage: executing
    API->>DB: validated SELECT, row and byte caps, timeout
    DB-->>API: rows
    API->>API: Rupiah formatting, fill template from results
    API-->>UI: result (answer, tables, SQL, HTTP status)
    UI-->>User: KPI tile, tables, SQL, step trail
```

If SQL Server reports a syntax or column error, the plan goes back to the model once with a sanitized error
message, and the policy, checking and executing steps run again on the new plan. Policy rejections, permission
errors and timeouts are never retried.

## 4. Where a request can be refused

```mermaid
flowchart TD
    Q["Question + role"] --> ID{"Role valid?<br/>key matches role?"}
    ID -- no --> X1["400/401/403 · identity"]
    ID -- yes --> RL{"Within rate limit?"}
    RL -- no --> X2["429 · rate_limited"]
    RL -- yes --> IG{"Asks to change data?"}
    IG -- yes --> X3["403 · write_request<br/>(Q5, both roles)"]
    IG -- no --> RS{"Ledger data requested<br/>by Sales_Staff?"}
    RS -- yes --> X4["403 · forbidden_data<br/>(Q4 as Sales_Staff)"]
    RS -- no --> EN{"Unit code exists?"}
    EN -- no --> X5["200 · not_found"]
    EN -- yes --> M["Model writes a plan"]
    M --> MR{"Model refuses<br/>or asks to clarify?"}
    MR -- yes --> X6["403 model_refused · 200 clarify"]
    MR -- no --> SP{"SQL inside the allowed subset<br/>and the role's tables?"}
    SP -- no --> X7["403 · policy code<br/>(not_select, forbidden_table, ...)"]
    SP -- yes --> FC{"Filtered customer/unit exists<br/>and is unambiguous?"}
    FC -- no --> X8["200 · not_found / ambiguous"]
    FC -- yes --> DBX{"SQL Server permits it<br/>for this login?"}
    DBX -- no --> X9["403 · database_denied"]
    DBX -- yes --> OK["200 · answer + tables + SQL"]
```

The last check, SQL Server's own permissions, holds even if every check above it were bypassed: the runtime logins
cannot write, and the Sales_Staff login is explicitly denied `GL_Journals`.

## 5. Data model and access per role

```mermaid
erDiagram
    Units ||--o{ Sales_Contracts : "UnitID"
    Sales_Contracts ||--o{ Invoices_Billing : "ContractID"
    Units {
        int UnitID PK
        varchar UnitNumber
        varchar Tower
        varchar Status
    }
    Sales_Contracts {
        int ContractID PK
        int UnitID FK
        varchar CustomerName
        decimal SalesAmount
        date ContractDate
    }
    Invoices_Billing {
        int InvoiceID PK
        int ContractID FK
        int PeriodMonth
        int PeriodYear
        decimal AmountBilled
        decimal AmountPaid
        date DueDate
    }
    GL_Journals {
        int JournalID PK
        varchar AccountCode
        varchar AccountName
        decimal Debit
        decimal Credit
        date TransactionDate
    }
```

| Table | `Sales_Staff` (login `southcity_sales`) | `Management` (login `southcity_mgmt`) |
|---|---|---|
| Units, Sales_Contracts, Invoices_Billing | SELECT | SELECT |
| GL_Journals | **DENY** | SELECT |
| Any table: INSERT / UPDATE / DELETE / ALTER / EXECUTE / CREATE | DENY | DENY |

## 6. Deployment and secrets

```mermaid
flowchart LR
    Dev["Developer machine"] -- "git push" --> GH["GitHub"]
    GH -- "auto deploy" --> V["Vercel project<br/>sensitive env vars"]
    Dev -- "scripts/provision_azure.sh<br/>(admin login, once)" --> AZ[("Azure SQL")]
    V -- "runtime users only" --> AZ
    V -- "API key" --> K["Moonshot"]
```

| Secret | Where it lives | Who uses it |
|---|---|---|
| Azure SQL admin password | Local `.env.azure` (gitignored) | Provisioning script only; never deployed |
| `southcity_sales` / `southcity_mgmt` passwords | Azure (as the users' credentials) and Vercel (sensitive, write-only) | The app at runtime |
| Kimi API key | Vercel (sensitive, write-only) and local `.env` | The app at runtime |
| Local Docker passwords | Local `.env` (gitignored) | Local runs only |

None of these are in the repository. Sensitive Vercel variables cannot be read back from the dashboard or CLI,
only used by the running function. The browser never receives a secret: it only calls the app's own API.

## 7. Key design decisions

- **Plain pipeline, no agent framework.** One model call plus at most one repair is a straight line; plain code is
  easier to audit than a LangChain/LangGraph graph.
- **The model never touches the database.** It returns a plan; only SQL that passed the policy runs, under a login
  that cannot write.
- **The model never writes figures.** Numbers in the answer come from result placeholders.
- **Same code everywhere.** Local Docker, tests and the cloud demo differ only in environment variables.
