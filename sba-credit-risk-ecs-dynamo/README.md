# sba-credit-risk-ecs-dynamo

Credit risk on company lending, end to end: PD, a loss measure, exposure and expected loss on
**1,639,728 SBA 7(a) small-business loans** approved FY2000 to 2026-09-30, with bank and
counterparty risk from **every FDIC failure since 1984** beside it. Two ECS Fargate containers,
a Step Functions gate between them, DynamoDB at the end, and a `--local` mode that runs the same
containers on a laptop with no AWS account.

The point of the project is honesty about what the data can identify:

> **The file records charge-off, not default, and charged-off amount, not LGD. So the chain models
> exactly those, names them that way, and still gets to a 12-month PD, a lifetime PD, an IFRS 9
> stage and IRB capital for every open loan.**

Two chains, never multiplied together:

- **SBA is the loan book.** A WoE scorecard on origination fields ranks loans within a vintage; a
  monthly charge-off hazard by loan age turns its bands into a 12-month PD for Basel and a lifetime
  curve for IFRS 9; a fractional logit on the origination bins and the loan's age at charge-off
  predicts gross charge-off severity. EL, stage and capital follow for each of the 281,077 open
  loans.
- **FDIC is bank and counterparty risk.** Failure in the next 12 months from filing ratios, out of
  time; the insurer loss rate, judged first out of time; the deposit run-off ratio, as a table; and
  a dollar EL backtest against what the fund actually lost.

An SBA borrower charging off and its lender failing are different events on different
populations. A product of the two chains would describe neither, so there is none.

## Architecture

```
 host                     S3 landing bucket                      ECS Fargate
 fetch_raw_data.py   ->   new/sba_loans/ (3 CSVs)
 stage_landing.py         new/fdic_failures/                     data-validity
                          new/fdic_financials/ (2 CSVs)    ┌──>  dtype dict per entity
                          new/fdic_institutions/           │     new/<entity>/ -> <entity>/
                          new/sba_recovery_rates/          │     {valid, expected, gate_passed}
                          new/_READY   (written last)      │            │  SendTaskSuccess
                                │                          │            ▼
                                ▼                          │     Choice: gate_passed?
                    EventBridge rule: key = new/_READY     │       │ no           │ yes
                                │                          │       ▼              ▼
                                ▼                          │     Fail state     credit-model
                    Step Functions ─── runTask ────────────┘     (the cause     SBA + FDIC chains
                    .waitForTaskToken, then .sync                 names the      results/<chain>/*.csv
                                                                  short entity)  archived/<entity>/
                                                                                     │
                                                                                     ▼
                                                                  DynamoDB sba_credit_risk_results
                                                                  chain (PK) · item (SK) · Decimal
```

The reference lab's shape, with five changes, each argued below under *The pipeline*: one config
list drives every entity, an expected file count per entity, a gate that fails into a Fail state,
nullable integers declared `float64`, and the validator's answer carried by a task token.

## Status

| File | State |
| --- | --- |
| `local-development/fetch_raw_data.py` | **done** — SBA FOIA files, FDIC tables, recovery report; byte-checked, sha256 manifest |
| `local-development/parse_sba_recovery.py` | **done** — SBA's Tables 10 and 11 into one tidy CSV, self-checking against each row's printed total |
| `credit_risk/config.py` | **done** — the one entity list: folders, file counts, dtypes, the gate |
| `credit_risk/validation.py` | **done** — exact dtypes with one widening; promotes valid files; the gate |
| `credit_risk/sba.py` · `hazard.py` · `woe.py` · `capital.py` | **done** — the SBA chain |
| `credit_risk/fdic.py` | **done** — the FDIC chain |
| `credit_risk/metrics.py` · `results.py` · `pipeline.py` · `stores.py` | **done** — AUC/Gini/KS/PSI, Decimal items, the model run, the only AWS-aware module |
| `docker-data-validity/` · `docker-credit-model/` | **done** — the two entrypoints on `python:3.12-slim`, pinned to the releases the notebook ran on |
| `step-functions/step-function.json` · `event-pattern.json` | **done** — seven states, no Pass; walked locally by `run_pipeline.py` |
| `local-development/stage_landing.py` · `run_pipeline.py` · `build_sample.py` | **done** — the upload, the local state machine, the committed sample |
| `credit-risk-walkthrough.ipynb` | **done** — executed on the full files, every figure below |
| `tests/` | **done** — 41 tests, no AWS, no network, no mocks |

## The data

| Source | Rows | As of |
| --- | --- | --- |
| SBA 7(a) FOIA, `FOIA_7a_FY2000_FY2009_asof_260930.csv` | 690,333 | 2026-09-30 |
| SBA 7(a) FOIA, `FOIA_7a_FY2010_FY2019_asof_260930.csv` | 545,751 | 2026-09-30 |
| SBA 7(a) FOIA, `FOIA_7a_FY2020_Present_asof_260930.csv` | 403,644 | 2026-09-30 |
| FDIC BankFind `failures` | 4,117 (3,524 FAILURE, 593 ASSISTANCE) | retrieved 2026-11-01 |
| FDIC BankFind `institutions` | 27,835 | retrieved 2026-11-01 |
| FDIC BankFind `financials`, every quarterly filing of every failed bank | 89,836 for 2,839 banks | retrieved 2026-11-01 |
| FDIC BankFind `financials`, every bank at every year-end 1984-2025 | 413,302 (8,395 at 2008-12-31) | retrieved 2026-11-01 |
| SBA Loan Program Performance report, Tables 10 and 11 → `data/sba_recovery_rates.csv` | 872 | FY2025 Q3 |

