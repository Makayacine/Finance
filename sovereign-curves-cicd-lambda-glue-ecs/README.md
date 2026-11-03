# sovereign-curves-cicd-lambda-glue-ecs

US Treasury yield curves and interest-rate risk, built every weekday from the Treasury's own
**FedInvest** end-of-day prices: a **Lambda** fetches the day's sources, a **Glue Python Shell**
job loads them into a Parquet lake with accrued interest and the dirty price, and an **ECS** task
builds three curves a day -- FIA's exact bootstrap, a Svensson fit of our own, and the Federal
Reserve Board's GSW curve as the answer key -- then prices every bill, note and bond off each and
measures its DV01, duration, convexity, key-rate DV01s and carry. Each of the three components has
its own GitHub Actions workflow that tests it and then deploys it. A `--local` path runs every
stage on a laptop with no AWS account, from a committed sample of October 2026 and two crisis days.

**South Africa is the second market, through the same code.** The same Lambda fetches the National
Treasury's fixed-rate bond auction results and its debt schedule, the same Glue job parses and
checks them, and the same ECS task prices each auction's cleared bonds with the JSE bond pricing
formula and bootstraps a curve from them with FIA, once per auction since April 2010, then checks
the curve's 10-year point every month against the OECD's and the IMF's South African bond yields.

The curve and risk arithmetic is the fixed income course's own: `bond_analytics/FIA.py` is vendored
unchanged, and everything here exists to feed it. FIA measures time in float years from "today" and
has no calendar, no day count and no accrued interest, so the package adds those and lets FIA's
`relative_bond`, `curve`, bootstrap, `YTM`, `duration` and `convexity` run as written.

The point of the project is a truth check that can fail:

> **Priced off GSW's published parameters, the notes and bonds FedInvest quoted on 23 October 2026
> come out within 0.6 to 2.1 basis points of yield on average in every bucket from one to ten years
> (notes), and the errors carry no trace of the mechanics: regressed on how far settlement is into
> the coupon period, the price error moves 0.027 per 100 against a half coupon of 1.938; on
> maturity, 0.018 per 10 years. What is left is the market -- the on-the-run 30-year 4.35 rich to
> the curve in March 2020, bills 47 basis points rich the same day, and one 6.5% bond quoted seven
> 32nds below what the bills say it is worth.**

South Africa has a check of its own, from the Treasury's own numbers:

> **Each new bond's first auction yield, through the JSE formula, gives back the initial issue
> price in the Treasury's debt schedule to within 0.001 for 11 of the 12 new bonds since 2010 whose
> first yield passes its checks, and to 0.005 for the twelfth. Over 153 months the auction curve's 10-year par yield
> moves with the OECD's long-term rate (monthly changes correlate 0.86) and sits 31 basis points
> below it, as the 10-year point of a rising curve should against a series of bonds "10 years and
> over": the 15-year point sits 28 above.**

## Architecture

```
EventBridge (weekdays 23:30 UTC)                                     GitHub Actions
      v                                                              test -> deploy, x3
Lambda: fetch            ->  S3 raw/        ->  Glue Python Shell: load  ->  S3 lake/ (Parquet)
FedInvest (CSRF, 1 POST)     one object         parse, clean -> dirty,       prices/ by date
GSW feds200628.csv           per source         validate, flag, never        securities/
Treasury auction records     per date           change a price;              reference_curves/ by year
NY Fed SOFR, BGCR, TGCR                         watermark per source         funding/ by year
NT auction results (xlsx)                                                    sa_bonds/
NT debt schedule (PDF)                                                       sa_auctions/ by year
OECD and IMF SA yields                                                       sa_reference/ by year
      |                                                  |
      +-- start_job_run(--date_filter) ------------------+
                                                         | Glue Job State Change: SUCCEEDED
                                                         v
                                          ECS task: curve and risk engine
                                          FIA bootstrap, Svensson fit, fit errors,
                                          DV01 / key rates / carry  ->  curves/ curve_params/
                                          (and the 2008 backfill)       fit_errors/ risk/  ->  Athena
                                          SA: JSE price, FIA bootstrap
                                          per auction, 10-year check ->  sa_prices/ sa_curves/
                                                                         sa_crosscheck/
```

No Step Functions. The schedule starts the Lambda; the Lambda fetches each source with its
failures isolated, writes the raw objects and starts the Glue job for that date with
`--date_filter`; an EventBridge rule on the job's `SUCCEEDED` state change runs the ECS task. The
rule and the schedule are in `eventbridge/`. The same image, run with `--mode backfill`, fetches
FedInvest from June 2008 one date at a time, loads it with the Glue job's own code, and runs the
engine over the range; for South Africa it fetches every fiscal year's auction file and one debt
schedule a year.

### The lab's shape, with nine changes

The reference lab deploys a Lambda, a Glue Python Shell job and an ECS task, each from its own
workflow. Each change below is a choice this project makes about the same pieces.

- **Step outputs go to `$GITHUB_OUTPUT`.** `::set-output` is deprecated; the Lambda workflow's
  "does the function exist?" step writes `exists=true|false` there.
- **Current action majors.** `actions/checkout@v7`, `actions/setup-python@v7`,
  `aws-actions/configure-aws-credentials@v6`, `amazon-ecr-login@v2`,
  `amazon-ecs-render-task-definition@v1`, `amazon-ecs-deploy-task-definition@v2`.
- **A supported Python.** The Lambda runs on `python3.13` and the image on `python:3.13-slim`.
  The Glue loader stays on 3.9, because that is Python Shell's only runtime (see the engine notes).
- **OIDC only.** Each deploy job has `permissions: id-token: write, contents: read` and one
  `role-to-assume`; there is no access key anywhere, and the workflows read no secrets.
- **`pythonshell`, not `glueetl`.** The job is created with
  `{"Name": "pythonshell", "PythonVersion": "3.9"}`, 0.0625 DPU and the `analytics` library set.
- **Path filters.** Each workflow runs only for its own component's folder and its own workflow
  file, on pushes to `main` and on pull requests alike, so a change to the Lambda never rebuilds
  the image.
