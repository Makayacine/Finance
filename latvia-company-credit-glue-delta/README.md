# latvia-company-credit-glue-delta

A 12-month probability of default for Latvian companies, from the annual accounts they file and
labelled from the insolvency register: **115,088 companies scored on 1 October 2026**, by a
scorecard fitted, calibrated and tested out of time on **1,607,967 scored company-years since
2009**, out of 2,354,336 live ones. Two AWS Glue 5.0 jobs on Delta Lake: one MERGEs dated batches of
the company register, the insolvency register and the accounts into three Delta tables; the other
reads those tables as they stood on each observation date, fits the scorecard and MERGEs today's
scores into a fourth. A `--local` mode runs both on a laptop with no AWS account, and
`local-docker-development.sh` runs them inside AWS's own Glue 5.0 image.

The point of the project is the lesson of the lab it imitates:

> **A MERGE only means something when the same key comes back with a changed value.** Here a
> company comes back when it is terminated, a proceeding when it ends and a statement when it is
> refiled, so the MERGE restates rather than appends — and the PD's training rows are read from
> the tables as they stood on each observation date, which only a table that kept its history can
> give.

## Architecture

```
 host                        bucket (--local: _localrun/bucket/)
 fetch_raw_data.py
 stage_batches.py    ──>     raw_zone/register/register_<date>.csv
                             raw_zone/insolvency_legal_person_proceeding/..._<date>.csv
                             raw_zone/financial_statements/, balance_sheets/, income_statements/
                                  │   73 quarterly batches, 2008-12-31 to 2026-11-01
                                  ▼
                 Glue 5.0  glue-delta-merge.py --table companies | proceedings | statements
                           explicit schema -> curate -> one winner per key -> MERGE,
                           one commit per batch, batch date in userMetadata -> archived/
                                  │
                                  ▼
                 lakehouse-dwh/companies     regcode                restated on termination
                 lakehouse-dwh/proceedings   proceeding_id          restated when it ends
                 lakehouse-dwh/statements    regcode, fiscal_year   partitioned by fiscal_year
                                  │   read VERSION AS OF the batch of each 30 September
                                  ▼
                 Glue 5.0  glue-credit-model.py
                           company-years 2009-2025 -> WoE scorecard -> results/model/*.csv
                           today's companies -> lakehouse-dwh/scored_companies (MERGE)
```

The reference lab is one PySpark script on the Glue 3.0 image, with a `delta-core` 1.0.0 jar handed
to `spark-submit`: it reads `raw_zone/<table>/` with `inferSchema`, MERGEs on the key with a bare
`whenMatchedUpdateAll`, and moves the files to `archived/`, logging and swallowing any error. The
same three zones and the same MERGE are here, with six changes argued under *The pipeline*.

## The data

| Source (data.gov.lv, Register of Enterprises, CC0-1.0) | Rows | Companies' rows |
| --- | --- | --- |
| `register.csv`, every entity ever registered | 489,966 | 347,053 |
| `insolvency_legal_person_proceeding.csv` | 18,053 | 17,548, for 15,471 debtors |
| `financial_statements.csv`, one row per annual report | 1,986,118 | 1,708,249, for 221,741 companies |
| `balance_sheets.csv` | 1,861,769 | joined on `statement_id` |
| `income_statements.csv` | 1,752,742 | joined on `statement_id` |

Retrieved 2026-11-02, as the Register published them on 2026-11-01. `fetch_raw_data.py` resolves
each download URL from the dataset's CKAN record at run time, renames a file into place only once
its byte count matches the server's, and records its row count and sha256 in a manifest. The 655 MB
of `data/raw/` is gitignored. The cash-flow statements (181,096, a tenth of the filings) are
fetched and not used.

**Companies only, decided on read.** A company is a private limited company (SIA, 339,247 of them),
cooperative society (KB, 3,770), joint-stock company (AS, 2,448), general or limited partnership
(PS 1,128, KS 450) or European company (SE, 10). Individual merchants, individual and family
enterprises, farms and fishing holdings carry their owners' names and are dropped before anything
is written, with associations, foundations, churches, unions, parties, the media register and
foreign branches. Every reader parses only an allow-listed set of columns — 5 of the register's 21,
8 of the insolvency file's 23 — so the register's name and address columns and the insolvency
file's debtor identity columns are never read.

**`data/sample/` is committed** so the chain and the tests run straight after a clone. It is cut by
company, not by row: up to 70 companies whose first default started in each year 2008-26, 60 whose
only applications were dismissed, 40 that filed group accounts, 40 with two statements for one year
and 3,600 with no default, each with all of its rows — 5,069 companies, 1.7 MB. **Every figure in
this README is from the full files**, printed by a cell of `company-credit-walkthrough.ipynb`,
except those it says come from a run on the sample, on a copy of a table or in the Glue 5.0 image,
and the model job's memory peak.

## What a default is

**The first start of a non-dismissed insolvency, legal-protection or out-of-court legal-protection
proceeding** — Basel's "bankruptcy or similar protection". Legal protection is a default because
the company has asked the court to hold its creditors off, and the file bears it out: of the 1,991
companies whose first default was legal protection, 1,199 later entered insolvency anyway.

