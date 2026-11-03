# Finance Projects

A curated collection of quantitative finance analyses, featuring exploratory data analysis, backtesting of trading strategies, and predictive modeling — alongside AWS data-engineering pipelines over banking, crypto-market, consumer-lending, small-business loan, company-accounts and gift-voucher data.

## Current Projects

* **Bulls, Banks & Bollinger**: Quantitative analysis of South African bank stocks, including EDA, risk metrics (VaR/CVaR), and backtesting of Moving Average & Bollinger Bands strategies with transaction costs.
* **`bank-marketing-cleaning-emr-redshift`**: PySpark cleaning and feature-preparation pipeline for 41,188 Portuguese bank term-deposit campaign records, submitted to EMR Serverless by Airflow, with KPIs upserted into Redshift.
* **`crypto-ticks-refinery-glue-dynamo`**: A ten-step statistical refinery over 340,971,834 Binance spot trades, aggregated into 5-second OHLCV bars and forked into continuous, categorical and Thompson Sampling bandit paths, landing in DynamoDB.
* **`credit-mailer-watermark-glue-redshift`**: Incremental ETL with a DynamoDB watermark over 58,168 randomised loan offers from a South African lender, orchestrated by Step Functions into a Redshift star, with an off-policy bandit evaluation.
* **`credit-mailer-watermark-azure-sql`**: The credit-mailer pipeline ported to Azure — a Blob Storage landing zone, a Table Storage watermark and an Azure SQL Database star in T-SQL — tested offline against Azurite and SQL Server 2022, and matched row for row against the AWS version's own local run.
* **`sba-credit-risk-ecs-dynamo`**: PD, gross charge-off severity, expected loss, IFRS 9 stage and IRB capital for the 281,077 open loans among 1,639,728 SBA 7(a) small-business loans, with FDIC bank-failure PD and a dollar EL backtest beside it, run as two ECS Fargate containers gated by Step Functions into DynamoDB.
* **`latvia-company-credit-glue-delta`**: A 12-month PD for 115,088 Latvian companies from the annual accounts they file and the insolvency register, fitted out of time on 1,607,967 scored company-years since 2009, with the Register's files replayed as 73 quarterly batches into Delta Lake by Glue 5.0 MERGE jobs and the training rows read back from the tables by time travel.
* **`voucher-float-kinesis-redshift`**: A gift-voucher service's issue, redemption and expiry events carried from a transactional outbox through Kinesis, a Firehose transform Lambda and Redshift to the daily outstanding liability, IFRS 15 breakage by issue month and a redemption life table, on two simulated years whose true breakage rate the young-cohort estimate recovers without bias.

*(More projects coming soon.)*

---

## The ten-step refinery

Three of the pipelines — crypto-ticks and both builds of credit-mailer — share one statistical
refinery. It chooses how a dataset is cleaned from the kind of target it has rather than from
habit, and it runs once, top to bottom: no step goes back to rework an earlier one.

**Steps 1–3 are a shared entryway** and run the same way whichever path follows: deduplication
on a key, normalised column names and types, and missing values kept visible rather than filled,
because where a value is missing can itself predict the outcome.

**The fork comes after Step 3, before anything is filled, on purpose.** Each path treats a gap
differently, so nothing can be imputed until the path is known. The refinery checks what kind of
quantity the target `y` is and sends the data down one of three isolated paths. Steps 4–10 keep
their numbers on every path, but what each step does, and whether it may run at all, depends on
the path:

| Step | Path 1 — continuous `y` | Path 2 — categorical `y` | Path 3 — bandit |
| --- | --- | --- | --- |
| 4 Imputation | median by default; above 5% missing, no medians and complete rows only | median; a missing category becomes its own state | none: a gap stays an unobserved state |
| 5 Diagnostics | univariate density | class balance, interaction correlation | stream diagnostics |
| 6 Topology | global cross-correlation | cross-correlation + sin/cos time coordinates | sin/cos time coordinates, where time has a period |
| 7 Feature engineering | deterministic cross-products | cyclical coordinates; interaction cross-products | interaction frames |
| 8 Pruning | variance threshold on numeric columns | one-hot encode *before* the variance threshold | **banned** |
| 9 Regularisation | cross-validated Elastic Net | cross-validated Elastic Net | **banned** |
| 10 Scaling | linear scalers only, so coefficients stay in the target's units | search across standard, power and quantile transforms | **banned** |