The three SBA files share one 42-column layout. Together they are 763 MB, and each is over
GitHub's 100 MB limit, so `data/raw/` is gitignored and `fetch_raw_data.py` downloads it in about
15 minutes. **The borrower's name, street, city and ZIP are never read**: every loader drops them
by `usecols` before a row is in memory, because some 7(a) borrowers are sole proprietors.

**`data/sample/` is committed** so the pipeline, both containers and the tests run straight after
a clone. It is stratified, not the head of the file: 0.6% of every (fiscal year, loan status)
cell of each SBA file with a floor of three, and on the FDIC side 35 failed banks per failure era,
10 assisted banks, the three 2023 failures and 400 banks that never failed, every table cut to
those banks whole. Its source line sits beside it in `data/sample/fdic/SOURCE.md`. It is small
enough that its scorecard ranks at chance, which is the right outcome for 1,939 training loans
and 55 coefficients; **every figure in this README is from the full files.**

## The SBA chain

### What "default" can mean in this file

The FOIA extract records one credit event: SBA **charge-off**. It comes after the lender has
liquidated, which is months or years after the 90 days past due Basel calls default, and a default
that recovers in full never becomes a charge-off at all. There is no default date either, only the
charge-off date. So:

- the scorecard's target is **P(charge-off by the as-of date)**, a whole-life probability, and it
  is validated **within vintage**;
- a 12-month figure comes from the hazard curve, and it is a 12-month probability of *charge-off*;
- **EXEMPT is censored, never good.** SBA withholds the status of every open loan, performing and
  delinquent alike. FY2020-Present is 61% EXEMPT, so it is never used as an out-of-time PD test.

The company book is corporations and partnerships: 1,199,622 loans, of which 774,722 paid in
full, 143,817 charged off and 281,083 are open. Individuals are dropped, and so are cancelled
(CANCLD) and undisbursed (COMMIT) loans. The file has no loan identifier — `LocationID` is the
lender's location — so `loan_id` is minted from the file's fiscal-year span and the row number.

### The fields that leak: `TermInMonths` and `CollateralInd`

A term is set at approval, so it looks like the safest origination field in the file. **It is
rewritten at charge-off.** Of paid-in-full company loans, 83.4% carry a whole-year term; of
charged-off ones, 8.9%. Loans at exactly 120 or 240 months charge off at about 0.5%; loans at
85-119 or 181-239 months, at about 31%. Given to the scorecard as a length band and a whole-year
flag, it lifts the holdout AUC from 0.623 to 0.915, every single vintage from 0.560-0.639 to
0.904-0.930 and FY2010 from 0.601 to 0.920, and every point of that is the outcome.

The same rewrite rules out the amortisation-schedule test as a way of splitting LGD from EAD: any
scheduled balance computed from that term is computed from a field the charge-off process has
already touched. The interest rate is out for a plainer reason — it is present for 0% of FY2000-04
approvals and 100% from FY2010, so it cannot rank loans across the crisis vintages. Bank name and
address are the loan's *current* assignee, and the secondary-market flag is set after sale.

**The collateral flag is attached on the way to charge-off.** For the FY2000-04 approvals it is
not a fact about the loan at approval. Of the resolved FY2000-03 company loans flagged as
collateralised, 93.0% charged off, against 11.3% of the unflagged ones, and in each of those four
vintages the flagged rate sits at 91.5-93.4% while the unflagged one runs 9.4-13.4%. Of the 83,563
FY2000-03 loans paid off by 2008, 12 carry the flag. The gap closes through FY2004 (59.8% against
16.4%) and FY2005 (30.8% against 22.6%), and from FY2006 the flag is flat or protective (FY2009:
11.7% against 17.9%). A field that leaked for five vintages cannot be trusted for the ones after
it, so it is out of the card and the severity model for every vintage. `TermInMonths` is out of
both too, and the loader reads neither.

### The scorecard