Seven resolution codes mean the court never opened the case: M01 and M12 (an insolvency application
dismissed or left without action), T07, T18 and T19 (the same for legal protection) and A09 and A12
(out of court). They are the dismissal and left-without-action codes of the Register's own code
sheet, and they remove 908 of the companies' 17,548 proceedings: M01 649, T18 189, T07 35, A09 29,
T19 6. M01 occurs only for applications made in 2008-12, a median of 38 days before the dismissal;
since 2013 the register records an insolvency once it is declared.

| | 2008 | 2009 | 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026* |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| first defaults | 1,344 | 2,253 | 2,369 | 816 | 885 | 877 | 963 | 802 | 747 | 612 | 625 | 583 | 398 | 280 | 353 | 277 | 344 | 312 | 270 |
| of which legal protection | 8 | 56 | 158 | 91 | 109 | 170 | 175 | 109 | 110 | 113 | 127 | 108 | 90 | 72 | 110 | 82 | 118 | 99 | 86 |

\* to 30 October. 15,110 companies in all.

### The label rule, stated once

A row is a company on 1 October of a year, the observation date; its horizon runs to the next 1
October.

- **Population**: every company registered before the observation date and not terminated before
  it, as the register stood on its eve, that is not already in default as the insolvency register
  stood that day.
- **Default**: its first non-dismissed proceeding, as the insolvency register stood on the eve of
  the horizon's end, started before that end. A case started a few days before the observation date
  and entered after it counts: the horizon is when it became known. An application still undecided
  at the horizon's end counts as it stood then.
- **Accounts**: the latest statement available on the eve of the observation date whose period
  ended within the 24 months before it — the last fiscal year or, from a late filer, the one
  before. A company with neither is in the population and not scored.
- **The as-of date** is the newest `entry_created_on` in the insolvency file, 2026-10-30. A horizon
  that ends after it is not labelled, so the labelled years are 2009-2025.

The insolvency register is no source of hindsight: `entry_created_on` follows the start by a median
of 1-5 days in every year. 119 of the 12,133 labelled defaults started before their observation
date and were entered after it, and 105 are applications undecided at the horizon's end and
dismissed since, which a label read from today's register would miss.

## When accounts exist: the bulk load and the embargo

`created_at` looks like a filing time, and from 18 January 2021 it is one. **Every statement the
Register published before that date carries the stamp of its bulk load instead**: 1,152,314
statements, 99.87% of them for FY2019 or earlier, stamped between 2020-08-11 and 2021-01-17, and
1,113,708 of them on 73 days with more than 3,000 stamps each, at a median of 17,534 a day. No day
since has seen more than 10,834. Read as filing dates, the stamps would make the median FY2008
statement 4,242 days late and leave no accounts at all before August 2020.

So the rule, from `company_credit/config.py`: **a statement stamped before 2021-01-18 is dated nine
months after its period end, and never later than its stamp**, which is an upper bound on when it
was filed; one stamped since is dated by its stamp. Nine months is measured on the real stamps. Of
companies' own accounts for 31 December year ends:

| Fiscal year | 2020 | 2021 | 2022 | 2023 | 2024 |
| --- | --- | --- | --- | --- | --- |
| filed by 30 June | 50.7% | 45.1% | 84.5% | 85.3% | 88.4% |
| **filed by 30 September** | **93.1%** | **91.6%** | **92.9%** | **93.2%** | **95.1%** |
| median days after year end | 180 | 195 | 142 | 145 | 144 |

The observation date is therefore 1 October, nine months after a 31 December year end, which 99.2%
of all statements have.

**What the rule costs, measured.** The test years, 2021-25, have real stamps, so they can be scored
twice: once as they are, once with every statement re-dated the bulk way, as if its filing time
were unknown. With real dates the test years score 560,508 company-years at an AUC of 0.8176 and
cover 90.2% of their defaulters. Re-dated, 14,084 more company-years are scored, defaulting at
0.33% against 0.26%; defaulter coverage rises to 93.1% and the AUC to 0.8182. The re-dating hands
39,570 company-years accounts newer than they had filed by 1 October, or accounts where they had
none, and those default at 0.41% against 0.25% for the rest: late filers are riskier, and the bulk
dating hides them. The cost to the ranking is small here because a late filer is still scored, on
the year before's accounts, and `accounts_lag` says so. Among companies scored on last year's
accounts only, a design that has no room for a late filer, the re-dating moves the AUC from 0.8080
to 0.8151 and adds 38,200 company-years that default at 0.41% against 0.23%. That is the leak the
training years carry and the test years do not, and it is why the test window is 2021-25: every
statement it uses has a real stamp or predates the window.

## The replay

The Register publishes today's state, nightly. `stage_batches.py` turns it back into the quarterly
extracts it would have published, using only the files' own dates: a company arrives on
`registered` and comes back, restated, on `terminated`; a proceeding arrives open on
`entry_created_on` and comes back on `proceeding_ended_on` with its resolution; a statement arrives
on the date the rule above gives it. Nothing is invented: a row whose state changed inside its
arrival quarter is sent once, in its final state, and the last batch is dated with the newest stamp
in the files, 2026-11-01, rather than with the end of a quarter still to come.