Path 3's bans are the design, not gaps in it: each one protects the engine downstream. On
crypto-ticks, one-hot encoding and variance pruning would dissolve the raw tokens its association
mining counts, and Elastic Net zeroes rare columns first, though a rare pairing can carry the
highest lift. On credit-mailer the same two steps would delete or shrink the thinnest price arms,
the ones Thompson sampling most needs to explore. On both, scaling turns a Beta posterior's counts
into numbers that are no longer valid shape parameters. Every banned step is reported with a
verdict rather than skipped silently: the crypto-ticks notebook runs each one on a copy to measure
what it would break, and the credit-mailer job prices the pruning and scaling bans on its own
counts.

| Project | Paths it runs |
| --- | --- |
| `crypto-ticks-refinery-glue-dynamo` | all three, from one table of 5-second bars |
| `credit-mailer-watermark-glue-redshift` | Path 3 only: take-up under a randomised price is its one usable target |
| `credit-mailer-watermark-azure-sql` | Path 3, reusing the AWS version's arithmetic |

[`crypto-ticks-refinery-glue-dynamo/README.md`](crypto-ticks-refinery-glue-dynamo/README.md)
walks every step on every path.

---

## AWS data-engineering pipelines

Six of the projects here are AWS data-engineering pipelines. Each is self-contained: the
exploratory notebook it grew out of, the production jobs, the orchestration, and whatever the
destination store needs — Redshift DDL, a DynamoDB key schema, or both.

### Layout

The six pipelines share a shape, so a reader who has seen one can navigate the rest:

```
<project>/
├── airflow-dag/        the DAG that orchestrates the pipeline
├── <engine>-jobs/      the compute jobs: spark-jobs/ for EMR Serverless, glue-jobs/ for Glue
├── data/               a committed sample so the project runs after clone
├── the prototype notebook, with its executed outputs
└── README.md           architecture, deployment steps, and what the job produces
```

Beyond that each project carries only what its stack needs, and the differences are the point
rather than drift. `bank-marketing-cleaning-emr-redshift` adds `redshift/` for the warehouse DDL,
`local-development/` for its notebook and a `requirements.txt`.
`crypto-ticks-refinery-glue-dynamo` adds `tests/` and a `local-docker-development.sh` that runs
the whole chain inside AWS's own Glue 4.0 image, and keeps its notebook at the project root
because every path in it is written relative to there.
`credit-mailer-watermark-glue-redshift` has **no `airflow-dag/` at all** — it is orchestrated by
Step Functions, so `step-functions/` takes that slot — and adds `mysql/` for the source database
it extracts from and `dynamodb/` for the one control-plane table that holds its load state.
`sba-credit-risk-ecs-dynamo` is orchestrated by Step Functions too, and its compute is two
container images rather than jobs, so `docker-data-validity/` and `docker-credit-model/` take
that slot; the code both images run lives in one `credit_risk/` package, with `tests/` and
`local-development/` beside it.
`latvia-company-credit-glue-delta` has **no `airflow-dag/` either**: its two Glue jobs run in a
fixed order, the three MERGEs and then the model, and `local-development/run_chain.py` runs them
that way locally. `glue-jobs/` holds the two jobs and the `lakehouse.py` they share; the rules,
the replay, the label and the scorecard live in one `company_credit/` package that both jobs, the
tests and the notebook import, with `tests/` and `local-development/` beside it, and a
`local-docker-development.sh` runs the chain inside AWS's own Glue 5.0 image.
`voucher-float-kinesis-redshift` is a stream, not a batch, so it has no `airflow-dag/` either —
the relay runs every minute and the expiry sweep nightly on the service's own instance, and
Firehose buffers for itself — and `kinesis-producer/` and `firehose-lambda/` take the jobs slot,
beside the `voucher_service/` package that is its source system and `warehouse/` and `redshift/`
for the SQL.

