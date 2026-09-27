# credit-mailer-watermark-azure-sql

The Azure port of [`credit-mailer-watermark-glue-redshift`](../credit-mailer-watermark-glue-redshift/):
the same 58,168 randomised loan offers from a South African lender, the same externalised
per-table watermark, the same star and the same contextual bandit. The extract now lands in
**Azure Blob Storage**, the watermark is stored in **Azure Table Storage**, and the warehouse is
**Azure SQL Database**, in T-SQL.

The data, the two-table split, the watermark lesson and the bandit's results are the sibling's
and are described in [its README](../credit-mailer-watermark-glue-redshift/README.md). This one is
about what moved, what T-SQL made of it, and what SQL Server lets the port prove that Redshift
could only state.

> **The load state still lives outside the code, one entity per table, and the table that proves
> it is still the one that CANNOT be loaded incrementally.**

The whole pipeline runs offline against Microsoft's own emulators: Azurite for Blob and Table
Storage, and SQL Server 2022 for the warehouse, running the same DDL and MERGE text Azure SQL
Database receives. The four-run demo takes **about 22 s** end to end, and the test suite runs **42
tests in about 40 s**.

## Architecture

```
 MySQL                         plain Python jobs                  Azure SQL Database
 credit_mailer                 (python-jobs/)                     db_credit_mailer
 ├── mail_offers        ──►  mysql-extraction.py            ──►  Blob: credit-mailer-lab/
 │     load_column=wave        reads the watermark from             raw_landing_zone/credit_mailer_db/
 │     INCREMENTAL             Table Storage, writes it back        <table>/data.csv
 └── client_attributes                                                    │
       load_column absent ┌── Table Storage ──────────────────┐           ▼
       FULL LOAD          │ IncrementalLoadConfigurations      │  azure-sql-raw-ingestion.py
                          │  credit_mailer / mail_offers  wave │    blob → fast_executemany → tmp_<table>
                          │  credit_mailer / client_attr.  —   │    MERGE → raw_zone.<table>
                          └────────────────────────────────────┘           │
                                                                           ▼
                                                            azure-sql-processed-layer.py
                                                              dim_client · fact_mailer
                                                                           │
                                                              refinery-path3.py
                                                              Steps 4–10, Path 3 · Beta–Bernoulli · SNIPS
                                                                           ▼
                                                              bandit_posterior · bandit_policy_value
```

`orchestration/run-chain.py` runs the six states in order, each as its own process, and stops at
the first one that fails. That is what the sibling's Step Functions state machine does, and it
keeps the same six state names.

## AWS ↔ Azure service mapping