| Table | Batches | Rows MERGEd | Inserted | Restated | Share restated | Per batch, after the first |
| --- | --- | --- | --- | --- | --- | --- |
| `companies` | 73 | 498,530 | 347,053 | 151,477 | 30.4% | 10.9% - 83.0% |
| `proceedings` | 73 | 33,303 | 17,548 | 15,755 | 47.3% | 25.2% - 68.4% |
| `statements` | 72 | 1,703,441 | 1,702,893 | 547 | 0.03% | 0% - 10.4% |

The register comes back for 151,477 companies, the ones that terminated after the quarter they
arrived in, and the insolvency file for 15,755 of its 17,548 proceedings, which arrive open and end
later. Statements almost never come back: 547 were restated by a refiling or a later period
arriving after the statement it replaces, and one arrived unchanged. Inside their own batch, 396
duplicate keys were cut to their winner before the MERGE and 4,412 group statements were dropped.
Every count is read back from the tables' own history: the batch date travels in each commit's
`userMetadata`, and Delta's operation metrics give the inserts and updates.

**Time travel builds the training rows.** For each observation year the model job reads the three
tables at the version of the batch dated 30 September, and the insolvency table again a year on. On
the eve of four observation dates, the proceedings table as it stood then against today's table
filtered to the proceedings entered before that date:

| Eve | Version | Proceedings, as of | Open then | Open in today's table, filtered | Open then, dismissed since |
| --- | --- | --- | --- | --- | --- |
| 2012-09-30 | 15 | 8,448 | 2,712 | 2 | 7 |
| 2016-09-30 | 31 | 12,473 | 1,165 | 12 | 4 |
| 2020-09-30 | 47 | 15,162 | 749 | 43 | 0 |
| 2024-09-30 | 63 | 16,722 | 772 | 293 | 5 |

The filter finds the same proceedings, but each with today's end date and resolution: of the 2,712
open on the eve of 1 October 2012 it shows 2 open, and 7 of the 2,712 now carry a dismissal code,
so a label read from it would drop them. Here the replay was built from the files' own change
dates, so a filter that also cut every changed column back to the day could rebuild these views;
the version needs no such knowledge of which column changed when. In production, where each night's
extract is MERGEd as it lands, a correction that carries no date of its own still lands in a
version, and only the version can give the earlier value back.

**Versions are found by batch date, never by `timestampAsOf`.** A replay commits a table's 73
batches within minutes, so a commit's wall-clock time says nothing about the business date it
carries: on the full proceedings table they span 244 seconds of one afternoon, and `timestampAsOf`
2016-09-30 is refused — `The provided timestamp (2016-09-30 00:00:00.0) is before the earliest
version available` — while the batch date finds version 31: 12,473 proceedings, 1,165 of them open.
Each commit carries its batch date in `userMetadata`, and `lakehouse.version_as_of` picks the
newest version dated on or before the day asked for; a re-run of a batch commits a second version
under the same date, and the later one wins. A test merges a later batch and requires the earlier
as-of view to come back row for row unchanged.

**The cadence does not change the model.** Every observation date is a 1 October, so a batch
boundary on each 30 September is all the as-of reads need. `--cadence year` cuts one batch a year
there. On the sample, the quarterly chain (73 batches) and the yearly one (20) built the same
35,282 rows and the same coefficients, in 855 s against 384 s.

## The PD

### Coverage

A company is scored when it has accounts no more than two fiscal years old.

| Window | Live company-years | Scored | Defaults | Of them scored |
| --- | --- | --- | --- | --- |
| train, 2009-17 | 1,247,720 | 700,965 (56.2%) | 9,190 | 5,149 (56.0%) |
| validate, 2018-20 | 434,288 | 346,494 (79.8%) | 1,330 | 1,253 (94.2%) |
| test, 2021-25 | 672,328 | 560,508 (83.4%) | 1,613 | 1,455 (90.2%) |

By year, the scored share runs from 15.0% of live companies in 2009 and 29.4% in 2010 to 60.8% in
2013 and 82.3-83.8% from 2019, and the share of defaulters scored from 18.0% in 2009 to 88.5-95.6%
from 2016.

The early years are thin because the published accounts start with FY2008 and reach most companies
only from about FY2013. A company with no recent accounts is not, on the whole, a riskier one: in
2021-25 the unscored default at 0.14% a year against 0.26% for the scored, and in 2018-20 at 0.09%
against 0.36%. In 2009-17, when accounts reached barely half the population, the two rates are all
but the same: 0.74% for the unscored and 0.73% for the scored. A dormant company that stops filing
is usually struck off rather than taken to court.

### The scorecard

The course's scorecard. Each numeric characteristic is cut at its training deciles, with a bin of
its own for a ratio whose base is zero or negative (an empty shell has no assets to divide by) and
for a statement with no balance sheet attached; each bin's WoE replaces the value, and a logistic
regression is fitted on 2009-17 with p-values clustered on the company, since a firm appears once a
year for up to seventeen years. A characteristic leaves at an IV under 0.02, or for a wrong sign or
a p-value over 0.05, one at a time.