The course's WoE scorecard, on the resolved FY2000-2009 vintages (1.3% or less of any of them
still EXEMPT), with a stratified 30% of every (vintage, outcome) cell held out. Ten
characteristics, each binned, WoE-ordered and turned into dummies with its riskiest bin as the
reference: amount, guarantee share, NAICS sector, project state (coarse-classed into six WoE
groups), processing method, business age (SBA's FY2018 recoding folded onto four groups),
jobs, franchise, term or revolver, and business type. The approval vintage enters as a
**level term that is fitted and then left out of the points**: it absorbs the 13%-to-35% swing in
charge-off rates across the decade, and the hazard calibration below supplies the level. p-values
are clustered on the lender location. Points are the course's 300-850 min-max scaling.

| | n | charge-offs | AUC [95% CI] | Gini | KS |
| --- | --- | --- | --- | --- | --- |
| Holdout, FY2000-2009 | 136,807 | 31,672 | **0.623** [0.619, 0.626] | 0.246 | 0.174 |
| Holdout, single vintages | 8,086 - 19,994 | 1,224 - 7,104 | 0.560 - 0.639 | 0.120 - 0.279 | 0.085 - 0.198 |
| FY2010, the next vintage | 33,631 | 3,042 | **0.601** [0.591, 0.613] | 0.202 | 0.144 |

**It ranks within every vintage, modestly.** Origination fields carry a Gini of about 0.25, and
the strongest of them is geography: project state has the highest IV (0.083), then processing
method (0.069) and sector (0.049). The card carries to the next vintage at a lower Gini.

**The open book scores like the development sample.** Score PSI against the development sample is
under 0.10 for every vintage except FY2010, FY2011, FY2013-2016 and FY2021, which run 0.10-0.19;
none reaches the 0.25 line. FY2022-2026, 72% of the open loans, sit at 0.016-0.028, so the ranking
below is applied to a population that scores like the one the card was fitted on. A PSI past 0.25
on the open vintages would be the trigger for a refit. A rare bin of an ordered characteristic
folds into its nearest neighbour, so a loan above $2m — which the FY2000-09 programme barely made,
the 7(a) cap being $2m until the Small Business Jobs Act of 2010 — scores as the largest amount the
card has seen.

### The hazard: 12-month PD for Basel, the lifetime curve for IFRS 9

Every company loan with a disbursement date enters a life table at age 0, or at its age on
2005-08-01 if it was already open then (below), and leaves by charge-off (the event), by payoff
(the competing exit) or on the as-of date (censored). **The dates are labels only; none is a
feature.** Payoff competes rather than censors: a repaid loan cannot charge off, and treating it
as censored would assume it went on carrying the hazard of the loans that did not repay. From
origination, the book's lifetime charge-off PD is **15.8%** with payoff competing and 49.1% with it
censored.

**The table starts on 2005-08-01.** No paid-in-full date in the file is earlier than 2005-05-31.
34,187 company payoffs carry that exact date and 6,671 more carry 2005-07-31, against about 2,000
in an ordinary month, all of them FY2000-05 approvals. They read as placeholders, not the days
loans were repaid, and left in they would keep loans at risk for years after they had repaid and
thin every early hazard. So a loan still open on 2005-08-01 enters the table at its age that day,
and every exit before it is dropped, charge-offs with the payoffs, or the early vintages would keep
their failures and lose their repayments. That removes 46,277 loans, 42,349 payoffs and 3,928
charge-offs, of which 45,713 are FY2000-04 approvals, a quarter of those vintages. The curve, the
band multipliers and each early vintage's observed against expected count experience since August
2005 as a result.

Each score band is calibrated to the curve by observed over expected charge-offs across its loans'
months at risk — a proportional-hazards assumption, which the notebook checks by overlaying each
band's own life table on its scaled curve.

| Band | Loans | Charge-off multiplier | 12-month PD at origination | Lifetime PD at origination |
| --- | --- | --- | --- | --- |
| A (highest score) | 195,080 | 0.52 | 0.08% | 8.4% |
| D | 137,165 | 0.98 | 0.15% | 15.5% |
| H (lowest score) | 106,068 | 1.84 | 0.28% | 28.4% |

The 12-month PD at origination is tiny because charge-off lags default: the median company loan
that charges off does so 49.8 months after disbursement. A loan's Basel PD is the 12-month
incidence **at its current age**, which for the open book averages 2.86% by exposure.

The curve stops at 277 months, where fewer than 500 loans are at risk, rather than extrapolate
a hazard estimated from almost nobody. The 231 open loans already older, $118m of exposure, carry
a PD of zero, and 140 more, $80m, have a 12-month window cut short at 277 months. Holding the last
two years' hazard flat beyond the cut instead would add $0.9m to the 12-month EL.

### IFRS 9 staging

Stage 2 needs a significant increase in credit risk *since origination*, and for one open loan the
file shows nothing that has changed since approval — no days past due, no modification, no
rating. What does arrive afterwards is the experience of the loan's own cohort. So the staging is
collective (IFRS 9 B5.5.1-B5.5.6): **a vintage whose charge-offs ran at least twice what the
calibrated curve expected, with a one-sided Poisson p-value under 0.01, puts its open loans in
stage 2.** Only FY2007 (2.14x) qualifies; FY2005 (1.45x), FY2006 (1.89x) and FY2008 (1.71x) do
not, and the FY2000-04 ratios count experience since August 2005 only. That is 470 open loans and
$311m of exposure. Stage 3 needs an observed default and the file shows none for an open loan, so
it is empty by construction.

The same table is the case for a point-in-time view. The curve is pooled over 27 vintages and
every calendar year since August 2005, so it is through the cycle — what Basel's long-run PD
wants — and every vintage since FY2010 has run at 0.35-0.76 of it. Scaling each open loan's
remaining hazard by its vintage's credibility-weighted experience would cut the staged ECL from
$3.27bn to $1.91bn. That is reported as a sensitivity, beside the booked figure, not in place of
it.

### Gross charge-off severity

Charged-off amount over original amount: **conditional on charge-off, gross of recoveries**, and
not an LGD. `GrossChargeOffAmount` has no balance at default and no recovery field beside it, so
the file proves only charged-off ≤ balance at default ≤ original. The product is what is
identified, and because severity (charged-off / original) depends on the age a loan charges off
at, as below, it is summed over age bands:

    EL = Σ over age bands  P(charge-off in the band) × severity for that band × original

On 143,789 company charge-offs with a positive amount, 4,482 exceed their original amount — SBA's
report defines the charged-off amount as principal *and interest*, which is the likeliest reason,
though the FOIA field is not documented that way. **The rule is one: cap at 1**, which keeps the
identity's upper bound. Capped, the mean is 0.744 and the median 0.820.

**Severity falls with the age a loan charges off at.** Capped, the mean runs from 0.926 for
charge-offs inside a loan's first year to 0.485 from the 180th month on, and every five-year group
of vintages shows the same slope, so it is the loan's age and not the vintage mix. Across all
company charge-offs with an age:

| Age at charge-off | Charge-offs | Mean severity |
| --- | --- | --- |
| 0-11 months | 1,676 | 0.926 |
| 12-23 | 15,222 | 0.906 |
| 24-35 | 25,208 | 0.860 |
| 36-47 | 26,169 | 0.810 |
| 48-59 | 21,144 | 0.741 |
| 60-71 | 16,012 | 0.669 |
| 72-95 | 20,447 | 0.613 |
| 96-119 | 9,743 | 0.580 |
| 120-179 | 7,265 | 0.519 |
| 180 or more | 822 | 0.485 |

The model carries it as a term beside the scorecard's origination bins. A charge-off's age is known
only once it has happened, so an open loan is priced across all of them: its chance of charging off
in each age band, from the hazard, times that band's severity. The `severity` the book carries is
the 12-month figure that mixture implies, and it fills the IRB LGD slot. 81 charge-offs have no age
and leave the fit only. The fractional logit is fitted on FY2000-09 charge-offs and scored on
FY2010-19 ones, each at its own age, against the training mean:

| Out of time, 30,536 charge-offs | MAE | RMSE | R² vs constant | Spearman |
| --- | --- | --- | --- | --- |
| constant (0.746) | 0.208 | 0.256 | — | — |
| fractional logit | **0.168** | **0.218** | **0.275** | 0.542 |

Without the age term the same model scores MAE 0.190, R² 0.135 and Spearman 0.395, and its dollars
are off by more: predicted over actual charged-off dollars, by age band on the same window, runs
0.76-1.39 without the term and 0.94-1.20 with it. On the open book the term alone raises the
12-month EL by 12.4%, the lifetime EL by 3.0% and RWA by 16%, and cuts the stage-2 ECL by 18%: most
open loans are from FY2022-26, so their next charge-offs come at young ages, which carry the
highest severity.

The rule's cost, on the open book's EL. The fitted model is kept and its level recalibrated to each
rule's mean on the training charge-offs: a fractional logit has no room above 1 to be refitted to
values that run to 464 times the loan, and EL is linear in severity, so the factor is exactly what
the rule changes.

| Severity above 1 | Charge-offs | Mean severity | Level factor | 12-month EL | Lifetime EL |
| --- | --- | --- | --- | --- | --- |
| **cap at 1 (used)** | 143,789 | 0.744 | 1.000 | $3.270bn | $15.61bn |
| drop the row | 139,307 | 0.736 | 0.990 | $3.238bn | $15.46bn |
| keep uncapped | 143,789 | 0.768 | 1.035 | $3.383bn | $16.16bn |

**Recoveries come from SBA's own report and are never added together.** Table 11 is recoveries
after charge-off as a percent of the amount charged off, by charge-off year: 2.68-6.65% for 7(a)
Regular in 2016-22, so a gross charge-off is close to final. Table 10 is total recoveries as a
percent of the amount SBA purchased on its guarantee, by purchase year: 34.3-38.6% for 2016-20,
with 2021 onward still immature, so SBA's loss as guarantor runs at roughly 61-66% of what it
purchases, at cohort level and undiscounted. The denominators differ, so they sit side by side in
`recovery#<year>` items and in the notebook and are never summed.

### The open book

Every EXEMPT company loan with a disbursement date, priced at its age on 2026-09-30:

| | |
| --- | --- |
| Open loans | 281,077 |
| Original exposure | $165.06bn, of which SBA guarantees $123.9bn |
| Exposure-weighted 12-month PD / severity | 2.86% / 0.725 |
| **12-month EL** | **$3.27bn** (1.98% of exposure) |
| Lifetime EL | $15.61bn (9.5%) |
| IFRS 9 ECL, staged | $3.27bn — stage 1 $3.268bn on 280,607 loans, stage 2 $5.0m on 470 |
| IFRS 9 ECL, point-in-time sensitivity | $1.91bn |
| IRB capital K × exposure / RWA | $25.3bn / $316.1bn |

The capital is the Basel corporate IRB formula with each slot filled by what the file has: the
12-month *charge-off* PD in the PD slot (floored at 0.05%), the gross charge-off severity expected
over the next 12 months' charge-offs in the LGD slot, the original amount as the exposure and
M = 2.5 years. RWA comes to 191% of exposure, and two things the formula is not given account for
most of it. **SBA's guarantee is not applied.** It covers 75% of the exposure, and a lender
holding capital on its own share only, with the guaranteed share at the US government's 0%
weight, would carry RWA of 48% of exposure. **The SME firm-size adjustment needs annual sales**
the file does not carry; without it these small firms get the correlation of a large corporate,
and at its fullest it would bring RWA to 146%. So the figure is the capital on the whole loan, as
if nobody guaranteed it.

## The FDIC chain

### The label rule, stated once

A row is a bank's year-end filing. Its 12-month horizon starts one quarter after the filing date,
the embargo, because a December call report is filed well into the next quarter. Only a FAILURE
row is a default, dated by its own FAILDATE; ASSISTANCE is not — Citibank N.A. and Bank of America
N.A. are assistance rows and both are open — and an assisted bank's rows stop at the assistance,
which removes the 37 banks that were assisted and failed later. A horizon that ends after the data
does is dropped even when the bank has already failed inside it, because the banks that will not
fail cannot be labelled yet. Events are deduplicated on CERT, earliest kept: 3 duplicates go, out
of the 42 FSLIC-RTC rows (21 pairs, 3 sharing a CERT). Features come only from filings, never from
the resolution or from `institutions.csv`.

| 413,302 year-end rows | |
| --- | --- |
| kept and labelled | 390,443 |
| censored exit: no filing at the next year-end and no failure (mergers are not in the table) | 15,986 |
| horizon not yet observed (the 2025 year-end, including 3 banks that failed inside it) | 4,403 |
| assisted, from the horizon that reaches the assistance onward | 1,503 |
| failure inside the embargo quarter (the row before holds it) | 749 |
| filed after the bank had failed | 218 |

### PD, out of time

Train on horizons in 1985-2007, validate on 2008-12, test on 2013-25. Equity to assets, ROA, net
interest margin, noncurrent loans, net charge-offs, loans to deposits and uninsured share; every
coefficient's p-value is clustered on the bank and every one is below 0.02. `RBCRWAJ` is out (0 or
above 100 for every bank in 1984-87, and 0 for 35-39% of banks at every year-end from 2020, when
community banks could opt into the leverage ratio instead), and so are total assets (p = 0.91).

