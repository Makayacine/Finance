# voucher-float-kinesis-redshift

A gift-voucher service's lifecycle events -- **issued, redeemed, expired** -- carried from the
service's own database through **Kinesis, a Firehose transform Lambda and Redshift** to the three
numbers a finance team needs about voucher float: the **outstanding liability** every day,
**breakage** by issue month (the value that lapses unredeemed, which IFRS 15 B46 has a seller
recognise as revenue in proportion to the pattern of redemptions, so the expected rate is needed
before a cohort has closed), and the **redemption curve**. Two simulated years, 49,209 vouchers
and 97,588 events, and a `--local` mode that runs every stage on a laptop with no AWS account.

The point of the project is an estimate that can be checked:

> **On a cohort that is days old, counting what has happened says almost no breakage: 2.4% for
> October 2026. A life table over every voucher's age says 22.5%. The simulator fixed the true
> rate at 23.97% before the first voucher was issued, and across 22 month-end backtests the
> young-cohort estimate shows no bias against it (mean error -0.04 points); a single cohort is
> typically within about a point (mean absolute error 0.81 points, 16 of 22 within one), 2.2 at
> worst.**

Two halves, and the line between them is the outbox:

- **The service is the system of record.** Flask and SQLite, REST for the writes that move
  money -- login, issue, redeem -- and GraphQL for the reads. Every state change writes an outbox
  row in the same transaction, so a voucher can never change without its event, or the other way
  round.
- **The pipeline is the lab's.** A relay publishes the outbox to Kinesis; Firehose hands each
  batch to a transform Lambda that checks every event and derives its cohort and age; Firehose
  `COPY`s the result into Redshift, where six views turn events into liability, breakage, a life
  table and a reconciliation against the service's own total.

## Architecture

```
 voucher service (Flask, SQLite)
   POST /login       Argon2id; a JWT with the role
   POST /vouchers    issuer; Idempotency-Key     ┐ one transaction each:
   POST /…/redeem    merchant; one UPDATE        ├ the voucher row
   sweep             nightly; dated at expiry    ┘ + an outbox row
   POST /graphql     finance; reads the service          │
                     DB and the views below              │
                                                         ▼
 kinesis-producer/outbox-relay.py              outbox, oldest first
   put_records, 500 a call,                              │
   PartitionKey = voucher_code,                          ▼
   marked only once accepted                   Kinesis Data Stream
                                                         │
                                                         ▼
 firehose-lambda/lambda_function.py  ◄─ invokes ─ Firehose
   7 fields and their order checked;                     │  Ok: a JSON line each
   issue_month, days_since_issue,                        │  ProcessingFailed: processing-failed/
   days_to_redeem derived                                ▼
                                               S3 ── COPY ──> Redshift
                                               voucher_float.voucher_events_raw
                                                         │  warehouse/views.sql
                                                         ▼
                                               voucher_event            one row per event_id
                                               voucher_state            one row per voucher
                                               daily_liability          by UTC day, from occurred_at
                                               redemption_life_table    competing exits, censored
                                               breakage_by_issue_month  realised, or projected
                                               reconciliation           against the service's total
```

The reference lab's shape, with four changes, each argued below: the producer reads an outbox
the service writes transactionally rather than a loop of made-up rows; the transform emits JSON
lines and fails a bad record on its own, where the lab built CSV with f-strings; the warehouse
dedupes on `event_id` and dates everything from the events, never from arrival; and the
warehouse's liability is reconciled to the cent against the service.

## The data, and why it is simulated

The question this project answers -- how much of today's float will never be claimed -- has a
right answer only in a world whose rules are known. A real voucher book does not publish its
customers' intentions, so a breakage estimator run on real data can only be compared with another
estimate. `local-development/simulate.py` fixes the rules first and writes them, with the true
breakage rate and the true redemption curve, to `data/simulation-truth.json`:

| Parameter | Value |
| --- | --- |
| Period | 728 days from 2024-11-04, as of 2026-11-02 00:00 UTC |
| Issue pattern | Poisson over 07:00-19:00 UTC: 72 a weekday, 0.55x Saturday, 0.35x Sunday, 2x on the last three business days of each month |
| Amounts | 50.00 to 1,000.00 in six denominations, 5% store credit to the cent |
| Never redeem | 18% |
| Redemption delay | Weibull, shape 0.8, scale 9 days |
| Term | 30 days |
| **True breakage rate** | **0.18 + 0.82 e^-(30/9)^0.8 = 0.2397**, by count and by value |