| Characteristic | IV, 2009-17 | Coefficient | Clustered s.e. | z |
| --- | --- | --- | --- | --- |
| total assets | 0.537 | −0.359 | 0.032 | −11.2 |
| net turnover | 0.521 | −0.594 | 0.033 | −17.9 |
| equity / total assets | 0.504 | −0.637 | 0.032 | −19.7 |
| cash / total assets | 0.394 | −0.592 | 0.031 | −19.0 |
| current assets / current liabilities | 0.388 | −0.393 | 0.028 | −14.1 |
| employees | 0.354 | −0.305 | 0.030 | −10.1 |
| interest expense / liabilities | 0.248 | −0.184 | 0.039 | −4.7 |
| net income / total assets | 0.159 | −0.855 | 0.054 | −15.8 |
| legal form (SIA, AS, KB, other) | 0.032 | −0.512 | 0.074 | −7.0 |
| `accounts_lag` (last fiscal year or the one before) | 0.016 | −0.557 | 0.104 | −5.4 |
| constant | | −4.951 | 0.018 | |

Each coefficient multiplies its characteristic's WoE, so every one is negative, as it should be: a
bin that holds more defaults than its share has a negative WoE and raises the PD.

Two characteristics leave: asset turnover at an IV of 0.0088, and the firm's age, which clears the
IV screen at 0.0395 but carries a p-value of 0.473 once the accounts are in.

**Late filing is in by design.** `accounts_lag` says whether the latest accounts are for the last
fiscal year or the one before. Its IV is 0.0157 on 2009-17, under the screen's 0.02, and 0.0962 on
2021-25. The difference is the bulk load: in the training years a statement filed two years late is
dated as if it had been filed on time, so many late filers look like on-time ones. With real dates,
the 7.7% of test company-years scored on the year-before's accounts default at 0.60% against 0.23%.
So it skips the IV screen by design and must still pass the other two: its coefficient is −0.557
with a clustered p-value of 8.4 × 10⁻⁸. Left to the screen, it goes, and the AUC is 0.8001, 0.8012
and 0.8151 on the three windows against 0.8008, 0.8027 and 0.8176 with it.

The logistic regression is written out in numpy (`company_credit/logit.py`), because Glue 5.0 ships
numpy and pandas and neither statsmodels nor scipy. On the 700,965 training rows it converges in 8
Newton steps and 1.3 s, and statsmodels, fitted beside it on the same WoE columns, agrees to
3.3e-16 on the coefficients and 8.3e-17 on the clustered standard errors; a test holds the two
together on simulated data with firm effects. Clustering moves the standard errors by factors of
0.72-1.12 against the naive ones, wider for eight of the ten characteristics.

### Out of time

Fitted on 2009-17, the validation level set on 2018-20, tested on 2021-25. AUC intervals are
DeLong's. The benchmark is the equity ratio alone.

| Window | Scored company-years | Defaults | Default rate | AUC [95% CI] | Gini | KS | Equity ratio AUC |
| --- | --- | --- | --- | --- | --- | --- | --- |
| train, 2009-17 | 700,965 | 5,149 | 0.73% | 0.801 [0.795, 0.807] | 0.602 | 0.453 | 0.647 |
| validate, 2018-20 | 346,494 | 1,253 | 0.36% | 0.803 [0.791, 0.815] | 0.605 | 0.455 | 0.651 |
| **test, 2021-25** | 560,508 | 1,455 | 0.26% | **0.818** [0.807, 0.829] | 0.635 | 0.482 | 0.654 |

The ranking holds out of time and does not decay: every test year scores 0.799-0.834, and every
year since 2009 beats the equity ratio, by 0.111 to 0.181 of AUC. The test window is the one with
real filing dates, where late filing is a sharper signal, and without `accounts_lag` its AUC is
0.815.

### Calibration: two levels

The model is fitted through the cycle of 2009-17, when 0.73% of scored companies defaulted within a
year; since 2019 the rate has been 0.24-0.34%. So it carries two levels, side by side. **Through
the cycle** is the model as fitted. **The validation level** shifts its log-odds by one constant,
−0.506, so that the mean PD over 2018-20 equals those years' observed 0.36%. On the test years the
through-the-cycle mean PD is 0.51% against an observed 0.26%, and the validation level's 0.31%. A
shift moves no ranking, so the AUC is the same under both; only the level differs.