| Window | Bank-years | Failures | Full model AUC [95% CI] | Gini | KS | Equity ratio only |
| --- | --- | --- | --- | --- | --- | --- |
| train 1985-2007 | 281,076 | 2,167 | 0.970 [0.967, 0.974] | 0.941 | 0.849 | 0.939 |
| **validate 2008-12** | 38,972 | 466 | **0.961** [0.950, 0.971] | 0.921 | 0.825 | 0.924 [0.910, 0.940] |
| **test 2013-25** | 70,395 | 77 | **0.963** [0.932, 0.985] | 0.925 | 0.854 | 0.951 [0.920, 0.973] |

**Why the equity ratio alone scores 0.92-0.95.** Failure is partly a regulatory decision. Since
FDICIA in 1991, prompt corrective action requires a critically undercapitalised bank — tangible
equity at or below 2% of assets — to be put into receivership within 90 days. Capital is close to
part of the definition of the event, so it is reported as the benchmark, not as a finding; the
full model adds 0.04 of AUC in the crisis and 0.01 since.

**The censoring rule's cost is nil.** Keeping the 15,986 exit rows as non-failures gives 0.962
[0.951, 0.970] on 2008-12 and 0.963 [0.933, 0.985] on 2013-25.

**Calibration moves with the cycle.** In 2008-12 the model expected 260 failures and saw 466; in
2013-25 it expected 98 and saw 77. The score PSI against training is 0.10 for 2008-12 and 0.61
for 2013-25: today's banks are a much healthier population than the one the model was fitted on.