Nothing is written to the database directly. Every voucher is issued with `POST /vouchers`, every
redemption attempted with `POST /vouchers/<code>/redeem`, through Flask's test client, and the
sweep runs nightly at 01:00 UTC on the service's own clock, which the simulator moves event by
event. Every response is checked against the plan -- 201, a replay of the same code, 200 before
expiry and 409 from it -- and the run stops at the first that is not. Two things are put in on
purpose: 2% of issue requests are sent twice under the same key, and the sweep is down from 9 to
15 March 2026.

| What the service did | |
| --- | --- |
| Vouchers issued | 49,209, worth 10,004,336.78 |
| Redeemed / expired / open at as_of | 37,144 / 11,235 / 830 |
| Redemptions refused because the voucher had lapsed (409) | 2,717 |
| Issue retries answered with the original voucher | 997 |
| Outbox events | 97,588 |

One seed, and the run reproduces itself: a full-scale run rewrites `data/simulation-truth.json`
byte for byte. A run with `--scale` or `--days` writes its truth into `_localrun/` instead.

## The service

### Writes are REST

| Endpoint | Role | Answers |
| --- | --- | --- |
| `POST /login` | anyone | 200 and a JWT carrying the role; 401 `invalid credentials` for a wrong password and an unknown user alike |
| `POST /vouchers` | issuer | 201; 400 for a bad body or a missing `Idempotency-Key`; 422 for a key reused with a different body |
| `POST /vouchers/<code>/redeem` | merchant | 200; 409 if already redeemed or lapsed; 404 if unknown |
| any of them | | 401 without a valid token, 403 with the wrong role -- in that order, before the body is read |

**Money is integer cents.** `amount_cents` must be a JSON integer above zero and at most
10,000,000: `100.0`, `1e3`, `"100"` and `true` are all refused, because a float that is whole
today stops being whole after somebody's currency conversion, and SQLite holds a 64-bit integer
that Python will happily overrun.

**Issuing needs an `Idempotency-Key`.** Issuing creates a liability, and a client whose request
timed out cannot tell whether it did. The first 201 for each (issuer, key) is stored with a hash
of the body: the same key and body -- key order and whitespace aside -- get that response back
byte for byte, and no second voucher. Only a 201 is stored, so a refused body can be fixed and
retried under the same key. The lookup and the insert share one `BEGIN IMMEDIATE` transaction,
which takes SQLite's write lock before the lookup: eight retries racing under one key get one
voucher between them, and eight issues under eight keys all succeed, where a plain `BEGIN`
answers some of them 500 `database is locked`.

**Redeeming is one statement.**

```sql
update vouchers set status = 'redeemed', redeemed_at = ?, redeemed_by = ?
 where code = ? and status = 'unredeemed' and expires_at > ?
```

The guard and the write are the same statement, so no second till slips between a check and an
update. The tests race two threads at each of 25 vouchers, and eight at one: one 200 every time.
A version that reads the voucher outside the write lock, checks it in Python and then updates by
code alone passes every other test and fails both of those. (Read and checked inside the
`BEGIN IMMEDIATE` transaction, it would pass them: the lock serialises the tills. The single
statement needs no such care.)

**The sweep dates a lapse at expiry.** A voucher stops being redeemable the second it expires; the
nightly sweep makes the lapse an event. That event's `occurred_at` is the voucher's `expires_at`,
not the sweep's run time, which is kept on the voucher row as `swept_at`. Stamping the sweep time
would make the daily liability depend on how often a scheduler fires.

**Codes are `VCH-` and 8 hex digits**: 32 bits, short enough to read over a till, but at 50,000
vouchers there is about a one-in-four chance of a repeat somewhere, so a collision draws a new code.

**Passwords are Argon2id** and the three users -- issuer, merchant, finance -- are seeded from
config at start-up, their plaintext from environment variables. An unknown username still costs
one Argon2 verification, so response time does not say which names exist. Every error body from
the REST routes is one fixed phrase per status: no field names, no echo of input, no stack trace.
A body nested too deep for the JSON parser is a 400 like any other bad body.