| Year | Scored | Defaults | Observed | PD, TTC | O/E, TTC | PD, validation | O/E, validation | AUC | PSI |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2009 | 15,142 | 446 | 2.95% | 1.05% | 2.79 | 0.64% | 4.58 | 0.780 | 0.093 |
| 2010 | 30,715 | 391 | 1.27% | 1.02% | 1.24 | 0.62% | 2.04 | 0.771 | 0.094 |
| 2011 | 55,570 | 457 | 0.82% | 0.90% | 0.92 | 0.55% | 1.51 | 0.780 | 0.036 |
| 2012 | 75,138 | 592 | 0.79% | 0.81% | 0.97 | 0.49% | 1.60 | 0.801 | 0.009 |
| 2013 | 87,714 | 678 | 0.77% | 0.75% | 1.03 | 0.46% | 1.69 | 0.800 | 0.001 |
| 2014 | 100,943 | 764 | 0.76% | 0.69% | 1.10 | 0.42% | 1.82 | 0.791 | 0.003 |
| 2015 | 108,701 | 653 | 0.60% | 0.68% | 0.89 | 0.41% | 1.46 | 0.805 | 0.004 |
| 2016 | 112,016 | 590 | 0.53% | 0.66% | 0.80 | 0.40% | 1.31 | 0.808 | 0.007 |
| 2017 | 115,026 | 578 | 0.50% | 0.64% | 0.78 | 0.39% | 1.28 | 0.788 | 0.012 |
| 2018 | 116,307 | 575 | 0.49% | 0.63% | 0.78 | 0.38% | 1.29 | 0.811 | 0.016 |
| 2019 | 115,753 | 388 | 0.34% | 0.58% | 0.58 | 0.35% | 0.95 | 0.794 | 0.035 |
| 2020 | 114,434 | 290 | 0.25% | 0.57% | 0.44 | 0.35% | 0.73 | 0.790 | 0.042 |
| 2021 | 112,165 | 326 | 0.29% | 0.53% | 0.55 | 0.32% | 0.90 | 0.811 | 0.068 |
| 2022 | 111,664 | 269 | 0.24% | 0.50% | 0.48 | 0.30% | 0.79 | 0.799 | 0.092 |
| 2023 | 111,388 | 300 | 0.27% | 0.50% | 0.54 | 0.30% | 0.89 | 0.819 | 0.102 |
| 2024 | 112,229 | 286 | 0.25% | 0.52% | 0.49 | 0.31% | 0.81 | 0.824 | 0.091 |
| 2025 | 113,062 | 274 | 0.24% | 0.51% | 0.47 | 0.31% | 0.77 | 0.834 | 0.094 |

Through the cycle, observed over expected sits near 1 for 2011-14, falls through 2015-18 and runs
0.44-0.58 from 2019: Latvian companies now default at about half the rate of the years the model
learned from. The validation level runs 0.73-0.95 from 2019. 2009 and 2010 sit far above both: the
crisis years, scored on the 15.0% and 29.4% of companies that had accounts. Score PSI against the
training years stays under 0.10 every year but 2023, at 0.102, so the population scored today looks
like the one the scorecard was built on; it is the default rate that has moved, not the companies.

### Sensitivities

| Variant | Train AUC | Validate AUC | Test AUC | Test mean PD, TTC / validation |
| --- | --- | --- | --- | --- |
| the model | 0.8008 | 0.8027 | 0.8176 | 0.51% / 0.31% |
| without `accounts_lag` | 0.8001 | 0.8012 | 0.8151 | |
| fitted on 2011-17 only | 0.8028 | 0.8032 | 0.8143 | 0.48% / 0.31% |
| insolvency-only label, same score | 0.7903 | 0.7898 | 0.8009 | |
| test years re-dated the bulk way | | | 0.8182 | |

Dropping 2009-10, whose accounts cover 15.0% and 29.4% of live companies, from the training years
changes the test AUC by −0.003 and lowers the through-the-cycle level a little. Judged against
insolvency alone, 1,043 of the 1,455 test defaults, the same score ranks at 0.801, so the ranking
does not rest on counting legal protection as default.

## Today's book

Every company live on 1 October 2026, scored from its latest accounts no more than two fiscal years
old. Its horizon runs to 1 October 2027, so nothing here is labelled yet.

| | |
| --- | --- |
| Live companies | 141,319 |
| Scored | 115,088 (81.4%), 8,478 of them on the year-before's accounts |
| Mean PD, through the cycle / validation level | 0.50% / 0.30% |
| Expected defaults by 1 October 2027 | 576 / 350 |
| Exposure proxy, total liabilities at the last filing | €53.8bn |
| Liabilities-weighted PD, validation level | 1.45%, which overstates: in 2021-25 it was 1.42% against 0.55% observed |

The book is concentrated: the 100 companies with the largest liabilities hold 34.1% of the exposure
proxy and the 1,000 largest 65.1%, and the scorecard gives them a mean PD of 1.39% and 1.38%,
against a median of 0.15% across the book. The test years say that is not where the defaults are.
Each year's scored companies ranked by total liabilities, 2021-25:

| Liabilities band, within each year | Company-years | Defaults | Observed | PD, TTC | PD, validation | O/E, validation | Share of liabilities |
| --- | --- | --- | --- | --- | --- | --- | --- |
| top 100 | 500 | 1 | 0.20% | 2.56% | 1.56% | 0.13 | 34.0% |
| 101-1,000 | 4,500 | 33 | 0.73% | 2.34% | 1.43% | 0.51 | 29.5% |
| 1,001-10,000 | 45,000 | 364 | 0.81% | 1.61% | 0.98% | 0.82 | 28.1% |
| the rest | 510,508 | 1,057 | 0.21% | 0.40% | 0.24% | 0.86 | 8.3% |
| all | 560,508 | 1,455 | 0.26% | 0.51% | 0.31% | 0.83 | 100% |