### The insurer loss rate, out of time first

COST is FDIC's estimated loss — disbursed less expected recoveries, revisable, as of 2026-07-31.
The amount disbursed is not in the API, so the rate is COST over deposits at failure: a proxy, on
a deposit snapshot. 2,747 failures since 1984 have COST, deposits and a filing 9-15 months
before failure; their mean rate is 0.253 and the median 0.223. One stage: 1.9% are exactly zero,
too few for a two-stage model to be anything but manufactured. Flagged: 139 rows from 1984-85, 96
FSLIC rows (FDIC's loss data are not comprehensive before 1986), 61 with COST at or below zero,
and the 2023 systemic exceptions — Silicon Valley Bank, Signature (COST 0) and First Republic.

Fitted on 1986-2007 non-FSLIC failures with a pre-1992 term (pre-FDICIA purchase-and-assumption
deals took all deposits), scored on later failures. The era-only model is the 1992-2007 mean,
0.162:

| Out of time | Failures | MAE era-only | MAE era + financials | R² vs era-only | Spearman |
| --- | --- | --- | --- | --- | --- |
| **2008-12** | 465 | 0.139 | **0.122** | **+0.206** | 0.10 |
| **2013-26**, 2023 systemic in | 84 | 0.107 | 0.105 | **−0.108** | 0.17 |
| 2013-26, 2023 systemic out | 81 | 0.108 | 0.106 | −0.118 | 0.14 |

**The financials help in the crisis and not since.** In 2008-12 they lower the error by an eighth,
mostly by moving the level with the banks' condition rather than by ranking (Spearman 0.10); on
the 84 failures since, they lower the absolute error a little and raise the squared error. Pooled
10-fold cross-validation says era terms explain 0.028 of the variance and era plus financials
0.274, which is the in-sample view and is reported second for that reason.

**Dollar backtest, given failure** (FDIC dollars; the API reports thousands):

| Window | Failures | Booked COST | Predicted | Count-weighted rate, actual / predicted | Deposit-weighted rate, actual / predicted |
| --- | --- | --- | --- | --- | --- |
| 2008-12 | 465 | $68.0bn | $100.9bn | 0.265 / 0.219 | 0.138 / 0.205 |
| 2013-26, systemic in | 84 | $38.8bn | $58.9bn | 0.208 / 0.187 | 0.084 / 0.127 |
| 2013-26, systemic out | 81 | $4.57bn | $4.99bn | 0.213 / 0.190 | 0.194 / 0.211 |

Count-weighted the model is low; deposit-weighted it is high. Large failures cost the fund less per
dollar of deposits than small ones, and the model, fitted on a failure population dominated by
small banks, does not know that.

### The deposit run-off ratio: a table, not a model

Deposits at failure over deposits at the filing nearest 12 months before, 9 to 15 months back.
2,769 of the 2,860 failures since 1984 have one. **Not clipped at 1**: 24.7% of failed banks had
more deposits at failure than a year earlier.

| Era | Failures | Mean | Median | p10 | p90 | Above 1 |
| --- | --- | --- | --- | --- | --- | --- |
| 1984-91 | 1,919 | 0.947 | 0.937 | 0.794 | 1.095 | 25.3% |
| 1992-2007 | 301 | 0.904 | 0.887 | 0.732 | 1.042 | 18.3% |
| 2008-12 | 465 | 0.985 | 0.933 | 0.792 | 1.126 | 27.7% |
| 2013-26 | 84 | 0.895 | 0.887 | 0.768 | 1.059 | 17.9% |
| **all** | 2,769 | 0.947 | **0.931** | 0.781 | 1.097 | 24.7% |

A regression on the same filing ratios, against a constant:

| | Failures | MAE model | MAE median | R² vs mean |
| --- | --- | --- | --- | --- |
| 10-fold CV, 1984-2026 | 2,769 | 0.1046 | 0.1075 | 0.067 |
| fit 1984-2007, test 2008-26 | 549 | 0.1189 | 0.1187 | 0.017 |

Three percent off the error in sample and nothing out of time. The container carries the table and
the chain uses the 1984-2007 mean, 0.941; the comparison lives in the notebook.

### The chain end to end, and today's banks

PD × loss rate × year-end deposits × 0.941, summed over every bank-year in a window, against the
COST booked on the failures inside it. Both models were fitted on horizons that end by 2008:

| Window | Expected failures | Actual | Expected loss | Booked COST | EL / actual |
| --- | --- | --- | --- | --- | --- |
| train 1985-2007 | 2,167 | 2,167 | $147.1bn | $123.6bn | 1.19 |
| 2008-12 | 260 | 466 | $34.1bn | $68.1bn | **0.50** |
| 2013-25, systemic in | 97.8 | 77 | $17.2bn | $38.6bn | 0.45 |
| 2013-25, systemic out | 97.8 | 74 | $17.0bn | $4.34bn | 3.93 |

The crisis is under-predicted by half, through the PD. Since 2013 the model's expected loss sits
between the two outcomes: four times the losses of the ordinary failures, and less than half once
the three 2023 failures are in. Most of that expected loss is a tiny PD on a very large deposit
base; the 2023 cluster is what such a tail looks like when it arrives.

Refitted on every labelled row and scored on the 2025 year-end filings: **4,393 banks, 4.7
expected failures in the next 12 months, $1.44bn of expected loss to the fund**, on $20.08tn of
deposits. The forecast is made as of the filing, so every bank still open when its horizon
begins, after the embargo quarter, is in.
An assistance row ends a bank's training rows but not its place here: Bank of America N.A. and
Citibank N.A. are scored, and rank third and fifth by expected loss ($105m and $68m), behind
JPMorgan Chase and Nano Banc ($122m, PD 0.42). A bank that failed after the embargo quarter is
scored too, since that failure is what the PD is for. A bank that failed inside it, Metropolitan
Capital Bank & Trust on 2026-01-30, is not: no horizon of the label rule counts that failure. None
of the top 25 has failed since the filing. 17 filers with equity of exactly zero are left out — 15
branches of foreign banks, which hold no capital of their own, and 2 CERTs with no institutions
record — rather than scored as the riskiest banks in the country. Negative equity is kept: that is
a domestic bank that is insolvent, which is what the model is for.

## The pipeline

**One list.** `credit_risk/config.py` holds `ENTITIES`: name, role (loan book, header, child,
dimension), expected file count, source pattern and dtype dict. The validator, the gate, the
staging script and the model container all walk it. The state machine names no entity; its
Choice reads one boolean computed from the list, so adding an entity is one entry in one file.

**Expected file counts.** SBA ships three CSVs and the FDIC financials are two grains of one child
table, so the gate requires each entity's ready folder to hold **exactly** its count: a missing
FY2020 file is `sba_loans 2/3`, and a stray fourth copy is `sba_loans 4/3`. Counting the ready
folder rather than this run's arrivals means a re-upload of one fixed file passes.

**A failed gate fails loudly.** The Choice's default is a Fail state whose cause is the
validator's summary, and any invalid file fails the gate even when the counts are met. A short
batch is a red execution with `ValidationGateFailed: sba_loans 2/3, ...; 1 invalid:
new/fdic_financials/bad.csv` on it, never a green Pass.

**Nullable integers are `float64`.** pandas cannot hold a blank in an `int64` column, so an integer
column with a blank infers `float64`, and the exact check would reject an `int64` declaration.
The converse also happens file by file — `LocationID` has blanks in two SBA files and none in the
third — so a `float64` declaration accepts `int64`, the one widening allowed. Codes that look like
numbers (`BankZip`, `FranchiseCode`) are read as strings so a leading zero survives.

**The validator's answer comes back through a task token.** `ecs:runTask.sync` returns the ECS
task description — ARNs, exit codes, timings — and nothing the container computed, so the
validator runs as `runTask.waitForTaskToken` with `$$.Task.Token` in its environment and answers
with `SendTaskSuccess`, or `SendTaskFailure` carrying the traceback. A validator that dies before
it can answer, an out-of-memory kill or an image that never starts, sends nothing, and the state
fails only at its one-hour timeout. The model container returns nothing the next state needs, so
it is `.sync`, and its exit code is its answer.

**One marker per batch.** The EventBridge rule matches the key `new/_READY`, which
`stage_landing.py` writes after every data file. Firing on every arrival would start eight
executions per batch and fail seven of them, and a red execution should mean something.

**Results, then archive.** The model container writes `results/<chain>/*.csv` and 153 DynamoDB
items, and only then moves the batch to `archived/<entity>/`, so a crash leaves the batch where a
re-run will find it. It moves the files it read and nothing else, each only if it still holds
what was read (on S3, a copy conditioned on the ETag), so a file that a second execution promotes
while the model runs is left for the next run rather than archived unread. Run one batch at a
time all the same: write the next `_READY` once the last execution has finished. Items are
`chain` (PK) and `item` (SK) — `book#open`, `scorecard#vintage#2006`, `band#H`, `vintage#2007`,
`pd#test#full`, `loss#out_of_time#test#excluded`, `el_backtest#…`, `counterparty#rank#01` — with
every number a `Decimal` built through `str`, and NaN left out.

**The model logic has no boto3 in it.** `credit_risk/stores.py` is the only module that knows AWS
exists, and it imports boto3 inside its AWS classes. Everything else takes a store and a table.
A port to another cloud replaces that file and the two entrypoints.

## Running it

Python 3.12+ and `pip install -r requirements.txt`. No AWS account, and Docker only if you want
the containers.

```bash
python -m pytest tests -q
```

The pipeline on the committed sample, the state machine walked step by step, in 10-17 s:

```bash
python local-development/stage_landing.py --local --data sample --clean
python local-development/run_pipeline.py --local
```

The full files, once `fetch_raw_data.py` has filled `data/raw/`:

```bash
python local-development/fetch_raw_data.py
python local-development/stage_landing.py --local --data raw --clean
python local-development/run_pipeline.py --local
```

The same two runs through the two images, built from the project root:

```bash
docker build -f docker-data-validity/Dockerfile -t sba-data-validity .
docker build -f docker-credit-model/Dockerfile -t sba-credit-model .
python local-development/run_pipeline.py --local --engine docker
```

`--local` maps the bucket onto `_localrun/bucket/` and the DynamoDB table onto
`_localrun/dynamodb/sba_credit_risk_results.json`, with the same keys and the same items.

## What the local run covers

**The flow you deploy, from its own definition.** `run_pipeline.py` reads `step-function.json` and
executes it state by state: the validator's real output lands at the real `ResultPath`, the Choice
is evaluated from its own `Variable`, and a failed gate ends in the real Fail state with its
`CausePath` resolved. The full files went through it in-process — all eight files valid, the gate
passed, both chains fitted, 153 items written and the batch archived, in 368 s end to end on a
laptop. The 12-month EL in the items is $3,269,624,742, the notebook's figure to the dollar. The
processes peak at 1.09 GB resident for the validator and 1.81 GB for the model, and at 2.5 GB and
3.5 GB of committed memory.

The two images are the same two entrypoints on `python:3.12-slim`, with the releases the notebook
and the local run use pinned: pandas 3.0.5 for the validator, and pandas 3.0.5, numpy 2.5.3, scipy
1.18.1 and statsmodels 0.15.0 for the model. `--engine docker` runs the state machine with them as
its two tasks.

**41 tests, no AWS, no network, no mocks.** `test_validation.py` (11): both dtype rules, codes as
strings, `never_read` columns, the committed sample against its own validator, a whole batch, a
missing file, a stray file, an invalid file. `test_state_machine.py` (9): the task-token wiring, a
validator that cannot import its code still answering its token, the Choice reading the key the
validator returns, no Pass state and no entity named, the event rule, the whole sample to `Done`
with every table archived and no borrower column in the loan-level output, a file promoted
mid-run left for the next run and not archived, a short batch to `GateFailed`, and items that carry
`Decimal` and never a float or a NaN. `test_sba.py` (15): borrower columns never read, the company
filter, EXEMPT never in the target, features unmoved when every label and excluded field is
scrambled and `TermInMonths` and `CollateralInd` never read, a five-loan life table worked by
hand, competing below censoring, one band's multiplier exactly 1, a left-truncated table counting
a loan only from the age it entered, a PD split by charge-off age adding back up to the PD, the IRB
formula against the Basel Committee's 92.32% risk weight at PD 1% and LGD 45%, the stage-2 rule,
the severity rules, the 300-850 scaling, WoE, AUC, KS and PSI. `test_fdic.py` (6): every branch of
the label rule on five invented banks, the exits-kept variant, the last filing scoring every bank
that was open when its horizon began, event dedupe, the run-off ratio left unclipped with the loss
flags set, and bank-clustered errors.

**Two engine differences to carry into the deploy.** Locally the task token has nothing to carry:
the validator's `--local` path returns its answer, which is the payload `SendTaskSuccess` sends on
AWS. And a folder move is one atomic rename, where an S3 move is a copy and then a delete: a
crash between the two leaves the object in both places. The next run re-validates the copy left in
`new/` and writes it onto the same key in the ready folder, so the move is idempotent rather than
atomic, and the ready count still comes out exact.

## Deploying it

1. Create the landing bucket and turn on its **EventBridge notifications**; create the DynamoDB
   table `sba_credit_risk_results` with partition key `chain` (String) and sort key `item`
   (String).
2. Push the two images to ECR and register two Fargate task definitions, containers named
   `data-validity` and `credit-model`, each with a task execution role that can pull from ECR and
   write logs. On the full batch the validator's process peaks at 1.09 GB resident and 2.5 GB
   committed, the model's at 1.81 GB and 3.5 GB, so 4 GB and 8 GB tasks leave room; Fargate's
   default 20 GB of ephemeral storage holds the 0.85 GB batch each task downloads.
3. Task role: `s3:ListBucket`, `GetObject`, `PutObject`, `DeleteObject` on the bucket;
   `dynamodb:PutItem` on the table; `states:SendTaskSuccess` and `SendTaskFailure` for the
   validator.
4. Create the state machine from `step-functions/step-function.json`, replacing every `<...>`
   value — cluster, task definitions, subnets, security group. Its role needs `ecs:RunTask`;
   `ecs:StopTask`, `ecs:DescribeTasks` and the `events:PutTargets`/`PutRule`/`DescribeRule`
   permissions `.sync` uses; and `iam:PassRole` on every role the two task definitions name, the
   task execution roles as well as the task roles, or the first `RunTask` is refused.
5. Create the EventBridge rule from `event-pattern.json` with your bucket name, targeting the state
   machine through a role EventBridge can assume that allows `states:StartExecution` on it.
6. Upload a batch: `python local-development/stage_landing.py --bucket <landing-bucket> --data raw`.

Every AWS name in these files is account-specific and set by whoever deploys it.

## What the data cannot say

- **Charge-off is not default.** It lags 90 days past due by a liquidation, and a fully recovered
  default never appears. Every SBA PD here is a charge-off probability, and its 12-month form is a
  12-month probability of charge-off.
- **LGD and EAD are not separable.** Only their product with the original amount is identified,
  and it is gross of recoveries; Table 11 puts recoveries after charge-off at 2.7-6.7% of it.
- **No open loan has a status.** IFRS 9 staging can only be collective, and stage 3 is empty by
  construction.
- **`TermInMonths` is rewritten at charge-off**, so neither the contractual term nor any schedule
  built from it is available, for a feature or for a balance.
- **`CollateralInd` is attached at charge-off for the FY2000-04 approvals**, so whether those loans
  were secured at approval is not available, and the flag is out of the card and the severity
  model for every vintage.
- **The FDIC loss rate is a proxy.** COST is an estimate revised over time, and its denominator is
  a deposit snapshot rather than the amount the fund disbursed.
- **Mergers are not in the failures table.** A bank that stops filing has left for a reason the
  table does not record; both treatments of that exit are reported.