| Role | AWS sibling | This port | Local stand-in |
| --- | --- | --- | --- |
| Source database | MySQL (RDS) | MySQL, **unchanged** | the sibling's DuckDB file, from its `build_source_db.py` |
| Compute | Glue **Python Shell** jobs | plain Python jobs, `python-jobs/` | the same scripts |
| Landing zone | S3 `s3://credit-mailer-lab/raw_landing_zone/credit_mailer_db/<table>/data.csv` | Blob Storage, ADLS Gen2 layout: container `credit-mailer-lab`, the same path | Azurite (Blob) |
| Watermark | DynamoDB `incremental_load_configurations`, key `table_name` | Table Storage `IncrementalLoadConfigurations`, PartitionKey `credit_mailer`, RowKey = table | Azurite (Table) |
| Warehouse | Redshift | Azure SQL Database | SQL Server 2022 CU27 container |
| Bulk load | `COPY ... IAM_ROLE ... EMPTYASNULL` | blob read + pyodbc `fast_executemany` into staging ([why](#the-bulk-load-which-shipped-and-why)) | the same code |
| Upsert | `MERGE` via staging table | `MERGE` via staging table, same generated text plus `;` | the same code |
| Orchestration | Step Functions, six-state chain | `orchestration/run-chain.py`, six-state chain | the same script |
| Credentials | Secrets Manager secrets | environment variables (Key Vault references in an app host) | `.env.example` |
| Watermark seeder | `dynamodb/write-to-dynamo.py` | `table-storage/write-to-table-storage.py` | the same script |
| DDL | `redshift/redshift-create-tables.sql` | `azure-sql/azure-sql-create-tables.sql` | applied by `local-development/apply_ddl.py` |

```
credit-mailer-watermark-azure-sql/
├── azure-sql/azure-sql-create-tables.sql   the T-SQL schema, GO-separated, 18 arms seeded
├── python-jobs/
│   ├── azure_common.py                     connections, landing/config addresses, the AWS loader
│   ├── mysql-extraction.py                 source → Blob, watermark in Table Storage
│   ├── azure-sql-raw-ingestion.py          Blob → staging → MERGE into raw_zone
│   ├── azure-sql-processed-layer.py        raw_zone → dim_client, fact_mailer
│   └── refinery-path3.py                   Steps 4–10 and the bandit, reading and writing Azure SQL
├── orchestration/run-chain.py              the six-state chain
├── table-storage/write-to-table-storage.py seeds and resets the two config entities
├── local-development/                      apply_ddl.py, create_container.py
├── tests/                                  42 tests: 18 ported, 24 for the load half
├── docker-compose.yml  .env.example  requirements.txt
```

## Reused, not copied

The four jobs import the AWS jobs' engine-neutral logic and add only the I/O. The AWS jobs are
loaded by path with `importlib`, because their file names carry hyphens, and their `glue-jobs/`
directory goes on `sys.path` first because three of them import `warehouse_common` at module
scope. Nothing in that tree imports boto3, duckdb or redshift_connector at module scope, so none
of those is loaded.

| From the AWS job | Used here as |
| --- | --- |
| `build_query`, `next_watermark`, `to_csv`, `extract` | the extraction job's SQL, watermark advance, CSV and fetch, unchanged |
| `TABLES`, `merge_sql` (raw ingestion) | the column lists and the raw MERGE, with a `;` appended |
| `FACT_MAILER_SELECT`, `ARM_BAND_JOIN`, `WAVE_WATERMARK`, `merge_sql`, `check_arm_grid`, `build_stage` | the processed layer, with its two staging statements rewritten |
| `DIM_CLIENT_SELECT` | taken by position, with the two BOOLEAN expressions replaced |
| the whole refinery: grid, binner, conjugate update, Thompson draws, SNIPS, bootstrap, Steps 4–10, `write_results` | unchanged |
| `CONFIGURATIONS` (the DynamoDB seeder) | the two declared config rows |
| `warehouse_common.run` | the statement runner |

The trade is that this folder depends on its sibling being at `../credit-mailer-watermark-glue-redshift`.
If it isn't there, every job fails at import and names the path it expected. A fork would be
self-contained, but it would drift silently from roughly 2,000 lines of reviewed logic, and a
missing sibling fails loudly instead.

## The watermark in Table Storage

The four runs are the sibling's, and they come out the same:

| run | source holds | `mail_offers` lands | watermark after | `raw_zone.mail_offers` | `fact_mailer` |
| --- | --- | --- | --- | --- | --- |
| 1 | wave 1 | **4,974** | `'1'` | 4,974 | 4,974 |
| 2 | waves 1–2 | **20,996** | `'2'` | 25,970 | 25,970 |
| 3 | waves 1–3 | **32,198** | `'3'` | 58,168 | 58,168 |
| 4 | waves 1–3 | **0** | `'3'` | 58,168, no value changed | 58,168, no value changed |

`client_attributes` lands and loads all 58,168 rows on every run and never gets a watermark.

Moving the config table to Table Storage changed five things, each of which could change which
rows move:

- **The name.** Table names are alphanumeric only, so `incremental_load_configurations` is refused
  (Azurite refuses it exactly as the service does). The port uses `IncrementalLoadConfigurations`.
- **The key is two-part.** PartitionKey is the source database (`credit_mailer`) and RowKey is the
  table. Because both entities share a partition, the seeder writes them in one **entity-group
  transaction**, so a seed or reset lands whole. DynamoDB's `batch_writer` is not atomic.
- **None is not stored.** Table Storage has no null type and drops a property whose value is
  None. So `client_attributes` has no `load_column` property at all, and a fresh `mail_offers` has
  no `last_extracted_value`. The extractor reads both with `.get()`, which turns the absence back
  into the None the AWS logic branches on.
- **So a reset must REPLACE.** A MERGE-mode upsert of `last_extracted_value=None` sends no
  property, so the old `'3'` survives and the next "run 1" is silently a run 4. The seeder writes
  with REPLACE. A test performs the MERGE reset, asserts the value survives, and then asserts that
  REPLACE clears it.
- **The watermark write is conditional.** The extractor captures the entity's ETag when it reads
  the config, and writes the new value `IfNotModified`. If a reset or a second run touches the
  entity mid-extract, the write fails instead of overwriting it. The landing blob is already in
  place by then, so the next run repeats the extract and the keyed MERGE absorbs the repeat. The
  AWS job's `update_item` is unconditional.

The order is unchanged: the watermark is **read** before the source is opened, and **written**
only after the landing blob's upload returns. The warehouse load is a separate state, exactly as
in the sibling.

## T-SQL changes versus the Redshift DDL

One schema file, applied as written. Every change below is one T-SQL forces, and each was
measured on the SQL Server 2022 container rather than assumed.

| Redshift / DuckDB | T-SQL | Why |
| --- | --- | --- |
| `create database db_credit_mailer;` | **removed** | Azure SQL provisions databases outside T-SQL. The local harness creates it in the container |
| one script, statements ending in `;` | batches separated by `GO` lines, which `apply_ddl.py` splits on | `CREATE SCHEMA` must open its batch; a `CREATE TABLE` after it in the same batch is error 156. `GO` is a client-side separator that pyodbc would pass through as a syntax error |
| `SMALLINT` on the raw 0/1 flags | **`SMALLINT`, kept** — not `BIT` | 14 flags are NULL on all 4,974 wave-1 rows, and that NULL is data. BIT would hold the NULL, but `CAST(2 AS BIT)` is 1, so BIT would quietly accept a value that isn't a flag |
| `BOOLEAN` on `dim_client.is_female`, `is_more_educated` | nullable **`BIT`** | T-SQL has no BOOLEAN. These two are computed in the MERGE, never loaded from a CSV, which is why BIT is safe here and not on the raw flags |
| `(ca.female = 1)` in the SELECT list | `CAST(CASE WHEN ca.female = 1 THEN 1 WHEN ca.female <> 1 THEN 0 END AS BIT)` | A comparison is not a value in T-SQL (error 102). Two WHENs and **no ELSE** keep the three-valued meaning: NULL stays NULL instead of becoming 0 |
| `CREATE TABLE raw_zone.tmp_x AS SELECT * FROM raw_zone.x` | `SELECT * INTO raw_zone.tmp_x FROM raw_zone.x WHERE 1 = 0` | T-SQL's CTAS. It carries names, types, order and nullability, so `client_id` and `wave` stay NOT NULL in staging; Redshift's CTAS carries names, types and order but not NOT NULL. Neither carries the PRIMARY KEY |
| `CREATE TEMP TABLE stage_x AS SELECT ...` | `SELECT ... INTO #stage_x FROM ...` | a `#` table is session-private and dropped when the connection closes |
| raw-ingestion `MERGE` with no terminator | the same text plus `;` | a MERGE must end in `;` (error 10713). The processed layer's MERGE already did |
| `ORDER BY n DESC LIMIT 10` | `SELECT TOP 10 ... ORDER BY n DESC` | no LIMIT in T-SQL |
| `BEGIN TRANSACTION;` / `COMMIT;` / `ROLLBACK;` sent as text | none: pyodbc's implicit transaction, `conn.commit()` / `conn.rollback()` | T-SQL nests them. A text BEGIN inside the driver's transaction sets `@@TRANCOUNT` to 2, `conn.commit()` then keeps nothing, and closing the connection rolls the load back |
| `COPY ... EMPTYASNULL` | empty field → `None` in Python before binding | `CAST('' AS SMALLINT)` is **0** in T-SQL, and would turn wave 1's 74,173 NULL cells into zeros: 69,636 in the 14 treatment flags and 4,537 in `badacct_last` |
| `COPY ... IGNOREHEADER 1` (positional, unchecked) | the header is compared with the column list before any row is sent | a client-side load can check what a COPY can only skip |
| multi-row `INSERT ... VALUES` | the same, limited to 1,000 rows (error 10738) | the refinery writes at most 54 and asserts the limit |

Not in the table, because it is not a T-SQL change: no SQL in either pipeline uses `AVG` over an
integer column. Take-up is `SUM(took_up)` over `COUNT(*)`, divided in Python. T-SQL returns an
integer from `AVG` of an integer column (`AVG` of 1 and 2 is 1, measured on the container), and so
does Redshift. DuckDB returns a double, which is why the sibling's local run could never have shown
a take-up rate of 0.

## The bulk load: which shipped and why

The faithful port of `COPY FROM 's3://...'` is a server-side load, where SQL Server reads the blob
itself through an external data source. That was tried first on SQL Server 2022 CU27 against
Azurite, with a database-scoped SAS credential:

| external data source `LOCATION` | result |
| --- | --- |
| `http://azurite:10000/devstoreaccount1/credit-mailer-lab` (path-style, Azurite) | created; `BULK INSERT` and `OPENROWSET(BULK ...)` fail with **error 12704**, "Bad or inaccessible location" |
| the same over `https://` | error 12704 |
| `https://devstoreaccount1.blob.core.windows.net/...` (virtual-host) | created, then resolved against the public Azure endpoint instead of the emulator (OS error 12175) |
| `abs://...` (data virtualization) | error 46530: the image ships without PolyBase |

**What shipped is the client-side load.** The raw-ingestion job reads the blob, turns empty fields
into None, checks the header, and inserts into the staging table with pyodbc `fast_executemany`,
which sends parameter arrays rather than one round trip per row. Each field is bound as the type
its staging column declares (int, Decimal or str, read from `information_schema`), not as text.
Under `fast_executemany`, pyodbc sizes a text parameter from the column's precision, which leaves no
room for a sign or a decimal point. `100.00` was refused for a DECIMAL(5,2) that holds it, and
`-32768` for a SMALLINT. Typed, both load, and a Decimal with more places than the column allows is
refused rather than rounded. The `client_attributes` state
(58,168 rows read, bound, merged and committed) takes under two seconds against the container.
Azure SQL Database supports `BULK INSERT ... WITH (DATA_SOURCE = ...)` over a real
`*.blob.core.windows.net` container. `bulk_load()` is the function to swap for that, and the MERGE
and everything after it would stay as they are.

## Three engine differences in the port's favour

Each of these was a caveat in the sibling. Here each is a test.

1. **SQL Server enforces `VARCHAR(16)`; DuckDB accepts it without checking.** The width is headroom
   over measured maxima of 8 (`coloured`) and 6 (`MEDIUM`). A 16-character `race` loads, and a
   17-character one fails the whole load and leaves the table as it was. Under
   `fast_executemany` the refusal comes from pyodbc, which sizes its buffer from the width the
   server reports. A plain INSERT gets error 2628 from the server itself. Both are asserted in
   `test_varchar16_headroom_is_enforced`.
2. **SQL Server enforces `PRIMARY KEY`; Redshift doesn't.** A landing file holding one
   `(client_id, wave)` twice passes through the keyless staging table, and the MERGE's second
   insert is error 2627. Nothing lands (`test_a_duplicated_key_fails_the_load_where_redshift_would_keep_both`).
   The MERGE is still what makes a re-run idempotent; the key is a second line behind it.
3. **SQL Server's `TRUNCATE` is transactional; Redshift's commits.** On Redshift the raw load is
   three commits, split at the two truncates. Here it is one unit: truncate, load, MERGE,
   truncate, one `conn.commit()`. `test_truncate_rolls_back_with_a_failed_load` commits a
   sentinel row into staging, fails a load at the MERGE, and finds the sentinel still there.

The DDL is transactional too. `apply_ddl.py` runs every batch on one connection with one commit,
and a failing last batch leaves the database with no tables rather than half a schema.

## What the local run covers

**42 tests. 30 of them run against the real emulators through the real clients**: pyodbc to SQL
Server, azure-storage-blob and azure-data-tables to Azurite. No mocks, and no SQLite or DuckDB
standing in for T-SQL. **The other 12 need neither container.** They are the 9 bandit tests, which
pin the refinery's arithmetic through the Azure job module, and three that check code rather than
services: the extractor's `--self-check`, every warehouse job's `--self-check`, and the chain's
state order against the sibling's Step Functions definition.

- `tests/test_watermark.py` (9: 8 against Azurite, plus the self-check): the sibling's watermark
  suite, ported test for test. It covers the
  four runs at the landing zone (4,974 / 20,996 / 32,198 / 0 rows; watermark `'1'`, `'2'`, `'3'`,
  `'3'` as text), the header-only blob of run 4, the full load that ignores a set watermark,
  `client_attributes` with its absent watermark, and the `exit 1` when the chain and the config
  disagree.
- `tests/test_bandit.py` (9, no emulator): the sibling's bandit suite with every test body
  byte-identical, asserting against the Azure refinery module. It pins that the job writing to Azure SQL runs the
  declared grid and the shared arithmetic.
- `tests/test_warehouse.py` (24: 22 against the emulators, plus the chain-order and self-check
  tests), the load half the sibling's suite never reached:
  - **The four runs through the chain.** Run 4 lands 0 rows, and a checksum over every column of
    `raw_zone.mail_offers` and `fact_mailer` shows it changed no value.
  - **The data rules re-earned in T-SQL.** Wave 1's 14 wholly-NULL flags (69,636 cells, of the
    wave's 74,173 NULL cells) reach the fact as NULL, and `bad_account` is non-null on exactly the
    4,381 take-ups. The BIT flags agree with the raw flags on all 58,168 clients. The published
    data has no NULL `female` or `edhi`, so the NULL branch of the CASE is tested on its own
    clients: NULL comes out NULL, 1 comes out 1 and 2 comes out 0. The 18 posterior means match
    the sibling's published table to five places.
  - **Parity with the AWS pipeline.** The AWS jobs, unmodified, run their own four-run sequence
    on DuckDB, and all seven star and bandit tables are compared with SQL Server's row for row:
    232,690 star rows plus the 60 bandit rows, including the Monte-Carlo columns to the eighth
    decimal.
  - **The three engine differences above**, an empty field landing as NULL, values at the full
    width of their columns (an offer rate of 100.00, a SMALLINT of -32,768), an idempotent reload,
    and a reordered extract refused before the warehouse is touched.
  - **The Table Storage traps**: the MERGE-mode reset and the stale ETag.
  - **The chain**: it matches the Step Functions definition state for state, a failing state
    stops it, and every job's `--self-check` passes.

**Engine-difference notes.**
- Azurite serves the Blob and Table APIs but has no DFS (ADLS Gen2) endpoint. So the jobs write
  the Gen2 path layout through the Blob API, which a hierarchical-namespace account accepts on the
  same paths.
- SQL Server 2022 and Azure SQL Database share the engine and the T-SQL this pipeline uses: MERGE,
  `SELECT ... INTO`, `#` tables, transactional TRUNCATE and DDL, and INFORMATION_SCHEMA.
  Provisioning, service tier, Entra ID authentication and firewall rules belong to the service and
  the connection string, not to these files.
- The MySQL source is the sibling's DuckDB stand-in, exactly as in the sibling. The pymysql branch
  reads `MYSQL_HOST`, `MYSQL_USER` and `MYSQL_PASSWORD` from the environment.
- The four-run demo's bootstrap interval bounds agree with the sibling's AWS jobs run in the same
  environment, digit for digit. They differ from the sibling README's printed bounds in the last
  digit on four of six cells, because those columns are Monte-Carlo and depend on numpy's
  generator stream. The posterior means, SNIPS values and lifts match the printed table exactly.

## Running it

Ubuntu 24.04 shown. Two prerequisites come first:
- **Docker Engine with the Compose plugin**, installed as in
  [Docker's Ubuntu guide](https://docs.docker.com/engine/install/ubuntu/). `docker compose` must
  be v2, since `--wait` is what blocks on the healthchecks.
- **`python3-venv`**, installed below, because Ubuntu's Python ships without `venv`.

The ODBC driver comes from packages.microsoft.com, and other distributions are listed there.

```bash
# 1. Microsoft ODBC Driver 18, the unixODBC headers pyodbc builds against, and venv
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
  | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/prod.list \
  | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev python3-venv

# 2. Python, in a virtualenv
cd credit-mailer-watermark-azure-sql
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. The two emulators. The first pull is about 3 GB, mostly SQL Server
docker compose up -d --wait

# 4. The suite
python -m pytest tests -q           # 42 passed

# 5. And the sibling's, which this port leaves untouched
(cd ../credit-mailer-watermark-glue-redshift && pip install -r requirements.txt \
   && python -m pytest tests -q)    # 18 passed
```

The tests set their own connection strings and create uniquely named databases, tables and
containers, which they remove at the end. To run the four-run demo by hand against the default
names, export the two connection strings and run the chain once per wave:

```bash
set -a && . ./.env.example && set +a
python local-development/apply_ddl.py --drop
python local-development/create_container.py
python table-storage/write-to-table-storage.py --create-table --reset
for wave in 1 2 3 3; do
  python ../credit-mailer-watermark-glue-redshift/local-development/build_source_db.py \
      --source ../credit-mailer-watermark-glue-redshift/data/adcontentworth_qje.tab.gz \
      --db _localrun/source.duckdb --through-wave "$wave"
  python orchestration/run-chain.py --source-db _localrun/source.duckdb
done
docker compose down                 # discards everything: Azurite holds its data in memory,
                                    # SQL Server in the container's own filesystem
```

Every job also has `--self-check`, which pins its pure decisions with no network, no service and no
files, and runs the AWS job's own self-check for the logic it reuses.

## Deploying it

1. Provision an Azure SQL database named `db_credit_mailer`, then run
   `azure-sql/azure-sql-create-tables.sql` with `sqlcmd -i` or any client that honours `GO`.
2. Create a storage account (hierarchical namespace optional; the jobs use the Blob API) with a
   container `credit-mailer-lab` and a table `IncrementalLoadConfigurations`. Then seed the table
   with `python table-storage/write-to-table-storage.py`.
3. Load the MySQL source as the sibling does, with its `mysql/mysql-queries.sql`.
4. Host the jobs anywhere that runs Python with ODBC Driver 18: a Container Apps job, Azure Batch
   or a VM. Ship this folder beside `credit-mailer-watermark-glue-redshift/`, keeping the sibling
   layout, with at least that project's `glue-jobs/` and `dynamodb/write-to-dynamo.py`. Set `AZURE_SQL_CONNECTION_STRING` (ODBC Driver 18 supports
   `Authentication=ActiveDirectoryMsi` for a passwordless managed identity),
   `AZURE_STORAGE_CONNECTION_STRING` and the three `MYSQL_*` variables, as Key Vault references
   rather than literals. Install `pymysql` for the source.
5. Run `orchestration/run-chain.py` on a schedule, or reproduce its six states in any sequential
   orchestrator. They must stay sequential, because each table lands on one fixed blob name.

Every Azure value in these files is a default to be set to your own: the database name, the
container, the table name and the connection strings.