Below the thousand largest the scorecard holds: observed over expected is 0.82 and 0.86 in the two
smaller bands against 0.83 for the window, and the 1,001-10,000 band, which defaults at three times
the window's rate, carries three times its PD. In the thousand largest it does not: one default in
500 company-years among the hundred largest at a predicted 1.56%, and 33 in 4,500 among the next 900
at 1.43%. Weighted by liabilities, the test years' PD was 1.42% and the share of liabilities owed by
companies that defaulted within the year 0.55%, so today's 1.45% overstates the same way.

**This is an SME scorecard, and the largest exposures need their own treatment.** It was fitted on
companies whose median company-year has two employees (one since 2021), and in 2009-17 the larger of
them did default more: 1.9-2.8% a year in each band of the 10,000 largest by liabilities against
0.47% below them. So its size bins rise in risk with size and then stop: total assets open their
last bin at €426,626 and employees at nine, each the riskiest bin on a reported value at −15 points,
and a company with €500m of assets scores on total assets exactly as one with €0.5m does. The
largest also hold little cash: in 2021-25 the hundred largest had a median 3.1% of their assets in
cash against 19.9% across the book, and a current ratio of 1.07 against 1.44, which costs them 20
and 8 points against a company at the book's medians. On today's book their figures are 1.9% and
0.98, and 28% of them report one employee or none. In the training years the hundred largest
defaulted at 1.89%, 17 in 900 company-years; since 2021, once in 500. The PD ranks and prices small
companies; for the few hundred that hold most of the liabilities it is no measure of their risk, and
they need a model or ratings built on large companies before this book's PD is applied to them.

The scores are MERGEd into `lakehouse-dwh/scored_companies` on (regcode, observation date), so next
year's run adds a date beside this one rather than overwriting it, and a re-run on the same date
replaces only the scores that changed.

**Exposure is a proxy, and there is no LGD.** The files carry no loan, no balance at default and no
recovery, so the exposure column is the company's total liabilities at its last filing and expected
loss is not computed. What the insolvency file does say about loss is how an insolvency ended. Of
the 8,635 company insolvencies started since the Insolvency Law of 1 November 2010, 7,702 have
ended in one of two ways: a plan to cover the creditors' claims completed (M09, 4,030, a median of
530 days), or a proposal to end the case for lack of the debtor's property (M08, 3,672, a median of
156 days). The M08 share of the two fell from 52.7-59.3% for the cases started in 2011-15 to
16.1-20.0% for those started in 2021-24. The faster M08 cases are the likelier to have ended
already, so the recent shares lean high, if anything: fewer insolvencies now end with nothing to
distribute.

## The pipeline

What changed from the reference, and why:

1. **Glue 5.0, natively.** The reference pins Glue 3.0 and hands `spark-submit` a `delta-core`
   1.0.0 jar. Glue 5.0 runs Delta Lake 3.3.0 through `--datalake-formats delta`, and its local
   image carries the jar, so nothing is downloaded or pinned by hand; the local pip pins are the
   image's own: Spark 3.5.4, Delta Lake 3.3.0, Python 3.11, pandas 2.2.2, numpy 2.2.6, pyarrow
   17.0.0. Glue 5.1, AWS's default, runs Spark 3.5.6 and Delta Lake 3.3.2, and AWS documents no
   local image for it, so the jobs target 5.0, the newest runtime a Docker run can match exactly.
2. **Explicit schemas, a header checked by name, `FAILFAST`.** Inference costs a pass over the file
   before the read the job wants — 1.3 s against none on the 12 MB statements batch of 2018-09-30 —
   and types each column from whatever that batch holds. In the sample's 2009-09-30 proceedings cut
   to the 50 still open, `proceeding_ended_on` infers as a string and both identifiers as numbers;
   Delta casts them back to the table's types on the MERGE, so that batch commits. Make one start
   date impossible, 2009-02-30, and the inferred batch fails only inside the MERGE's write, as a
   cast error 12.9 s in. With the declared schema the same file is refused at the read, `Text
   '2009-02-30' could not be parsed`, before any MERGE work. The header must also match the schema
   by name (`enforceSchema=False`): a register batch with `registered` and `terminated` swapped in
   its header stopped the job with `CSV header does not conform to the schema`, after the two
   batches before it had committed and with the bad one left in `raw_zone/`. And `FAILFAST` raises
   on a malformed row instead of nulling it.
3. **A MERGE that updates only a newer, changed row.** The reference's bare `whenMatchedUpdateAll`
   lets an older extract, delivered late, overwrite a newer one, and counts a re-delivered row as
   an update. Here a matched row is updated only when it ranks after the stored one — on its own
   state first (a terminated company after a live one, a closed proceeding after an open one, a
   statement by period end, filing time and id), and for a company or a proceeding the batch date
   only to break ties — and differs from it in a value. A stale extract under a new date changes
   nothing, and `numTargetRowsUpdated` is the number of restatements.
4. **One row per key per batch.** A company can file a refiling or a short year in the same quarter
   as the statement it replaces, so each batch is cut to its winner by the same ranking before the
   MERGE. Delta alone catches only half of this: it refuses two source rows that match one stored
   row — on a copy of the full table, a repeated stored key was refused with `Cannot perform Merge
   as multiple source rows matched and attempted to modify the same target row` — but it inserts a
   new key twice. The 2015-09-30 batch has 93 such keys: MERGEd bare, it committed and left the
   table holding 93 keys twice; through the job it inserted 104,584 rows, restated 18 and held no
   key twice.