The notebook is kept deliberately. It is where the logic was worked out, and it carries the
executed outputs, so the production job can be diffed against something that is known to be
correct rather than against a description of it.

### `bank-marketing-cleaning-emr-redshift`

Cleaning and feature-preparation pipeline for the UCI Bank Marketing dataset — 41,188 rows of
Portuguese bank term-deposit campaign records, May 2008 to November 2010.

A PySpark job cleans the raw CSV and engineers features, writing a curated model-ready Parquet
frame to S3 and two flat KPI CSVs alongside it. Airflow validates the raw file, submits the job
to EMR Serverless, upserts the KPIs into Redshift through a `tmp_` staging table, and archives
the processed object.

```
S3 raw/  ->  EMR Serverless (PySpark)  ->  S3 curated/  (Parquet, keeps the ML vectors)
                                       ->  S3 kpis/     (CSV)  ->  Redshift reporting_schema
```

| Output | Grain | Contents |
| --- | --- | --- |
| curated Parquet | one row per contact | 26 columns, 8 of them `VectorUDT` feature vectors |
| `segment_level_kpis` | date × sector × education | contacts, subscribed, subscribe rate, avg age, avg campaign |
| `monthly_kpis` | date | contacts, unique jobs, subscribed, subscribe rate, avg euribor, top sector, % cellular |

The interesting problems in it were Spark-specific rather than pipeline-specific:

- **Row order has to be captured before it is needed.** The source file has `month` but no year.
  The year is recoverable by counting month rollovers in file order — but a Spark DataFrame has
  no row order, and by the time the reconstruction runs the frame has been through a shuffle.
  `monotonically_increasing_id()` called at that point numbers rows in *post-shuffle* order and
  produces years from 2013 to 2027. The id has to be stamped immediately after the read.
- **A surrogate key silently disables `dropDuplicates()`.** Once `row_id` exists, a bare
  `dropDuplicates()` compares it too, finds every row unique, and removes nothing. It needs an
  explicit `subset` of the real columns.
- **Plan depth fails before data volume does.** Fifty chained transformations plus six fitted ML
  models grew the logical plan past what Catalyst would re-analyse, and the job died with a
  several-hundred-line plan dump that pointed at the wrong stage. Two `localCheckpoint(eager=True)`
  calls fix it; the pipeline itself runs in well under a minute.

Full detail, deployment steps and the verified run figures are in the project README.

### `crypto-ticks-refinery-glue-dynamo`

A ten-step statistical refinery over Binance spot tick data — 340,971,834 trades across BTCUSDT,
ETHUSDT and SOLUSDT for January 2025, aggregated into 5-second OHLCV bars.