### Reads are GraphQL

Graphene on the same Flask app, finance role only, **three queries and no mutation type**:

```graphql
query Float($code: String!) {
  voucher(code: $code) { code status amountCents redeemedAt events { eventType occurredAt publishedAt } }
  liability(asOf: "2026-11-01") { outstandingCents outstandingVouchers }
  cohort(month: "2026-10") { issued redeemed expired open breakageRate week: redeemedBy(days: 7) }
}
```

`voucher` reads the service's own database, with each event's `publishedAt` from the outbox: it is
the question "what does the system of record say right now". `liability` and `cohort` read the
warehouse, where copies have been dropped and late events placed on their own days -- DuckDB
locally, the Redshift Data API on AWS, the same three statements either way. Money is `BigInt`
cents, because GraphQL's `Int` is 32-bit and runs out at 21,474,836.47. A failure inside a
resolver reports `internal error` and nothing else, and a body that is not a GraphQL request --
`variables` that are not an object, a query nested past the parser's limit -- gets
`invalid request`. A query that does not parse or validate gets graphql-core's own message,
which can name a field or repeat a value: `Cannot query field 'nope' on type 'Voucher'`.

## The pipeline

**The relay is at least once, on purpose.** `kinesis-producer/outbox-relay.py` reads unpublished
outbox rows oldest first, sends up to 500 a `put_records` call keyed by `voucher_code`, and marks a
row only once the response shows the stream accepted it. A crash between the call and the mark
re-sends the batch; a throttled record in a partly accepted call goes again on the next pass,
behind records that were after it. The alternative to a duplicate is a lost event, and a lost
redemption is a liability the warehouse would report forever. The partition key keeps one
voucher's events on one shard, which is as much order as the stream promises; nothing downstream
needs more.

**The transform checks, derives and fails one record at a time.** `firehose-lambda/lambda_function.py`
takes the lab's contract -- `event['records']`, base64 `data`, every `recordId` back once -- and
holds each event to exactly its seven fields, their types and the order a voucher's life allows:
an issue at `issued_at`, a redemption before `expires_at`, an expiry at it. It derives
`issue_month`, `days_since_issue` and `days_to_redeem` from the event's own `issued_at`, never from
an assumed term, and emits one JSON line, which `COPY ... JSON 'auto'` matches to columns by name.
A bad record goes back `ProcessingFailed` with its data untouched, to the bucket's
`processing-failed/` prefix, and the rest of the batch loads. `sample-events/firehose-batch.json`
is a Firehose event with four good records and seven bad ones, the lab's CSV line among them.

**The warehouse builds everything from `occurred_at`.** A sweep that runs a week late puts a
week of expiries on the stream after redemptions dated up to a week later than them, and the
relay's retries reorder records too. So `warehouse/views.sql` never uses the order rows arrived
in: `voucher_event` keeps one row per `event_id`, `voucher_state` takes each voucher's issue and
its earliest exit, and `daily_liability` adds each voucher on its issue day and takes it off on its
exit day. A late event corrects every past day it belongs to the next time a view is read.

The same SQL text runs on DuckDB and on Redshift: no `generate_series`, no `QUALIFY`, no
`datediff` (the two engines spell it differently), an explicit frame on every window aggregate
with an `ORDER BY`, every division of two integers cast to `double precision` first, and ages
from `extract(epoch from ...)`. A test checks the text for each of those, and another builds the
views a second time with DuckDB dividing integers as Redshift does, which changes no figure. A
third holds `warehouse/landing-tables.sql` and `redshift/redshift-create-tables.sql` to the same
columns and types.

## What it finds

### The liability

The float runs at 173,086 on an average day and moves with the issue pattern: each month-end
reward run doubles that day's issuance and runs off over the next 30 days. At the end of
1 November 2026 it is **171,795.16 on 830 vouchers**.

**The sweep outage changes none of it.** A report that dated each lapse at the sweep that
recorded it would run a day behind the warehouse every night, outage or not: the sweep at 01:00
records the previous day's lapses, so that report sits above the warehouse by exactly the day's
expiries -- 4,181.12 on 8 March, 1,679.22 on 16 March. The outage added to that lag. Against the
same report with the sweep on time, it overstated the liability by 4,181.12 on 9 March, rising to
15,337.74 on the 15th, and from the 16th the two agree again. The warehouse's series runs through
the outage as if the sweep had been on time, and `tests/test_warehouse.py` proves it end to end:
the same small simulation with the sweep on time and a week late gives identical daily
liability, and before the catch-up's events arrive the outage days really are overstated.