- **No credentials and no query strings built by hand.** Every source is public, so the code holds
  no password; bucket and job names come from the environment, and the one query with parameters
  is built with `urlencode`.
- **Tests that assert outcomes.** The handler tests read back what landed in moto's S3 and the
  arguments moto's Glue recorded for the job run; none passes because a mock was called.
- **Register the task definition, then retarget the rule.** The engine is a task EventBridge runs,
  not a service, so the deploy registers the new revision and points the `SUCCEEDED` rule at it.
  There is no service to wait on, and no `wait-for-service-stability`.

**The deploy job runs only on a push to `main`, and only when its repository variable holds a role
ARN:** `if: github.event_name == 'push' && github.ref == 'refs/heads/main' &&
vars.SOVEREIGN_CURVES_LAMBDA_ROLE_ARN != ''`, with `..._GLUE_...` and `..._ECS_...` for the other
two. `vars` is available in a job-level `if` and reads as `''` when unset, so on a repository
with no variables the deploy jobs show as skipped and the runs stay green; `secrets` is not
available there, which is why the guard reads a variable. One variable per workflow, because the
lab uses one deploy role per workflow and this repository holds other projects whose variables must
not switch these on. Setting a variable later still never deploys from a pull request.

## The data

| Source | What | Licence |
| --- | --- | --- |
| [FedInvest](https://www.treasurydirect.gov/GA-FI/FedInvest/selectSecurityPriceDate), US Treasury | end-of-day prices for every marketable Treasury: CUSIP, type, rate, maturity, call date, buy, sell, end of day | US government work, public domain |
| [Treasury Securities Auctions Data](https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/), US Treasury Fiscal Data | one record per auction since 1979: original term, issue and dated dates, first coupon date and length | US government work, public domain |
| [GSW yield curve](https://www.federalreserve.gov/data/yield-curve-tables/feds200628.csv), Federal Reserve Board | Svensson parameters and zero yields, daily from 1961 | US government work, public domain |
| [SOFR, BGCR, TGCR](https://markets.newyorkfed.org/api/rates/secured/all/search.json), Federal Reserve Bank of New York | the overnight repo rates, daily from 2 April 2018 | the New York Fed's Terms of Use: copying and distribution with its notice and disclaimer, in `data/sample/SOURCES.md` |
| [Fixed-rate bond auction results](https://investor.treasury.gov.za/Debt%20Operations%20and%20Data/Historical%20Results/Fixed-rate%20bonds/), National Treasury of the Republic of South Africa | one spreadsheet per fiscal year from 2010-13: auction and settlement dates, bond, coupon and redemption year, amounts, bids, clearing yield, best and worst bid, bid-to-cover | download and copying for educational and non-commercial use, unmodified, with the Treasury acknowledged: the parsed rows are committed with `Source: National Treasury of the Republic of South Africa` on each, the spreadsheets are not |
| [Schedule of domestic government bonds](https://investor.treasury.gov.za/Debt%20Operations%20and%20Data/Schedule%20of%20Domestic%20Debt/), National Treasury | one PDF a month: each bond's coupon, issue date and price, interest dates, redemption and nominal outstanding | as above: parsed rows committed, PDFs not |
| [Long-term interest rates, South Africa](https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/ZAF.M.IRLT.PA.....), OECD | monthly, the South African Reserve Bank's yield on government bonds of 10 years and over | any use, with the OECD's citation, on every row |
| [Government bond yield, South Africa](https://fred.stlouisfed.org/series/INTGSBZAM193N), IMF International Financial Statistics, as FRED distributes it | monthly | publishing with the IMF named as source, on every row; commercial reuse needs the IMF's permission |

**FedInvest's prices are clean.** The page wants the `_csrf` token and session cookie from a GET
before it will accept the POST of `priceDate`. On 30 October 2026 it listed 464 securities: 50
bills, 241 notes, 112 bonds, 53 TIPS and 8 FRNs. Bills, notes and bonds make the curves; TIPS and
FRNs are loaded with their type and kept out, because FedInvest gives no index ratio and FIA has no
floating leg.

**FedInvest has no issue dates**, so on its own it cannot say which note is the on-the-run 10-year
or whether a first coupon is odd. The Treasury's auction records can: the original auction of each
CUSIP gives its original term, its issue and dated dates and the length of its first coupon
period. They cover every note and bond FedInvest lists on the three truth-check dates.

**Settlement is the next business day, and the bills decide it.** A clean price cannot show the
convention -- it is continuous across a coupon date whichever settlement it was struck for -- but a
bill a few days from maturity can, because counting one day too many collapses its yield. On Friday
30 October 2026:

| Bill maturing | 3 Nov | 5 Nov | 10 Nov | 12 Nov | 17 Nov | 19 Nov |
| --- | --- | --- | --- | --- | --- | --- |
| discount yield from Monday 2 Nov (T+1) | 3.82% | 3.88% | 3.88% | 3.88% | 3.88% | 3.88% |
| discount yield from Friday 30 Oct (T+0) | 0.95% | 1.94% | 2.82% | 2.98% | 3.23% | 3.30% |

The coupon date then checks the accrued interest. Five notes between one and ten years paid a
coupon on 15 October 2026. Settling T+1, the trade on the 14th settles on the coupon date: their
median dirty price fell 1.8960 that day against a half coupon of 1.9375, and dirty price plus the
coupon paid moved -0.0210, while the other 186 notes moved +0.0096.

**The committed sample** in `data/sample/` is every October 2026 business day, 15 September 2008
and 16 March 2020: 24 FedInvest pages (Columbus Day has no prices), the 1,765 auction records of the
965 CUSIPs on them, the 19 GSW rows for those dates and the New York Fed's rates, gzipped at the keys
the Lambda writes them to. The full history stays out of the repository (`data/full/` is ignored).

**South Africa's rows are committed whole**, because they are small: `data/sa/nt_auctions.csv`,
every fixed-rate bond auction from 6 April 2010 to 29 September 2026, 2,386 bond auctions on 811
dates, and `data/sa/nt_bonds.csv`, the 35 fixed-rate bonds in the 17 debt schedules read (31 March
each year from 2011, and 30 September 2026). `local-development/sa_download.py` fetches the
Treasury's files with the Lambda's fetchers, parses them with the loader's parsers and writes both;
the OECD and IMF files sit in `data/sample/raw/` with the US sample.

## The pieces

**`bond_analytics/`** is the package every component imports.

- `dates.py` turns a CUSIP and calendar dates into an FIA `relative_bond`: coupons every six
  months counted back from maturity with the end-of-month rule, actual/actual (ICMA) accrued
  interest including the Treasury's rule for short and long first coupons, the Federal Reserve's
  holiday calendar, and each cash flow's time as actual days from settlement over 365.25. A coupon
  falling on the settlement date belongs to the seller. Standard library only.
- `bootstrap.py` feeds FIA's `curve.build_from_bonds` one bond per maturity bucket: bills under a
  year, then fixed-coupon notes and bonds, never callable, preferring issues older than the first
  off-the-run. FIA adds one knot per bond and divides by zero when two bonds share a maturity;
  the buckets make that impossible.
- `svensson.py` fits Svensson parameters by minimising duration-weighted price errors, GSW's
  objective, from six starting points, one of them the day before's answer, and records any
  parameter that ends on a bound. `SvenssonCurve` subclasses FIA's `curve` and overrides only
  `discount_factor`, so FIA's pricing and risk functions run on it; its `shift` keeps the curve's
  shape, where FIA's would rebuild it as piecewise linear. FIA's own `build_nelson_siegel` cannot
  be used: `exp` is undefined in it and its result is discarded.
- `risk.py` takes DV01, modified duration and convexity on the bond's own semiannual yield through
  FIA's `YTM`, `create_flat_curve`, `dollar`, `duration` and `convexity`, and key-rate DV01s at
  2, 5, 10, 20 and 30 years from triangular one-basis-point bumps of the fitted curve shifted by the
  bond's z-spread. The triangles add to one, so the five key rates add to the parallel curve DV01.
- `quality.py` flags, `truth.py` tabulates, `schemas.py` fixes every lake table's columns.
- `jse.py` is the JSE bond pricing formula: yield to all-in price, accrued interest actual/365,
  the books-closed (ex-coupon) period ten days before each coupon, coupons on two fixed day-and-month
  dates, and the same cash flows as an FIA `relative_bond`. `nt_auctions.py` and `nt_schedule.py`
  read the Treasury's spreadsheets and PDFs and check them; `sa_reference.py` reads the OECD and IMF
  files; `sa_curve.py` builds a curve per auction and the monthly check.

**`lambda-functions/fetch.py`** is standard library and boto3, so the deployment package is the one
file. Each source is fetched and written inside its own `try`; one failing is logged and reported in
the response and never stops the others, and the Glue job is started for the sources that arrived.

**`glue-pythonshell/load.py`** loads raw dates into the lake, in the order auctions, FedInvest, GSW,
NY Fed, then the Treasury's schedule, its auctions, OECD and IMF, so a day's prices see that day's
new CUSIPs and a day's auctions that day's bonds. `load_type` is `full` or `incremental`;
`--date_filter` stops the load at a date, as in the lab. A page for the wrong date, a CUSIP listed
twice or a changed header stops its own source at that date, so the watermark never skips a day,
and fails the run once the other sources are loaded. A holiday page has no table and writes no
partition. The loader never changes a price.

**`ecs/app.py`** runs the engine over each price date after its own watermark and writes
`curves/`, `curve_params/`, `fit_errors/` and `risk/`, one Parquet file per date; then the South
African engine over each auction date after its own, writing `sa_prices/` and `sa_curves/` by year
and `sa_crosscheck/`. `--market us` or `sa` runs one.

**`athena/create-tables.sql`** has a table for each of the fourteen lake tables, with partition
projection, so a date is readable as soon as its file lands.

## What it finds

### The truth check

GSW's parameters for the day, through `SvenssonCurve` and FIA's `price`, against FedInvest's clean
price plus accrued. Mean absolute error, notes and bonds, three months or more from maturity, no
callable bonds:

| Bucket | 2008-09-15 notes | bonds | 2020-03-16 notes | bonds | 2026-10-23 notes | bonds |
| --- | --- | --- | --- | --- | --- | --- |
| 1-2y | 0.081 / 5.2 bp | | 0.038 / 2.4 bp | 0.107 / 7.0 bp | 0.018 / 1.5 bp | 0.219 / 17.3 bp |
| 2-3y | 0.088 / 3.6 bp | | 0.054 / 2.2 bp | 0.105 / 3.6 bp | 0.013 / 0.6 bp | 0.080 / 3.7 bp |
| 3-5y | 0.185 / 4.8 bp | | 0.113 / 2.6 bp | 0.092 / 2.0 bp | 0.020 / 0.6 bp | 0.083 / 2.4 bp |
| 5-7y | 0.627 / 10.5 bp | 0.301 / 4.0 bp | 0.084 / 1.4 bp | 0.227 / 3.2 bp | 0.104 / 2.1 bp | |
| 7-10y | 0.951 / 12.3 bp | 0.518 / 5.8 bp | 0.189 / 2.1 bp | 0.211 / 2.1 bp | 0.126 / 2.0 bp | 1.013 / 13.9 bp |
| 10-15y | | 0.387 / 3.3 bp | | 0.074 / 0.6 bp | | 0.396 / 4.7 bp |
| 15-20y | | 0.252 / 1.7 bp | | 1.142 / 5.4 bp | | 0.281 / 2.8 bp |
| 20-25y | | 0.076 / 0.5 bp | | 1.080 / 4.4 bp | | 0.137 / 1.5 bp |
| 25-30y | | 0.157 / 0.9 bp | | 1.212 / 4.5 bp | | 0.247 / 2.0 bp |

Price per 100, then basis points of yield. Under a year, bills were 22.0, 46.8 and 2.6 basis points
from the curve on the three days: GSW is not fitted to bills, and in both crises they traded rich.
The notebook has every bucket, signed errors and counts.

**Three regressions guard the mechanics, and none moves:**

| | 2008-09-15 | 2020-03-16 | 2026-10-23 |
| --- | --- | --- | --- |
| median half coupon | 2.250 | 1.125 | 1.938 |
| price error per coupon period elapsed (a wrong accrued shows near the half coupon) | 0.040 | 0.068 | 0.027 |
| price error per 10 years of maturity (a wrong time axis grows with maturity) | -0.006 | -0.097 | -0.018 |
| price error per coupon point, within buckets, on-the-run left out (wrong cash flows grow with the coupon) | +0.165 (0.016) | +0.012 (0.016) | +0.005 (0.006) |
| the same in basis points | +2.25 (0.40) | +0.54 (0.21) | +0.56 (0.13) |
| basis points per coupon point, years since issue held | +0.95 (0.69) | -0.11 (0.47) | +0.47 (0.13) |
| basis points per year since issue | +0.45 (0.20) | +0.16 (0.10) | +0.18 (0.03) |

Standard errors in brackets. In 2008 the coupon slope is mostly age: held beside the years since
issue it falls to under two standard errors. In price it is a hundredth of a point or less on the
other two days. What remains on 23 October 2026 is half a basis point of yield per point of coupon,
with no slope in price to go with it.

### On-the-run against off-the-run, like with like

Pooled over every maturity the comparison is skewed: every note under a year is off-the-run, and a
short bond's yield moves a lot for a small price error, so the off-the-run issues' pooled error in
basis points carries maturities the on-the-run issues do not have. Inside each bucket that holds an on-the-run issue, mean absolute error in bp:

| | 2008-09-15 | | | 2020-03-16 | | | 2026-10-23 | | |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bucket | on | first off | off | on | first off | off | on | first off | off |
| 1-2y | 3.2 | 3.9 | 5.5 | 15.3 | 5.8 | 2.4 | 0.6 | 0.2 | 2.1 |
| 2-3y | | | | 8.7 | 3.4 | 2.1 | 1.6 | 0.3 | 0.8 |
| 3-5y | 0.1 | 2.7 | 5.0 | 11.6 | 6.2 | 2.4 | 0.5 | 0.5 | 0.7 |
| 5-7y | | | | 2.4 | 0.1 | 1.6 | 1.5 | 1.4 | 2.1 |
| 7-10y | 19.6 | 17.9 | 8.8 | 6.9 | 4.1 | 1.7 | 1.3 | 0.6 | 3.2 |
| 15-20y | | | | | | | 2.0 | 2.8 | 2.8 |
| 25-30y | 0.4 | 1.4 | 0.8 | 16.5 | 12.0 | 3.4 | 2.9 | 3.1 | 1.8 |

In March 2020 the on-the-run issue is the worst-priced in every bucket that holds one, the 30-year
4.35 rich to the curve (-16.5 bp); on the day Lehman failed it is the 10-year, 1.66 rich (-19.6 bp).
In October 2026 the three differ by a basis point or two either way.

### A quote the loader flags and keeps

The largest error in basis points on 23 October 2026, +399.7, belongs to the 6.5% bond maturing
15 November 2026, quoted 99-29 clean. Two notes mature the same day, and the bills value all three without a
fitted curve:

| CUSIP | Coupon | Buy | Sell | End of day | Dirty | Less its value on the bills |
| --- | --- | --- | --- | --- | --- | --- |
| 91282CJK8 | 4.625 | 0 | 100.03125 | 100.00000 | 102.0611 | -0.96 32nds |
| 912828U24 | 2.000 | 0 | 99.87500 | 99.87500 | 100.7663 | -0.49 32nds |
| 912810EY0 | 6.500 | 0 | 99.96875 | 99.90625 | 102.8030 | -7.16 32nds |

The notes sit within a 32nd, the quote's own rounding; the bond is the quote. A fixed tolerance
against the bills would not do -- at two 32nds it flagged 11 to 21 notes a day on the seven October
and November 2026 days tried, mostly notes six to ten months out, and all 51 checked on 16 March
2020: notes trade cheap to bills -- so the
loader compares each quote's gap with the median gap of the notes and bonds maturing within 31 days
of it, and adds `off_neighbours` beyond four 32nds. Over the sample, 1,188 quotes have neighbours:
median 0.40 32nds, 90th percentile 1.42, and 41 flagged -- 40 of them three old 30-year bonds,
the 6.5% of November 2026 on 18 days, the 6.375% of August 2027 on 14 and the 6.625% of February
2027 on 8. The price stays as quoted. The fit and the error tables leave out everything within three
months of maturity, as GSW do, which takes the November bond out on its own.

### Three curves

On 23 October 2026, zero rates against GSW's, in basis points:

| Tenor | 0.25 | 0.5 | 1 | 2 | 3 | 5 | 7 | 10 | 15 | 20 | 25 | 30 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| our Svensson fit | 2.8 | 2.3 | 1.6 | 0.9 | 0.7 | 0.5 | 0.1 | -0.6 | -1.2 | -1.0 | -0.5 | 0.2 |
| FIA's bootstrap | -3.0 | 0.0 | -5.7 | -0.0 | -0.1 | 0.9 | 2.7 | -4.6 | 0.2 | 2.5 | -1.7 | -4.0 |

Our fit's parameters that day are (0.000, 4.046, 4.465, 16.115, 1.964, 16.046) against GSW's
(0.000, 4.010, 4.512, 16.154, 1.923, 15.833), over 301 notes and bonds, RMSE 3.14 bp. On
16 March 2020 the two agree within 3.5 bp at six months and 0.7 bp from a year out. On
15 September 2008 the yields agree within 1.2 bp from two to twenty years, but the parameters do
not: the two humps' decay parameters are close (GSW's own are 3.98 and 4.94), their loadings trade
off, and ours ends with `beta3` on its bound of 60. **A fit on a bound is recorded, never silent:**
`curve_params` and every `fit_errors` row carry `params_at_bound`, and the run logs a count. In the
sample, 21 of 23 fits end with `beta0`, the long-run level, at zero -- where GSW's own fit puts it,
5.5e-13 on 23 October -- one with `beta3`, and one on no bound.

Against FedInvest, notes and bonds three months or more from maturity, every sample day:

| Curve | Security-days | Mean absolute error | |
| --- | --- | --- | --- |
| FIA's bootstrap | 7,573 | 2.26 bp | 0.047 per 100 |
| our Svensson fit | 7,573 | 2.27 bp | 0.123 per 100 |
| GSW | 5,880 | 2.30 bp | 0.120 per 100 |

The bootstrap is exact on its own 40 to 45 knots a day and interpolates between them; the two
smooth curves miss everything a little. GSW's row is shorter because its file ends on 23 October.

### Risk

Per 100 face, the on-the-run issues on 30 October 2026, funding at SOFR, 3.95%:

| Maturity | Coupon | Yield | DV01 | Mod. duration | Convexity | KRD 2y | 5y | 10y | 20y | 30y | 30-day carry |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2028-09-30 | 4.750 | 4.571 | 0.0181 | 1.80 | 4.2 | 0.0186 | | | | | 0.060 |
| 2029-10-15 | 4.875 | 4.644 | 0.0274 | 2.72 | 9.0 | 0.0196 | 0.0085 | | | | 0.070 |
| 2031-09-30 | 5.000 | 4.718 | 0.0437 | 4.30 | 21.9 | 0.0041 | 0.0406 | | | | 0.077 |
| 2033-09-30 | 5.000 | 4.811 | 0.0586 | 5.77 | 39.6 | 0.0029 | 0.0370 | 0.0201 | | | 0.078 |
| 2036-08-15 | 4.625 | 4.929 | 0.0758 | 7.69 | 71.6 | 0.0027 | 0.0105 | 0.0644 | | | 0.052 |
| 2046-08-15 | 5.125 | 5.338 | 0.1192 | 12.10 | 200.1 | 0.0030 | 0.0087 | 0.0254 | 0.0838 | | 0.094 |
| 2056-08-15 | 5.125 | 5.313 | 0.1456 | 14.82 | 331.1 | 0.0030 | 0.0087 | 0.0242 | 0.0358 | 0.0752 | 0.094 |

Across all 8,991 security-days the five key-rate DV01s add up to the parallel curve DV01 to within
6.7e-7 of it. Carry is the coupon income over 30 days less financing the dirty price at the day's
funding rate: SOFR from April 2018 and, before it, the money-market yield of the bill nearest four
weeks out, 0.46% on 15 September 2008.

The interesting problems were about conventions the data does not state:

- **Settlement had to be read from the bills.** A coupon-date check cannot separate T+0 from T+1,
  because a clean price is continuous across a coupon date by construction. The six shortest bills
  on 30 October 2026 are flat at 3.82-3.88% counted from Monday and climb from 0.95% counted from
  Friday.
- **A library with no calendar needs one built around it, not into it.** FIA's date code paths are
  stubs. Feeding it float years from settlement, cash flows already dated with the end-of-month rule
  and odd first coupons, and dirty prices, leaves its bootstrap, `YTM` and risk functions as
  written; the truth check's accrued and maturity slopes are what say the adapter is right.
- **A pooled table can say the opposite of the truth.** Pooled over maturities, the off-the-run
  issues, the ones GSW is fitted to, came out best in price and among the worst in basis points;
  inside buckets, the on-the-run issues are the outliers in both crises.

## South Africa

### The Treasury's figures, checked and never changed

The 16 auction files hold three layouts: the 2010-13 file is one long table with amounts in
R million and no best or worst bid; the rest have a sheet per month, a column per bond, and the
labels down the first or the second column; the 2012-13 to 2014-15 files state yields as fractions
(0.0757), which the loader multiplies by 100 and records as `yield_unit = fraction` on all 521 of
those rows. 2,525 bond columns come out of the 16 files, and 2,386 bond auctions once the year the
2010-13 and 2012-13 files share is taken once. No figure is changed. Each check that fails is a
flag on the row, and a flagged yield stays in `sa_auctions` and out of the curve:

| Flag | Rows | Example | Curve |
| --- | --- | --- | --- |
| `no_bid_range` | 228 | the 2010-13 long table has no best or worst bid | in |
| `coupon_differs_from_schedule` | 51 | R186's "(5%;2026)" against its 10.5% | in |
| `maturity_year_differs_from_schedule` | 50 | R2035's terms given as "2030" from June 2019 to January 2020, a column copied | in |
| `date_outside_fiscal_year` | 36 | the 2018-19 file's January and February auctions dated 2018, on Mondays; the same days in 2019 are Tuesdays | out |
| `clearing_outside_bids` | 32 | R2053 at 1.85 on 22 April 2025 against bids of 11.76 to 12.00; R2044 at 1105.0 on 29 July 2025; all three bonds on 29 August 2017 half a basis point to two below their best bid | out |
| `files_disagree` | 16 | the long table and the 2012-13 file differ on 8 of their 44 shared dates: R2048 at 8.40 or 9.50 on 21 August 2012 | out |
| `misdated_auction` | 15 | the 2012-13 file's February and March 2013 sheets sell R2030 and R2037 months before either was issued, at 8.5 to 9.2%, where the long table has the same weeks at 7.9 to 8.5% | out |
| `before_issue_date` | 7 | the bonds in those auctions not yet issued, R2032 in the auction of 8 July 2014 "settling" in June, and R2053's first auction "settling" on Good Friday, 7 April 2023, four days before its issue | out |
| `off_neighbouring_auctions` | 5 | R214 at 8.01 on 6 September 2011 between 8.59 and 8.54, with the day's other bond 23 basis points lower | out |
| `settlement_date_doubtful` | 3 | the 8 July 2014 auction settling on 11 June | out |
| `bond_code_unrecognised` | 1 | a bond column reading `0` in the long table | out |

A row can carry more than one flag. 104 of the 2,386 rows stay out of the curve and 2,282 are
priced; 20 auction dates are left with no usable bond, eight of them the misdated Mondays of 2018.
The spike check needs both of a bond's neighbouring auctions within six weeks, more than 50 basis
points from each on the same side, and the day's other bonds not moving half as far the same way:
on 30 March 2021, when R2044, R2048 and R2037 all rose 0.6 to 0.8 percentage points, none is flagged.

The debt schedule has one inconsistency of its own: R2030's interest dates are listed as 28/02 and
31/08, but it redeems on 31 January 2030. The coupon cycle is taken from the redemption date and the
bond is flagged; its issue price below says the redemption date is the right one.

### The JSE formula, against the Treasury's issue prices

There is no free South African price to test a yield-to-price formula against: JSE mark-to-market
data carries no redistribution licence. The debt schedule has one price per bond, though, its
initial issue price, and for a bond first sold at auction that is the first clearing yield on the
issue date. Priced with `jse.price`:

| Bond | Issued | Clearing yield | Schedule price | All-in | Clean | Gap |
| --- | --- | --- | --- | --- | --- | --- |
| R213 | 2010-05-28 | 9.050 | 80.922 | 82.62874 | **80.92189** | -0.00011 |
| R214 | 2010-06-04 | 8.820 | 75.517 | 77.22692 | **75.51733** | +0.00033 |
| R2023 | 2012-06-22 | 7.770 | 99.806 | 102.24794 | **99.80616** | +0.00016 |
| R2037 | 2013-07-19 | 8.820 | 96.875 | 100.81035 | **96.87473** | -0.00027 |
| R2030 | 2013-10-04 | 8.540 | 95.261 | 96.68549 | **95.26083** | -0.00017 |
| R2032 | 2014-06-13 | 8.970 | 95.303 | **95.30336** | 93.63076 | +0.00036 |
| R2044 | 2014-07-18 | 9.065 | 100.832 | **100.83272** | 96.80532 | +0.00072 |
| R2035 | 2015-07-17 | 8.820 | 103.843 | **103.84342** | 100.46363 | +0.00042 |
| R2040 | 2015-09-11 | 9.130 | 99.747 | **99.74710** | 98.71148 | +0.00010 |
| R2033 | 2024-09-13 | 9.970 | 104.700 | **104.69510** | 100.14715 | -0.00490 |
| R2038 | 2024-09-13 | 10.905 | 104.710 | **104.71031** | 99.76442 | +0.00031 |
| R2039 | 2025-10-03 | 9.905 | 99.778 | 99.85878 | **99.77762** | -0.00038 |

The Treasury publishes some issue prices clean and some all-in, and each matches its own kind. A
wrong accrued would have shown in the clean ones, and a wrong discounting in all of them. R2030's
match settles its cycle: on the schedule's 28/02 and 31/08 its accrued interest would be 34 days,
not 65, and the clean price would miss by 0.08. R2042, the twelfth new bond since 2010, cleared at
9.095 against bids of 10.09 to 10.15 and is flagged; R2048 was issued before its first auction,
and R2053's first auction is the one dated Good Friday.

120 of the 2,282 priced auctions settle in a books-closed period, ex-coupon, with negative accrued
interest and no next coupon in the cash flows. No auction settles in a bond's last coupon period,
where the formula turns to simple interest, so `jse.price` refuses that case. On the same cash flows,
FIA's own semiannual `YTM` comes within 1.45 basis points of the JSE yield (mean -0.67): the
difference between the JSE's broken period and FIA's actual/365 time axis.

### A curve per auction, and the 10-year check

Each auction date's usable bonds, priced, go to FIA's `build_from_bonds` unchanged: 791 curves, 636
of them from three bonds, 120 from two, 33 from four and two from one, in about three seconds for
all of them. Each curve reprices its bonds exactly and holds the zero rate flat beyond them; a tenor
outside the shortest and the longest bond is marked `extrapolated`. Ten years lies inside the knots
on 387 of the 791 dates.

The monthly 10-year par yield, from those dates only, against the two series, in basis points:

| Year | Auction dates | Inside the knots | Par less OECD | Par less IMF |
| --- | --- | --- | --- | --- |
| 2010 | 37 | 20 | -12.1 | -12.2 |
| 2011 | 48 | 32 | -27.5 | -28.1 |
| 2012 | 45 | 24 | -43.8 | -42.7 |
| 2013 | 49 | 25 | -29.8 | -30.0 |
| 2014 | 48 | 7 | -2.0 | -2.3 |
| 2015 | 47 | 0 | | |
| 2016 | 48 | 5 | +12.6 | +9.9 |
| 2017 | 48 | 18 | -28.4 | -30.1 |
| 2018 | 49 | 18 | -29.7 | -30.6 |
| 2019 | 42 | 23 | -15.2 | -15.8 |
| 2020 | 49 | 40 | -71.4 | -75.7 |
| 2021 | 49 | 32 | -36.8 | -38.3 |
| 2022 | 49 | 39 | -24.4 | -34.2 |
| 2023 | 48 | 31 | -43.6 | -44.1 |
| 2024 | 49 | 34 | -25.2 | -25.6 |
| 2025 | 48 | 24 | -45.3 | -52.0 |
| 2026 | 38 | 15 | -9.2 | -9.4 |

Over the 153 months both sides have, the par yield is 30.6 basis points below the OECD series on
average (mean absolute 31.6, standard deviation 24.0) and 32.6 below the IMF's (34.0); monthly
changes correlate 0.86 with each. The two series come from the same Reserve Bank yield and differ
by 3.0 basis points on average. The gap is a matter of maturity: the series is bonds of 10 years
and over, and the same comparison at other tenors brackets it --

| Tenor | Months | Par less OECD | Mean absolute |
| --- | --- | --- | --- |
| 7 years | 83 | -98.7 | 98.7 |
| 10 years | 153 | -30.6 | 31.6 |
| 15 years | 181 | +28.1 | 30.6 |
| 20 years | 183 | +50.9 | 51.6 |

-- so the series sits between the curve's 10- and 15-year points in an average month. The gap is
widest in 2020, -71 basis points over the year and -128 in March, and 2020 is the year the curve
was steepest from 10 to 15 years: 116 to 136 basis points in its five steepest months, against a
median month's 53. In 2015 every auction sold bonds on one side of ten years, so there is no month
to compare.

The interesting problems were in the figures:

- **A yield-to-price formula needed a price to be tested against.** The issue prices in the
  Treasury's own schedule are the only free South African prices, and they come clean or all-in
  without saying which; matching one or the other to a thousandth, bond after bond, is the test.
- **Some wrong rows pass every check a row can make on its own.** Two months of later auctions
  filed under February and March 2013, with yields inside their own bids, are caught only by selling
  a bond before it existed; a bond label swapped between two columns only by the second file that holds
  the same day. Both checks look across rows, and both keep the figure and flag it.
- **The schedule contradicts itself once,** on R2030's coupon dates, and the issue price says which
  half is right.

## Running it

Python 3.12+ and `pip install -r requirements.txt`. No AWS account.

```bash
python -m pytest tests glue-pythonshell ecs -q
```

```bash
cd lambda-functions && python -m unittest discover -v
```

The whole chain from the committed sample -- stage it where the Lambda would have written it, load
it with the Glue job's code, run the engine -- in about a minute:

```bash
python local-development/run_local.py
```

That includes South Africa from `data/sa`. To fetch the Treasury's files afresh, rewrite `data/sa`
and keep the files, then load them through the Glue job's own parsers rather than the committed
rows:

```bash
python local-development/sa_download.py --raw _localrun/sa-raw
```

```bash
python local-development/run_local.py --sa-raw _localrun/sa-raw
```

Both routes give the same `sa_auctions`, `sa_prices`, `sa_curves` and `sa_crosscheck`, value for
value.

The truth check, against the staged sample or, with `--fetch`, against a fresh download:

```bash
python local-development/truth_check.py --raw-root _localrun/raw-bucket
```

The engine image, against the same folders, with no network at all:

```bash
docker build -f ecs/Dockerfile -t sovereign-curves-engine .
```

```bash
docker run --rm --network none -v "$PWD/_localrun:/data" sovereign-curves-engine --mode daily --local-root /data
```

`sovereign-curves-walkthrough.ipynb` builds `_localrun/` if it is not there and walks every figure
in this README.

## What the local run covers

**The code you deploy, as it is.** The Lambda's `fetch_all`, the Glue job's `run` and the engine's
`run` are called unchanged, with two folders whose files sit at the buckets' keys standing in for
S3. `run_local.py --fetch --start <date> --end <date>` runs the fetcher against the live sources;
it fetched the four US sources for 2 November 2026, and `fetch_all` fetched the four South African
ones for 3 November 2026. `sa_download.py` fetched the 16 auction files and 17 debt schedules the
same day. The engine image built from `ecs/Dockerfile` ran both markets in full with
`--network none`: the 23 US sample dates in 56.8 seconds and the 811 South African auction dates
in 3.2.

**155 tests.** `tests/test_dates.py` (15): holidays, T+1 settlement, the end-of-month rule, accrued
by hand, a coupon on the settlement date, short and long first coupons against the Treasury's rule,
and a round trip through FIA's `YTM`. `tests/test_sources.py` (8): the FedInvest page, a holiday
page, a page for the wrong date, a changed header, the GSW file's preamble and Nelson-Siegel years,
the original auction kept over reopenings, and run ranks. `tests/test_curves.py` (11): GSW's
published yields from its parameters, a fit that recovers the parameters that priced its bonds,
`SvenssonCurve` inside FIA, one bond per bucket, the bootstrap repricing every knot, and DV01,
duration and convexity against finite differences. `tests/test_checks.py` (8): the quote flag on a
seven-32nds quote and not on a common basis, the bound flag, and both new tables on data built to
give known answers. `tests/test_sa_sources.py` (29): the Treasury's three sheet layouts, yields as
fractions, every spelling of the coupon and redemption, each flag on the row it was found on (R2053's
1.85, the misdated 2013 sheets, the two files that disagree, the spike against a move across the
curve), the debt schedule's lines, R2030's cycle, a three-legged bond, and the OECD and IMF files
with their citations. `tests/test_sa_pricing.py` (19): eight issue prices from their first auction
yields, the books-closed date, an ex-coupon buyer's cash flows, the leap-year coupon, the refused
last period, FIA's bootstrap repricing each auction's bonds, and the monthly check.
`tests/test_wiring.py` (15): every workflow's triggers, guard, permissions, actions and runtimes, the
event pattern against a Glue event, and every Athena column against the Parquet schema.
`lambda-functions/test_fetch.py` (19): the CSRF handshake request by request, each source's checks,
the Treasury's file for a fiscal year, its newest schedule, a reset connection tried again and a
404 not, one failing source and one failing write isolated, the pause between backfill dates, and
the handler on moto's S3 and Glue. `glue-pythonshell/test_load.py` (16): `awsglue` through
`sys.modules`, dirty prices and the watermark, nothing new on a rerun, `--date_filter`, a full
reload, a holiday, a bad page stopping its source there, a duplicate CUSIP, the flagged quote kept,
GSW upserts, the NY Fed's averages left out, the Treasury's files with every row sourced, a revised
file replacing its rows, and auctions with no schedule failing only their own source -- on Python
3.9 with pandas 1.4.2, numpy 1.22.3, boto3 1.21.21, pyarrow 7.0.0, openpyxl 3.1.5, xlrd 2.0.1 and
pypdf 5.9.0 as well as on 3.13. `ecs/test_app.py` (15): the engine on moto's S3 over two sample
days, our fit against GSW, a bound recorded, funding and carry by hand, the backfill, and the South
African engine over every committed auction, its check against OECD and IMF, a rerun with nothing
new and its backfill. All of them pass in UTC and in Africa/Johannesburg.

**Engine differences to carry into the deploy.**
- **Glue Python Shell runs Python 3.9 only.** 3.6 reached end of life on 1 June 2026 and
  `--glue-version` is ignored for Python Shell. The loader and the modules it imports are written
  for 3.9 and the `analytics` set's versions; pyarrow is not on that list, so the job adds
  `pyarrow==7.0.0` with `--additional-python-modules`, with `openpyxl==3.1.5`, `xlrd==2.0.1` and
  `pypdf==5.9.0` for the Treasury's `.xlsx`, `.xls` and PDF files; the engine image, on 3.13, pins
  their current versions. The parsed schedules and auctions are the same on both. `bond_analytics`
  arrives as a wheel in
  `--extra-py-files`, because Python Shell 3.9 takes no `.egg`. The Glue workflow's tests run in a
  `python:3.9-slim` container with those pins. AWS now points Python Shell workloads towards Lambda
  or ECS; this project keeps the lab's Glue job.
- **S3 for folders.** `FolderStore` lists, reads, writes and deletes by the same POSIX keys
  `S3Store` uses, and every key is built with `PurePosixPath`, so the keys are the same on Windows.
- **One Parquet schema per table.** pyarrow writes a column that is empty on a day, such as
  `call_date`, as the null type; Athena refuses a table whose partitions disagree. Every table is
  written with its column list from `bond_analytics/schemas.py`, which the DDL is tested against.
- **EventBridge passes no date to the engine.** The task runs `--mode daily` and works out its
  dates from the lake and its own watermark, so a retried or duplicated `SUCCEEDED` event finds
  nothing new rather than recomputing. South Africa keeps its own watermark, `engine_sa`.
- **The Treasury's site resets a connection now and then.** Its requests are tried three times,
  two and then four seconds apart; a 404 is not retried.

## Deploying it

1. Create the raw bucket `<raw-bucket>`, the lake bucket `<lake-bucket>` and a scripts bucket
   `<scripts-bucket>` in `<region>`.
2. Add GitHub's OIDC identity provider to the account: URL `https://token.actions.githubusercontent.com`,
   audience `sts.amazonaws.com`. Create three deploy roles, one per workflow, each trusting it with
   `token.actions.githubusercontent.com:aud` equal to `sts.amazonaws.com` and
   `token.actions.githubusercontent.com:sub` equal to `repo:Makayacine/Finance:ref:refs/heads/main`.
   The Lambda role needs `lambda:GetFunction`, `CreateFunction`, `UpdateFunctionCode` and
   `iam:PassRole` on the function's execution role; the Glue role `s3:PutObject` on the scripts
   bucket, `glue:GetJob`, `CreateJob`, `UpdateJob` and `iam:PassRole`; the ECS role ECR push on
   `<ecr-repository>`, `ecs:RegisterTaskDefinition`, `events:PutTargets` on the rule and
   `iam:PassRole` on the task's two roles and the rule's role.
3. Create the execution roles: the fetch Lambda's (`s3:PutObject` on `<raw-bucket>/raw/*`,
   `glue:StartJobRun` on the job), the Glue job's (read `<raw-bucket>`, read and write
   `<lake-bucket>`, read `<scripts-bucket>`), the engine task's (read `<raw-bucket>` for the
   backfill, read and write `<lake-bucket>`), the ECS execution role, and a role EventBridge
   assumes to `ecs:RunTask` the engine.
4. Create the ECR repository `<ecr-repository>`, the ECS cluster `<cluster>`, and a subnet and
   security group with outbound HTTPS for the task.
5. Set the repository variables: `SOVEREIGN_CURVES_AWS_REGION`, `..._RAW_BUCKET`,
   `..._LAKE_BUCKET`, `..._SCRIPTS_BUCKET`, `..._FETCH_FUNCTION`, `..._FETCH_EXECUTION_ROLE_ARN`,
   `..._GLUE_JOB`, `..._GLUE_EXECUTION_ROLE_ARN`, `..._ECR_REPOSITORY`,
   `..._ECS_EXECUTION_ROLE_ARN`, `..._ENGINE_TASK_ROLE_ARN`, `..._ECS_CLUSTER_ARN`,
   `..._EVENTS_ROLE_ARN`, `..._SUBNET_ID`, `..._SECURITY_GROUP_ID`. Set the three role variables,
   `SOVEREIGN_CURVES_LAMBDA_ROLE_ARN`, `..._GLUE_ROLE_ARN` and `..._ECS_ROLE_ARN`, last: each turns
   its deploy job on.
6. Create the rules: `aws events put-rule --cli-input-json file://eventbridge/fetch-schedule.json`
   with `fetch-schedule-targets.json` as its targets, filled in, plus `lambda add-permission` for
   `events.amazonaws.com`; and `put-rule` with `glue-succeeded-rule.json`, its job name filled in.
   The ECS workflow writes that rule's target on every deploy.
7. Push to `main`, or rerun each workflow: the Lambda, the Glue job and the image deploy in turn.
8. Run the backfill once as a task with the command overridden:
   `--mode backfill --start 2008-06-02 --end <yesterday>`. It pauses two seconds between
   FedInvest dates, and then fetches South Africa's 16 auction files and one debt schedule a year,
   two seconds apart. `--market sa --end <yesterday>` runs South Africa's alone.
9. Run `athena/create-tables.sql` in Athena with `<lake-bucket>` filled in.

Every AWS name here is account-specific and set by whoever deploys it.

## What the sample cannot say

- **It is 23 days.** The tables above are for the three truth-check dates and October 2026; the
  backfill fills in every day from June 2008, and the same tables run over all of them in Athena.
- **GSW is an answer key, not the truth.** It is fitted to off-the-run notes and bonds with its own
  exclusions; agreeing with it says the mechanics match theirs, and the residuals are the market's.
- **TIPS and FRNs are in the lake and out of the curves.** FedInvest gives no index ratio, and FIA
  has no floating leg.
- **South Africa's curve is a primary-market curve.** Each is two to four bonds' clearing yields on
  the day the Treasury sold them, joined by FIA's straight lines in the zero rate and flat beyond
  them, and the shortest bond auctioned since 2010 had 2.82 years left. Treasury bills and
  inflation-linked bonds, which the Treasury auctions too, are not loaded.
- **The OECD and IMF series are not a 10-year point.** They are monthly yields on bonds of 10 years
  and over; the check is that the curve's 10- and 15-year points bracket them and move with them.