The point of the project is the refinery's structure rather than the bars: it is the one project
that runs all three paths of [the ten-step refinery](#the-ten-step-refinery), each from the same
table of bars.

```
S3 raw/  ->  Glue: glue-ingest-bars.py  ->  S3 curated/ bars  (1,607,040 x 18)
             Steps 1-3, path-blind                  |
                           ============== THE FORK ==============
                                     |              |              |
                              path1 continuous  path2 categorical  path3 bandit
                              next-bar return   up/flat/down       Thompson Sampling
                                     |              |              |
                                     +--------------+--------------+
                                                    |
                                  Glue Python shell: glue-dynamo.py  ->  DynamoDB
```

| Output | Grain | Contents |
| --- | --- | --- |
| `bars/` | symbol × 5s slot | 18 columns, decimal money, a declared calendar rather than an observed one |
| `path{1,2,3}/` | per path | the scaled feature matrix, Step 6's dependency topology, and the model or posterior ledger |
| DynamoDB | `<run-id>#<path>` / grain | 96 items — one per surviving feature, (feature, class) cell and bandit arm |

Verified run: 340,971,834 ticks to 1,607,040 bars in 6 min 56 s. The interesting problems were
again about arithmetic and determinism rather than plumbing:

- **`time` is epoch microseconds, not milliseconds.** Sixteen digits. Reading it as milliseconds
  returns a perfectly valid timestamp in the year 56971 and raises nothing. Binance moved spot
  trade archives to microsecond resolution for 2025 data, and the download page does not say so.
- **Money is `DecimalType`, never `Double`, because a Double pipeline is not idempotent.**
  Summing 2M real `quote_qty` values under 3, 11 and 29 shuffle partitions gives three different
  doubles and one identical decimal — float addition is not associative and Spark promises no
  stable merge order, so the same job over byte-identical input would emit different bars.
- **`open` and `close` come from `min_by`/`max_by` on `trade_id`**, not `first()`/`last()` and
  not an ordering on the timestamp. `first()`/`last()` over a `groupBy` return whatever reached
  the partition first, and the timestamp is only *non-decreasing*: 75% of ticks share a
  microsecond with their predecessor, up to 1,110 in a single microsecond, and 24,851 of those
  tie groups have a non-constant price. An `event_time` ordering leaves open and close genuinely
  undefined on a large fraction of bars.

Full detail, the per-path step tables and every verified figure are in the project README.

### `credit-mailer-watermark-glue-redshift`

Incremental ETL over a South African consumer-lender field experiment — 58,168 direct mail loan
offers sent in three waves in 2003, each at a randomly assigned interest rate, from
Bertrand et al.'s QJE 2010 replication deposit (CC0).

The lesson is the watermark, and what makes it a lesson is the asymmetry: the load state lives in
DynamoDB, one row per table, and one of the two source tables **cannot** be loaded incrementally.
A pipeline where every table has a timestamp would not need this — a hard-coded predicate would
do. `client_attributes` is a CRM snapshot with no event column anywhere in it, so its
`load_column` is `None` and it ships all 58,168 rows on every run.

```
MySQL  ->  Glue Python Shell  ->  S3 landing  ->  Redshift raw_zone  ->  processed_zone star
           (watermark in DynamoDB)                                              |
                                                                        Path 3: the bandit
```

Everything is a Glue **Python Shell** job — the standard library, boto3 and numpy, no pandas and
no Spark. The largest extract is 58,168 rows of 7 columns, and a cluster would spend longer
starting than the whole chain takes.

| Run | Source holds | `mail_offers` extracts | Watermark after |
| --- | --- | --- | --- |
| 1 | wave 1 | 4,974 | `1` |
| 2 | waves 1-2 | 20,996 | `2` |
| 3 | waves 1-3 | 32,198 | `3` |
| 4 | waves 1-3 | **0** | `3`, unchanged |

Run 4 is the point. A job that ignored the stored value would extract all 58,168 rows and look
healthy doing it — same exit code, same object, same duration. The row count is the only thing
that separates them.

The analytical half is Path 3 of [the ten-step refinery](#the-ten-step-refinery), and only
Path 3: no candidate target for the other two paths survives. `amountbrw_unc` is zero on 92.47%
of rows, and `badacct_last` exists only for the 4,381 people who got a loan. What the file does
have is an action that was genuinely randomised, which makes the off-policy evaluation causal
rather than decorative.

- **The file reproduces the paper exactly.** Waves 2 and 3 are 53,194 rows, which is the paper's
  published N; mean rate 793 basis points against its "793"; 87.2% of applications became loans
  against its "87%"; and the maximum rate offered in each risk band is the lender's own standard
  schedule to the cent. That is the provenance check, and it is cheaper than trusting a filename.
- **One of six off-policy comparisons clears zero**, and it is the band where the arms separate:
  HIGH risk on wave 3, +11.78%, CI95 [+0.0025, +0.0091], with best and worst arms 6.7 posterior
  standard deviations apart. Take-up falls from 5.76% on the cheapest arm to 4.08% on the
  dearest, though not monotonically — arms 3 and 4 are inverted by well under one posterior sd.
- **One came out negative.** LOW risk on wave 3, -9.62% — the policy trained on the earlier waves
  did worse than the lender's own randomisation. LOW's top three arms are within one standard
  deviation of each other and its greedy arm never settles, moving 1 -> 2 -> 0 across the rounds.
  A confident policy over arms that do not separate is an overconfident one.

Full detail, the verified figures and the three banned steps are in the project README.

### `sba-credit-risk-ecs-dynamo`

Credit risk on company lending, end to end: PD, a loss measure, exposure and expected loss on
1,639,728 SBA 7(a) small-business loans approved from FY2000 to 2026-09-30, with bank and
counterparty risk from every FDIC failure since 1984 beside it. The two chains are never
multiplied together. An SBA borrower charging off and its lender failing are different events on
different populations, and a product of the two would describe neither.

```
S3 new/  ->  ECS: data-validity  ->  Choice: gate passed?  ->  ECS: credit-model  ->  DynamoDB
             exact dtypes and           no: Fail state,         SBA and FDIC chains    153 items
             file counts, answers       its cause names         results/, then the
             through a task token       the short entity        batch is archived
```

| Chain | Measure | Full data |
| --- | --- | --- |
| SBA scorecard | holdout AUC, FY2000-09 / the next vintage, FY2010 | 0.623 / 0.601 |
| SBA open book | 281,077 loans, $165.06bn of exposure | 12-month EL $3.27bn, lifetime EL $15.61bn |
| IFRS 9 | collective stage 2 | FY2007 only: 470 loans, $311m |
| IRB capital | RWA on the whole loan, SBA's guarantee not applied | $316.1bn, 191% of exposure |
| FDIC failure PD | AUC, validate 2008-12 / test 2013-25 | 0.961 / 0.963 |
| FDIC counterparty | the 2025 year-end filings | 4,393 banks, 4.7 expected failures, $1.44bn EL |

The interesting problems were about what the file can and cannot identify:

- **The file records charge-off, not default, and charged-off amount, not LGD.** Charge-off
  follows the lender's liquidation, months or years after the 90 days past due Basel calls
  default, and the file has no balance at default and no recovery beside the amount. So the
  chain models exactly those and names them that way: a 12-month *charge-off* PD fills the Basel
  PD slot, gross charge-off severity the LGD slot, and the original amount, an upper bound, the
  exposure.
- **Two origination fields are written after the outcome.** `TermInMonths` is rewritten at
  charge-off: 83.4% of paid-in-full company loans carry a whole-year term against 8.9% of
  charged-off ones, and given to the scorecard it lifts the holdout AUC from 0.623 to 0.915, every
  point of it the outcome. `CollateralInd` is attached on the way to charge-off for the FY2000-04
  approvals: 93.0% of the flagged FY2000-03 loans charged off, against 11.3% of the rest. Both are
  out of every model.
- **Payoff dates before August 2005 are a conversion stamp.** No paid-in-full date is earlier than
  2005-05-31, and 34,187 company payoffs carry that exact date against about 2,000 in an ordinary
  month. Left in, they would keep repaid loans at risk for years and thin every early hazard, so
  the life table is left-truncated: a loan still open on 2005-08-01 enters at its age that day,
  and every exit before it is dropped, charge-offs with the payoffs.

Full detail, the label rules for both chains and every verified figure are in the project README.

### `latvia-company-credit-glue-delta`

A 12-month probability of default for Latvian companies, from the annual accounts they file with
the Register of Enterprises and labelled from its insolvency register: 2,354,336 company-years since
2009, and 115,088 companies scored on 1 October 2026. The lab it imitates MERGEs batches into Delta
tables; here the batches are today's files replayed as the quarterly extracts they would have been,
so a company comes back when it is terminated and a proceeding when it ends, the MERGE restates
rather than appends, and the model reads its training rows from the tables as they stood on each
observation date.

```
S3 raw_zone/  ->  Glue 5.0: glue-delta-merge.py   ->  Delta: companies, proceedings,
73 dated          explicit schema, one winner per      statements (by fiscal year)
batches           key, conditional MERGE, batch date         |  VERSION AS OF each 30 September
                  in userMetadata, archive last              v
                                                     Glue 5.0: glue-credit-model.py
                                                     WoE scorecard, out of time  ->  results/
                                                     today's book  ->  Delta: scored_companies
```

| Measure | Full data |
| --- | --- |
| MERGE restatements: companies / proceedings / statements | 151,477 of 498,530 / 15,755 of 33,303 / 547 of 1,703,441 |
| PD AUC: train 2009-17 / validate 2018-20 / test 2021-25 | 0.801 / 0.803 / 0.818, against 0.647 / 0.651 / 0.654 for the equity ratio |
| Calibration on the test years, observed 0.26% | mean PD 0.51% through the cycle, 0.31% at the 2018-20 level |
| Today's book | 115,088 of 141,319 live companies scored, 576 / 350 expected defaults, €53.8bn of liabilities |

The interesting problems were about what the files know, and when:

- **`created_at` is a load stamp until 18 January 2021.** 1,152,314 statements, 99.87% of them for
  FY2019 or earlier, carry stamps from the Register's bulk load of August 2020 to January 2021, at a
  median of 17,534 a day on its busy days. Read as filing times they would leave 2009-19 with no
  accounts at all, so a statement stamped before then is dated nine months after its period end,
  when 91.6-95.1% of FY2020-24 accounts had really been filed. The test years have real stamps, so
  they measure the rule's cost: re-dated the bulk way, their AUC moves from 0.8176 to 0.8182, and
  from 0.8080 to 0.8151 among companies scored on last year's accounts alone.
- **Late filing is a signal the bulk load hides.** Whether a company's latest accounts are last
  year's or the year before's has an IV of 0.016 on the training years and 0.096 on the test years,
  where filing dates are real. The IV screen would drop it, so it is kept by design and must still
  pass the sign and p-value checks.
- **Time travel by batch date, not by wall clock.** A replay commits a table's 73 batches within
  minutes — the proceedings table's span 244 seconds — so `timestampAsOf` cannot find the version
  of a business date. Each commit carries its batch date in `userMetadata`, and the as-of reads
  look it up there. And a bare MERGE inserts a key twice when a batch repeats it and the table
  does not hold it yet, so each batch is cut to one row per key first.

Full detail, the label rule and every figure are in the project README.

### `voucher-float-kinesis-redshift`

A gift-voucher service's lifecycle events — issued, redeemed, expired — carried from the
service's own database through Kinesis, a Firehose transform Lambda and Redshift to the three
numbers finance needs about voucher float: the outstanding liability every day, breakage by issue
month, and the redemption curve. The data is two simulated years, 49,209 vouchers and 97,588
events, because only a simulation fixes the true breakage rate, 23.97%, before the first voucher
is issued, and so lets the estimate be checked against something other than another estimate.

```
Flask + SQLite  ->  outbox   ->  relay        ->  Kinesis  ->  Firehose + transform  ->  Redshift
REST writes,        same txn     put_records,                  Lambda: JSON lines,       six views
GraphQL reads       as the       keyed by                      a bad record fails
                    change       voucher                       on its own
```

| Output | Grain | Contents |
| --- | --- | --- |
| `daily_liability` | UTC day | outstanding cents and vouchers, dated from the events, never from arrival |
| `redemption_life_table` | days since issue | redeemed and expired as competing exits, open vouchers censored |
| `breakage_by_issue_month` | issue month | realised once the cohort has closed, projected while it is open |
| `reconciliation` | one row | the warehouse's liability against the service's own total, to the cent |

The interesting problems were about dates and estimates rather than throughput:

- **Counting what has happened books almost no breakage on a young cohort.** For October 2026,
  days old, expired over resolved gives 2.4%; a life table over every voucher's age projects
  22.5%. Across 22 month-end backtests the projection shows no bias against the true rate (mean
  error -0.04 points), and a single cohort is typically within about a point of it (mean absolute
  error 0.81 points, 16 of 22 within one), 2.2 at worst.
- **A lapse is dated at expiry, not at the sweep that records it.** The sweep was down for a week
  in March 2026, and its catch-up put a week of expiries on the stream behind redemptions dated
  later. The warehouse builds everything from `occurred_at`, so its daily liability is the one an
  on-time sweep would have given.
- **At least once, then dedupe, then reconcile.** Every state change writes an outbox row in the
  same transaction, the relay marks a row only once Kinesis has accepted it, and the warehouse
  drops copies on `event_id`. Its liability then equals the service's own: 171,795.16 on 830
  vouchers on both sides.

Full detail, the backtest table and the deployment steps are in the project README.

### Running the pipelines

Each project's README carries its own prerequisites. For bank-marketing and crypto-ticks you need
Python 3.11, `pyspark==3.5.5`, and a Java 11 or 17 JDK on `JAVA_HOME` — Spark 3.5 does not support
Java 21+. `latvia-company-credit-glue-delta` needs Python 3.11, a Java 17 JDK, `pyspark==3.5.4` and
`delta-spark==3.3.0`, the versions of the Glue 5.0 runtime it deploys to, with pandas 2.2.2, numpy
2.2.6 and pyarrow 17.0.0 pinned to match; the first Spark start fetches the Delta Lake jar.
`credit-mailer-watermark-glue-redshift` needs none of that: it is Python Shell throughout, so `pip
install -r requirements.txt` and a `duckdb>=1.4` is the whole prerequisite.
`sba-credit-risk-ecs-dynamo` needs Python 3.12 and pandas 3, because its validator compares dtype
names exactly and pandas 3 names a text column differently from pandas 2; its own
`requirements.txt` covers the rest, and Docker is needed only to run the two images.
`voucher-float-kinesis-redshift` needs Python 3.12+ and its `requirements.txt`, and nothing else:
its `local-development/run_local.py` runs the whole chain, service to reconciliation.
The Spark jobs take a `--local` flag that runs them against plain filesystem paths, so every
pipeline can be exercised end to end without an AWS account.

`crypto-ticks-refinery-glue-dynamo` can also be run against the runtime it actually deploys to,
without installing any of the above:

```bash
cd crypto-ticks-refinery-glue-dynamo && ./local-docker-development.sh
```

That runs all five jobs inside `amazon/aws-glue-libs:glue_libs_4.0.0_image_01` — Spark 3.3.0,
Python 3.10, Java 8 — which is a stricter check than local Spark 3.5.5, because it is the API
surface the jobs are written against rather than a superset of it. The Latvian project's script
does the same with `public.ecr.aws/glue/aws-glue-libs:5`, Spark 3.5.4-amzn-0 and Delta Lake 3.3.0,
on its committed sample:

```bash
cd latvia-company-credit-glue-delta && ./local-docker-development.sh
```

The AWS-side values in each DAG are account-specific and must be set to your own before deploying:
EMR Serverless application id, IAM execution role ARN, S3 bucket and the `redshift_default` Airflow
connection for the bank-marketing pipeline; S3 bucket, Glue job names and the DynamoDB table for
the crypto-ticks one; the cluster, task definitions, subnets and security group in the state
machine definition for the SBA one; the bucket passed as `--bucket` and the names given to its two
Glue jobs for the Latvian one. Each project README states exactly what its own verification covers.

---

## How to Contribute

1. Fork this repository.
2. Clone your fork:

   ```bash
   git clone https://github.com/<your-username>/Finance.git
   cd Finance
   ```
3. Create a branch for your project:

   ```bash
   git checkout -b feature/your-project-name
   ```
4. Add your project in a new folder under this repo, with a clear README and any data files.
5. Commit and push your branch:

   ```bash
   git add .
   git commit -m "Add <project-name> analysis"
   git push origin feature/your-project-name
   ```
6. Open a Pull Request describing your project.

---

## Future Work

* Add new case studies in macroeconomic modeling, portfolio optimization, and machine learning in finance.
* Implement continuous integration for reproducible analyses.
* Include live data feeds and real-time strategy evaluation.

---

*© 2025 Vince Mbanze*