### Breakage by cohort

A cohort is one calendar month of issues. 23 have closed; their realised breakage by value runs
0.2178 to 0.2636, mean 0.2384, standard deviation 0.0142 -- about what 2,000 vouchers a month give
once a 1,000.00 voucher counts twenty times a 50.00 one.

A cohort still open needs an estimate. `breakage_by_issue_month` adds to the cohort's realised
lapses, for every open voucher, its value times the chance that a voucher of its age goes on to
lapse:

    P(lapse | open at age a)  =  (final expired share - expired share at a) / survival at a

read off `redemption_life_table`, an actuarial life table by days since issue, in cents, with
redeemed and expired as competing exits and every open voucher censored at its age, counting half
on its last day. The pooled table puts breakage at **0.2379** against the true 0.2397, and its
curve against the true one:

| Redeemed within | 1 day | 3 days | 7 days | 14 days | 21 days | 30 days |
| --- | --- | --- | --- | --- | --- | --- |
| warehouse | 0.1327 | 0.2818 | 0.4610 | 0.6254 | 0.7076 | 0.7621 |
| true | 0.1299 | 0.2787 | 0.4581 | 0.6226 | 0.7056 | 0.7603 |

### The estimator, on cohorts days old

At each month end from January 2025 the warehouse is rewound -- `warehouse_as_of` set to midnight
on the 1st, so every later event is invisible and every open voucher censored at its age then --
and the month just ended is estimated, with 40-50% of its vouchers still open:

| 22 month-end estimates | Mean error | Mean absolute error | Within a point | SD | Largest miss |
| --- | --- | --- | --- | --- | --- |
| projected, against the true rate | **-0.0004** | 0.0081 | 16 | 0.0098 | 0.0222 |
| projected, against what the cohort went on to do | +0.0019 | 0.0098 | 12 | 0.0118 | 0.0253 |
| naive -- expired over resolved -- against the true rate | -0.2327 | 0.2327 | 0 | 0.0076 | 0.2397 |

The mean error says the estimator has no bias; it does not say any one estimate is that close. A
single month's projection is typically 0.8 of a point from the true rate and 2.2 at worst, because
each young cohort is one draw of about 2,000 vouchers. The naive rate is what a report gets by
counting what has happened, and it would book almost no breakage on a young cohort. **The backtest
also caught the estimator's one bias.** An earlier version read an open voucher's survival at the
start of its last day, ignoring the part of that day it had already come through without
redeeming; with day 0's hazard at 13%, every young cohort came out about half a point low, -0.0060
against the truth. It now interpolates across the day, the same assumption the half-day censoring
makes, and two four-voucher tests pin the arithmetic, one for each half of it.

### The reconciliation

| | Service | Warehouse |
| --- | --- | --- |
| Unredeemed cents at 2026-11-02 00:00 | 17,179,516 | 17,179,516 |
| Unredeemed vouchers | 830 | 830 |
| Difference | | **0** |

Beside it, `reconciliation` counts exits whose issue never arrived and vouchers with two exits --
either would explain a difference, and both are zero -- and the copies the dedupe dropped, which
is informational: the relay delivers at least once by design, and the copies are gone before
anything is summed. The comparison is like with like only when the control total is taken
straight after a sweep: until a lapsed voucher is swept its row still says `unredeemed`, while the
warehouse, dating the lapse at expiry, has already taken it off. And the service can only be read
as it is now, so `warehouse_local.py` refuses an `--as-of` earlier than the service's latest
issue, redemption or sweep rather than put that label on a later balance; without one it reports
as of that latest change.

## Running it

Python 3.12+ and `pip install -r requirements.txt`. No AWS account.

```bash
python -m pytest tests -q
```

The whole chain from an empty `_localrun/` -- simulate, relay, transform, `COPY`, reconcile -- in
about two minutes, almost all of it the 90,000 or so REST calls:

```bash
python local-development/run_local.py
```