5. **The batch date travels in the commit**, and log retention is lifted from Delta's 30 days to a
   hundred years, or the early versions would stop being readable a month after the replay.
6. **Archive last, and raise.** Files move to `archived/` only once their MERGE has committed, and
   a failed move raises. The reference swallowed it, so a batch could be merged and left in
   `raw_zone/` with nothing saying so. A re-run is safe: the batch MERGEs as a no-op and moves. On
   the sample, a file put where `archived/` should be made the move fail after the 2012-09-30 batch
   had committed as version 4: the job raised `NotADirectoryError` and the batch stayed in
   `raw_zone/`. The re-run committed version 5 — 141 rows, 0 inserted, 0 restated, 141 unchanged —
   and archived it.

Statements are partitioned by fiscal year, and the MERGE names the batch's fiscal years in its join
condition. Delta can then skip every other partition when it looks for matches, and either way it
rewrites only the files where it finds one. On a copy of the full table at the version before the
2025-09-30 batch — 165 files in 20 partitions — the batch MERGEd alike with the list and without
it, two runs each: scanning took 2.3-2.4 s with it and 2.2 s without, and the MERGE removed 2 files
and added 12 every time. At this size a full scan costs nothing worth saving; the list is there for
a table that grows by a partition a year.

A statement batch is three files — the statements, their balance sheets, their income statements —
and the job refuses a batch with one missing rather than merge statements with no figures. With one
income-statement file deleted from a staged 2011-09-30 batch, the job raised `FileNotFoundError`
naming it before its first MERGE: all 21 staged files stayed in `raw_zone/`, none was archived and
no table was written. A file in `raw_zone/` without a batch name stops the job too, rather than
being skipped.

## Running it

Python 3.11, a Java 17 JDK on `JAVA_HOME` and `pip install -r requirements.txt`: what Glue 5.0
runs. The first Spark start fetches the Delta Lake jar from Maven Central and caches it. The 32
tests take about 70 s:

```bash
python -m pytest tests -q
```

The chain on the committed sample, one batch a year, in about six and a half minutes:

```bash
python local-development/run_chain.py --data sample --cadence year
```

The full files, quarterly, in about 26 minutes:

```bash
python local-development/fetch_raw_data.py
python local-development/run_chain.py --data raw
```

Each step on its own, which is what `run_chain.py` runs:

```bash
python local-development/stage_batches.py --local --data raw --clean
python glue-jobs/glue-delta-merge.py --local --table companies
python glue-jobs/glue-delta-merge.py --local --table proceedings
python glue-jobs/glue-delta-merge.py --local --table statements
python glue-jobs/glue-credit-model.py --local
```

And the chain on the sample inside AWS's Glue 5.0 image, `public.ecr.aws/glue/aws-glue-libs:5`
(Spark 3.5.4-amzn-0, Delta Lake 3.3.0, Python 3.11, Java 17), with nothing installed but Docker:

```bash
./local-docker-development.sh
```

`--local` maps the bucket onto `_localrun/bucket/`. The walkthrough notebook reads what the full
run leaves there.

## What the local run covers

**The jobs you deploy, run as written.** `glue-delta-merge.py` and `glue-credit-model.py` are the
Glue entrypoints themselves; `--local` changes two things only: the bucket is a folder, and Spark's
master is `local[*]`. The full files went through them on a four-core machine: 73 quarterly
batches, 218 commits across the three tables, then the model and the MERGE into `scored_companies`,
in 26 minutes (stage 87 s, companies 310 s, proceedings 263 s, statements 505 s, model 397 s). The
model job's Python driver peaked at 8.2 GB resident and its JVM at 3.6 GB.

**The image you deploy to.** `local-docker-development.sh` ran the chain on the sample inside
`public.ecr.aws/glue/aws-glue-libs:5` in 500 s, with the image's own Spark 3.5.4-amzn-0 and Delta
Lake 3.3.0-amzn-0: 20 yearly batches, 19 of them with statements, then the model, which came out as
the local yearly run's, AUC 0.787, 0.750 and 0.746 on the three windows.

**32 tests, no AWS and no mocks.** `test_label.py` (7), on twelve invented companies with each
answer worked by hand: the population without the terminated, the not yet registered and the already
defaulted, every label, the insolvency-only label, a dismissal that counts only once it is known,
the labelled years stopping at the as-of date, the embargo, and the 24-month accounts window.
`test_replay.py` (6): a company restated on termination, a proceeding restated when it ends, the
last batch dated by the newest stamp, a statement's balance sheet and income statement travelling
with it, the restatement share, and the yearly cadence cutting on the eve of every observation date.
`test_scorecard.py` (8): WoE and IV by hand, special bins kept apart, an AUC tie counted as a half,
DeLong against a stratified bootstrap, KS and PSI, the numpy logit and its clustered errors against
statsmodels, the validation shift hitting its target without moving the ranking, and the liabilities
bands ranked within each year with observed set against predicted. `test_delta.py` (11), against
real Delta tables in a temporary folder: a proceeding restated by a MERGE, a stale batch replayed
late that changes nothing, an as-of view a later batch cannot change, lats and thousands converted
and group accounts dropped, Spark's availability date equal to the replay's, two statements for one
year in one batch cut to the winner, the bare MERGE that refuses them, a statement batch missing a
file left in `raw_zone/`, a file without a batch name stopping the job, bucket keys with forward
slashes on every OS, and a re-run of one date's scores replacing only the ones that changed.