Or stage by stage:

```bash
python local-development/simulate.py
python kinesis-producer/outbox-relay.py --local
python local-development/firehose_local.py
python local-development/warehouse_local.py --as-of 2026-11-02T00:00:00Z
```

The API, to walk with curl:

```bash
python -m voucher_service serve --db _localrun/voucher-service.db --warehouse _localrun/warehouse.duckdb
```

```bash
curl -s -X POST localhost:5000/login -H 'Content-Type: application/json' -d '{"username":"issuer","password":"issuer-local-only"}'
```

`voucher-float-walkthrough.ipynb` builds `_localrun/` if it is not there and walks every figure
in this README.

## What the local run covers

**The service you deploy, as it is.** The REST and GraphQL endpoints, the transactions, the
outbox, the sweep and the SQLite file are the same code and the same database engine on both
paths. The simulator drives all 49,209 vouchers through the real endpoints.

**Every stage between the outbox and the views, with a stand-in at each AWS service:**

| On AWS | Locally | What is the same |
| --- | --- | --- |
| Kinesis Data Stream | `FolderStream`: a JSONL file per shard | the records `put_records` is given, the shard chosen from the MD5 of the partition key as Kinesis chooses it |
| Firehose | `firehose_local.py` | Firehose's event shape, a batch at a time, the response contract checked, `Ok` and `processing-failed/` objects, a checkpoint per shard |
| The transform Lambda | the same file, called in-process | all of it: the handler is the one you deploy |
| Redshift and its `COPY` | DuckDB and `read_json` | `warehouse/views.sql`, unchanged; the landing columns and types |

**176 tests, no AWS, no network.** `test_auth.py` (22): Argon2id at rest, the same 401 for a
wrong password and an unknown user, every bad token -- other key, expired, `alg: none`, unknown
role, no expiry -- 401 before 403 before the body, and JSON nested too deep refused with 400.
`test_issue.py` (37): the code and the 30-day expiry, the outbox row, the replay byte for byte,
422 for a reused key, keys per issuer, 21 bodies refused with nothing written, a code collision
redrawn, an outbox insert made to fail inside SQLite taking the voucher with it, and eight
requests at once under one key and under eight. `test_redeem.py` (11): 200, 409, 404,
unredeemable from the expiry second, a failed outbox insert taking the redemption with it, and
the two races. `test_sweep.py` (7): overdue and unredeemed only, dated at expiry, harmless twice,
the boundary second shared with redemption, and a failed outbox insert taking the whole sweep
with it. `test_relay.py` (11): outbox order, nothing twice, one shard per voucher, batch sizes and
the 500-record cap, the stream name and partition key Kinesis is given, throttled records retried
through the AWS class itself, giving up with nothing marked, and a crash before marking
re-sending copies that share an `event_id`. `test_simulate.py` (6): the plan drawn from its
parameters, the formula, a byte-identical rerun, and a scaled run leaving the committed truth
alone. `test_lambda.py` (29): the sample batch, the derived fields, 22 malformed events -- the
boundaries of a voucher's life among them -- and a record too deep to parse failing alone.
`test_warehouse.py` (16): the DDL pair, the portable SQL by its text and by integer division,
the reconciliation, copies dropped, a lost redemption found, two exits and an orphan exit
counted, the late sweep, nothing after `as_of` visible, an earlier `as_of` refused, and three
life tables worked by hand. `test_graphql.py` (37): finance only, exactly three queries, each
against the warehouse, `redeemedBy` against the service's own rows, refusals down to a year 0,
Arabic-Indic digits and `variables` of `0`, and a resolver failure saying only `internal error`.

**Three engine differences to carry into the deploy.** Firehose's `COPY` into Redshift is at least
once as well, which the dedupe already absorbs. The local stream keeps shard files in the order
records were put; Kinesis keeps order only within a shard, which is all the warehouse assumes.
And the reconciliation's two sides must be read at the same moment: locally the run ends on a
sweep at `as_of`, so they are; on AWS, close on the nightly sweep -- take the control total
straight after it, and move `as_of` to the sweep's time once the relay and Firehose's buffer have
caught up past it.

## Deploying it

1. Create the Kinesis Data Stream `<stream-name>` in `<region>`, on-demand or with `<shards>`
   shards, and an S3 bucket `<bucket>` for Firehose's intermediate objects and its
   `processing-failed/` prefix.
2. Create a Redshift Serverless workgroup `<workgroup>` with database `<database>`. In one query
   editor session run `redshift/redshift-create-tables.sql`, then `warehouse/views.sql` unchanged.
   Firehose reaches Redshift over the workgroup's public endpoint on port 5439: turn on public
   access and allow Firehose's address range for `<region>` in the workgroup's security group.
3. Create the Lambda `<transform-function>` on Python 3.12 from `firehose-lambda/lambda_function.py`,
   handler `lambda_function.lambda_handler`, 256 MB, a 60-second timeout. It needs only the
   standard library. Test it with `firehose-lambda/sample-events/firehose-batch.json`: four `Ok`,
   seven `ProcessingFailed`.
4. Create the Firehose stream `<delivery-stream>`: source `<stream-name>`; transform with
   `<transform-function>`, buffering 1 MB or 60 seconds; destination Redshift `<workgroup>`, database
   `<database>`, table `voucher_float.voucher_events_raw`, with the column list and the `COPY`
   options in the header of `redshift/redshift-create-tables.sql`; intermediate bucket `<bucket>`.
   Its role needs `kinesis:DescribeStream`, `GetShardIterator` and `GetRecords` on the stream,
   `lambda:InvokeFunction` on the transform, and `s3:PutObject`, `GetObject` and `ListBucket` on
   the bucket.
5. Run the service on an EC2 instance `<instance>` with the SQLite file on an EBS volume at
   `<db-path>`: `pip install flask graphene argon2-cffi pyjwt boto3 gunicorn`, then
   `gunicorn -b 0.0.0.0:<port> voucher_service.wsgi:app` behind `<load-balancer>` with TLS. Set
   `VOUCHER_DB_PATH=<db-path>`, `VOUCHER_JWT_SECRET` and the three `VOUCHER_*_PASSWORD` variables from
   Secrets Manager `<secret-id>`, and `VOUCHER_REDSHIFT_WORKGROUP=<workgroup>`,
   `VOUCHER_REDSHIFT_DATABASE=<database>` and `AWS_REGION=<region>` for the GraphQL reads.
6. On the same instance, schedule the relay every minute and the sweep nightly at 01:00 UTC:
   `python kinesis-producer/outbox-relay.py --db <db-path> --stream-name <stream-name> --region <region>`
   and `python -m voucher_service sweep --db <db-path>`. The instance role needs
   `kinesis:PutRecords` on the stream; `redshift-data:ExecuteStatement`, `DescribeStatement` and
   `GetStatementResult`; and `redshift-serverless:GetCredentials` on the workgroup.
7. Close each night on the sweep. Straight after it, take the control total --
   `sqlite3 <db-path> "select count(*), sum(amount_cents) from vouchers where status = 'unredeemed'"`
   -- and once the relay has drained and Firehose's buffer has passed `<sweep-time>`, write it and
   move `as_of` in one transaction:
   `aws redshift-data batch-execute-statement --workgroup-name <workgroup> --database <database> --sqls "delete from voucher_float.service_control_total" "insert into voucher_float.service_control_total values ('<sweep-time>', <vouchers>, <cents>)" "update voucher_float.warehouse_as_of set as_of = '<sweep-time>'"`.
   The warehouse then reports as of the close, and `voucher_float.reconciliation` shows
   `difference_cents` 0.

Every AWS name here is account-specific and set by whoever deploys it.

## What the simulation cannot say

- **The truth is the simulator's.** The estimator recovers an 18% never-redeem share and a
  Weibull delay because those are what generated the data. A real programme whose terms,
  channels or customers change would redeem differently from its own history, and the backtest
  table is where that would show: young-cohort projections drifting from what their cohorts go
  on to do.
- **Breakage is assumed independent of the amount.** The simulator draws them separately, so the
  rate is the same by count and by value. Real customers spend a large voucher more surely than a
  small one, and the value-weighted rate would then sit below the count-weighted one; the views
  are already in cents for that reason.
- **One currency, one term, no partial redemption.** A voucher is spent whole or not at all.
  Partial spends would make `amount_cents` on a redemption a part of the voucher and the state a
  running balance.