**The failure paths, run.** Each in a scratch bucket, on the sample unless it says otherwise: a
statement batch with its income-statement file deleted, refused before the first MERGE; a register
batch with two columns swapped in its header, refused at that batch after two commits; an archive
move made to fail after its commit, then the re-run that MERGEd as a no-op and archived; an
all-open proceedings batch read by inference, committed through Delta's casts, and the same batch
with an impossible date, failed inside the MERGE's write where the declared read refuses it up
front. On copies of the full statements table: the 2015-09-30 batch MERGEd bare, through the job,
and with a stored key repeated, and the 2025-09-30 batch with and without the partition list. *The
pipeline* gives what each one did.

**Three engine differences to carry into the deploy.** *A local move is one atomic rename; an S3
move is a copy and then a delete.* A crash between the two leaves the batch in `raw_zone/` and in
`archived/`; the next run finds it, MERGEs it as a no-op and copies it onto the same archive key,
so the move is idempotent rather than atomic. *Delta Lake 3.3 commits to S3 through its
single-driver log store*, which keeps concurrent writers apart only inside one driver, so two jobs
writing one table at once can lose a commit. Run the three merge jobs in parallel if you like, but
never two runs of one table, and give the model job's `scored_companies` one writer too. *The image
is Glue 5.0, not 5.1*: Glue 5.1 is AWS's default and runs Spark 3.5.6 and Delta Lake 3.3.2, and AWS
documents no local image for it, so the jobs target 5.0, the runtime the Docker run matches
exactly.

## Deploying it

1. Create a bucket. Upload `glue-jobs/glue-delta-merge.py`, `glue-jobs/glue-credit-model.py` and
   `glue-jobs/lakehouse.py`, and a zip of the `company_credit/` package (`python -m zipfile -c
   company_credit.zip company_credit/`).
2. Create two Glue 5.0 Spark jobs from the two scripts, each with the job parameters
   `--datalake-formats delta`, `--extra-py-files
   s3://<bucket>/<prefix>/lakehouse.py,s3://<bucket>/<prefix>/company_credit.zip` and `--bucket
   <bucket>`. The merge job also takes `--table companies`, `proceedings` or `statements`: create
   it once and pass the table per run, or create three. Both jobs set the Delta extension, the
   Delta catalog, UTC and the hundred-year log retention on their own session. The model job holds
   the company-year rows on its driver, and on the full files its Python process peaked at 8.2 GB
   resident beside a 3.6 GB JVM: a G.2X driver's 32 GB holds that with room.
3. The jobs' role needs `s3:ListBucket` on the bucket, `s3:GetObject`, `s3:PutObject` and
   `s3:DeleteObject` on its `raw_zone/`, `lakehouse-dwh/`, `archived/` and `results/` prefixes, and
   what any Glue job needs to read its scripts and write its logs.
4. Stage the batches: `python local-development/stage_batches.py --bucket <bucket> --data raw`,
   with boto3 and credentials for the bucket on the machine that runs it.
5. Run the merge job for each table, then the model job. The three merges are independent and may
   run at the same time; one table takes one writer at a time.
6. From then on, each new extract lands as `raw_zone/<source>/<source>_<YYYY-MM-DD>.csv`, three
   files for a statement batch, and the same two jobs run again: the merges take every waiting
   batch oldest first, and the model reads the new versions.

Every AWS name in these files is an account-specific placeholder the deployer supplies.

## What the data cannot say

- **The replay rebuilds only what today's files still show.** A resolution code corrected, a
  statement replaced in place, a company reregistered: none left a trace of its earlier value, so
  none can be replayed, and the restatement shares are lower bounds.
- **Before 2021, when accounts were filed is not in the file.** The nine-month rule stands in for
  it in 2009-20, and the test years measure what that costs.
- **The largest companies are outside what the scorecard learned.** It was fitted on micro and
  small companies, and on the thousand largest by liabilities it overstated the 2021-25 PD two- to
  eightfold, so their PD needs a model of its own.
- **There is no LGD and no exposure.** No recovery amount exists anywhere in these files; total
  liabilities at the last filing is a proxy for what creditors are owed, not for any one lender's
  exposure.
- **A company struck off is not a default.** A dormant company the registrar removes without
  insolvency proceedings leaves the population as a non-default, though some of them owed money
  they never paid.
- **Accounts cover few companies before about FY2013**: 15.0% and 29.4% of live companies in 2009
  and 2010.
- **Insolvency applications are recorded only to 2012.** M01, a dismissed insolvency application,
  occurs for applications made in 2008-12 and never after; since 2013 an insolvency is in the file
  once it is declared, so an application refused since then left no row to exclude. Dismissed
  legal-protection applications (T18) run through 2026.
