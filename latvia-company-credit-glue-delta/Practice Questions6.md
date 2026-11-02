**Question 1**: How do you read a company register and an insolvency file so that names, addresses and debtor identity columns never reach memory?

**Before Execution** (`[pd.read_csv(path, sep=";", nrows=0)]` | `[Header Inspection]`):

```text
register.csv: 129 MB, 21 columns in the header
insolvency_legal_person_proceeding.csv: 6 MB, 23 columns in the header
allow-listed: 5 of 21 and 8 of 23

```

**After Execution** (`[stage_batches.read_source(RAW, source)]` | `[Allow-Listed Frames]`):

```text
register: 489,966 rows x 5 columns, 149 MB, 2.7 s
insolvency: 18,053 rows x 8 columns, 10 MB, 0.2 s
columns outside the allow-list: 0 and 0

```

**Code**:

```python
# The allow-list is the only column source any reader is given
def read_source(folder, source):
    return pd.read_csv(
        source_path(folder, source), sep=";",
        usecols=list(config.SOURCE_COLUMNS[source]),
        dtype=str, keep_default_na=False, na_values=[""],
        encoding="utf-8-sig",
    )[list(config.SOURCE_COLUMNS[source])]

```

**Answer:** Keep one allow-list per source in `config.py` and pass it to `usecols` in the single reader every job, test and notebook uses. The 16 register and 15 insolvency columns outside it are never tokenised.

**Notes**:

* **Core Execution Mechanic:** `usecols` makes the C parser skip every unlisted field on every row, so the name, address and debtor identity text is never turned into Python strings.


* **Core Execution Mechanic (Dummy Translation):** The reader is handed a short shopping list and walks past every shelf that is not on it.


* **Boundary / Memory Constraint:** Five register columns of 21 hold 489,966 entities in 149 MB, all as text, before the type filter keeps 347,053 companies.


* **Boundary / Memory Constraint (Dummy Translation):** Reading a quarter of the columns keeps the whole register small enough for a laptop.


* **Failure Mode / Downstream Impact:** Dropping columns after a full read puts individual merchants' names in memory, in tracebacks and in any frame a debugger prints.


* **Failure Mode / Downstream Impact (Dummy Translation):** Sole traders' names are personal data, so deleting them after loading is already too late.



---

**Question 2**: How do you tell a bulk-load stamp from a real filing time in a `created_at` column?

**Before Execution** (`[created_at - year_ended_on, by fiscal year]` | `[Apparent Filing Lag]`):

```text
FY2008: median 4,242 days after the period end
FY2015: median 1,721 days | FY2019: 324 days
FY2021: median   185 days | FY2024: 139 days

```

**After Execution** (`[created_at.dt.normalize().value_counts()]` | `[Bulk Window Isolated]`):

```text
stamped before 2021-01-18: 1,152,314, 2020-08-11 to 2021-01-17
73 days over 3,000 stamps: 1,113,708, median 17,534 a day
busiest day since: 10,834 | real stamps: 833,804

```

**Code**:

```python
# Before the cut-off the stamp is only an upper bound on the filing date
created = pd.to_datetime(created_at, format="mixed").dt.normalize()
assumed = pd.to_datetime(year_ended_on) + pd.DateOffset(months=9)
bulk = created < pd.Timestamp("2021-01-18")
available = created.where(~bulk, np.minimum(created, assumed))

```

**Answer:** Profile the stamp by fiscal year and by day. A twelve-year filing lag and 17,534 statements a day are a load job, not filers, so date everything stamped before the cut-off nine months after its period end, never later than its stamp.

**Notes**:

* **Core Execution Mechanic:** `np.minimum` keeps the stamp when it is the earlier date, because no statement was filed after the Register loaded it.


* **Core Execution Mechanic (Dummy Translation):** A shelf label dated August 2020 says the report arrived by then, probably long before.


* **Boundary / Memory Constraint:** The cut-off is the day after the load's last stamp; 99.87% of the statements before it are FY2019 or earlier.


* **Boundary / Memory Constraint (Dummy Translation):** The rule stops the day real filing times begin, so nothing after it is guessed.


* **Failure Mode / Downstream Impact:** Read as filing times, the stamps leave every 2009-19 observation date with no accounts, so the training years would hold no scored company.


* **Failure Mode / Downstream Impact (Dummy Translation):** Taking the load date at face value hides ten years of accounts.



---

**Question 3**: How do you choose an embargo and an observation date from when companies actually file?

**Before Execution** (`[real stamps, 31 December year ends]` | `[Share Filed By 30 June]`):

```text
FY2020 50.7% | FY2021 45.1% | FY2022 84.5%
FY2023 85.3% | FY2024 88.4%
statements ending 31 December: 99.2%

```

**After Execution** (`[same rows, by 30 September]` | `[Share Filed By The Embargo]`):

```text
FY2020 93.1% | FY2021 91.6% | FY2022 92.9%
FY2023 93.2% | FY2024 95.1%
median days after year end: 180 195 142 145 144

```

**Code**:

```python
# Measured on the real stamps only, companies' own accounts
real = own[(own.created >= pd.Timestamp(config.BULK_LOAD_END)) & december
           & own.year.astype(int).between(2020, 2024)]
days = (real.created - real.ended).dt.days
filed = (days <= 273).groupby(real.year).mean()

EMBARGO_MONTHS = 9
OBSERVATION_MONTH, OBSERVATION_DAY = 10, 1

```

**Answer:** Measure how many companies have actually filed by each candidate date, on the years with real stamps. Nine months after a 31 December year end catches 91.6-95.1%, so observe on 1 October and embargo bulk-loaded statements by nine months.

**Notes**:

* **Core Execution Mechanic:** 273 days after 31 December is 30 September; that share holds at 91.6-95.1% across five years while June's runs 45.1-88.4%.


* **Core Execution Mechanic (Dummy Translation):** By the end of September nearly everyone has handed in last year's accounts, in slow years and fast ones.


* **Boundary / Memory Constraint:** A 1 July observation date would have caught only 45.1-50.7% of FY2020-21 accounts, scoring half the population on two-year-old figures.


* **Boundary / Memory Constraint (Dummy Translation):** Pick the date by when reports really arrive, not by when they are due.


* **Failure Mode / Downstream Impact:** An embargo shorter than real filing hands the training years accounts nobody could have read yet, so the test years disappoint.


* **Failure Mode / Downstream Impact (Dummy Translation):** If training pretends reports arrived early, the model learns from the future.



---

**Question 4**: How do you keep one statement per company and fiscal year when companies refile and file group accounts?

**Before Execution** (`[groupby(regcode, year).size()]` | `[Company-Years In The File]`):

```text
company statements: 1,708,249 | group statements: 4,412
company-years 1,702,893, of which 936 hold more than one:
183 refilings of one period | 753 two periods in one year

```

**After Execution** (`[glue-delta-merge.py --table statements]` | `[One Row Per Key]`):

```text
statements: 72 batches, 1703441 rows, 547 restated (0.03%)
group dropped 4,412 | in-batch duplicates cut 396
inserted 1,702,893: one row per company-year

```

**Code**:

```python
# Rank by the statement's own state: period end, filing time, id
order = ["year_ended_on", "created_at", "statement_id"]
window = Window.partitionBy("regcode", "fiscal_year").orderBy(
    *[F.col(c).desc_nulls_last() for c in order])
ranked = frame.withColumn("_rank", F.row_number().over(window))
kept = ranked.filter("_rank = 1").drop("_rank")

```

**Answer:** Drop group accounts by schema and source type, then keep the statement ranking last on period end, filing time and id, inside each batch and against the stored row.

**Notes**:

* **Core Execution Mechanic:** One three-term ordering ranks rows inside a batch with `row_number()` and against the stored row in the MERGE condition, so both choices agree.


* **Core Execution Mechanic (Dummy Translation):** Of two reports for one year, the newest for the latest period wins.


* **Boundary / Memory Constraint:** 547 statements arrived after the one they replace and restated it; 396 met their rival inside one batch.


* **Boundary / Memory Constraint (Dummy Translation):** Many second reports land with the first, so the winner is picked before writing.


* **Failure Mode / Downstream Impact:** Kept, a group statement filed after the parent's own wins its key, scoring the parent on its subsidiaries' assets and debts.


* **Failure Mode / Downstream Impact (Dummy Translation):** A consolidated report describes the whole family, not the one company scored.



---

**Question 5**: How do you put accounts filed in lats, in euros and in thousands of euros on one scale?

**Before Execution** (`[pd.crosstab(year, currency)]` | `[Reporting Units]`):

```text
FY2012: LVL 97,677 | EUR 0
FY2013: LVL 112,838 | EUR 280
FY2014: LVL 0 | EUR 122,149
rounded_to_nearest THOUSANDS: 133, all EUR

```

**After Execution** (`[same companies, adjacent years]` | `[Unit Checks]`):

```text
median FY2014 / FY2013 total assets: 1.4207 (46,681 companies)
1 / 0.702804 (the 2014 conversion rate): 1.4229
THOUSANDS x 1000 / neighbour year's ONES: median 1.146 (54)

```

**Code**:

```python
# One factor per statement, applied to every amount column
rate = (F.when(F.col("currency") == "LVL", F.lit(1 / config.LVL_PER_EUR))
        .otherwise(1.0)
        * F.when(F.col("rounded_to_nearest") == "THOUSANDS",
                 config.SCALE["THOUSANDS"]).otherwise(1.0))
for column in config.AMOUNTS:
    frame = frame.withColumn(column, (F.col(column) * rate).cast("double"))

```

**Answer:** Read the reporting currency and rounding off each statement, convert lats at the fixed rate and multiply thousands by 1,000 in the curation step. Check both against the same companies' neighbouring years before trusting either.

**Notes**:

* **Core Execution Mechanic:** The factor is built per row from two text columns, so a lats statement in thousands would get both multipliers at once.


* **Core Execution Mechanic (Dummy Translation):** Every report is translated into whole euros before it is compared with anything.


* **Boundary / Memory Constraint:** The median company grew 1.4207 times from FY2013 to FY2014, within 0.2% of the conversion factor, so the jump is the currency, not growth.


* **Boundary / Memory Constraint (Dummy Translation):** Balance sheets that seem to grow 42% overnight are lats becoming euros.


* **Failure Mode / Downstream Impact:** Left unconverted, the FY2008-13 amounts read about 30% low against the euro years, and the size bins learn the currency switch as if it were risk.


* **Failure Mode / Downstream Impact (Dummy Translation):** Mixing currencies makes every firm look 42% bigger in the year the money changed name.



---

**Question 6**: How do you define default from an insolvency register when some applications are refused?

**Before Execution** (`[proceedings.proceeding_form.value_counts()]` | `[Companies' Proceedings]`):

```text
INSOLVENCY 14,987 | LEGAL_PROTECTION 2,111
OUT_OF_COURT_LEGAL_PROTECTION 450
17,548 proceedings for 15,471 debtors

```

**After Execution** (`[panel.first_defaults(proceedings)]` | `[First Defaults]`):

```text
dismissed or left without action: 908
M01 649 | T18 189 | T07 35 | A09 29 | T19 6
companies with a default: 15,110
first via legal protection 1,991, later insolvent 1,199

```

**Code**:

```python
# The seven codes from the Register's own code sheet
DISMISSED = ("M01", "M12", "T07", "T18", "T19", "A09", "A12")

def first_defaults(proceedings, insolvency_only=False):
    kept = proceedings[~proceedings.proceeding_resolution_type.isin(DISMISSED)]
    if insolvency_only:
        kept = kept[kept.proceeding_form == "INSOLVENCY"]
    return pd.to_datetime(kept.groupby("regcode").proceeding_started_on.min())

```

**Answer:** Count the first start of any insolvency, legal-protection or out-of-court proceeding the court did not dismiss or leave without action, taking those codes from the Register's code sheet. Keep insolvency only as the sensitivity.

**Notes**:

* **Core Execution Mechanic:** The dismissed rows are removed before the minimum start date is taken, so a refused application never dates a default.


* **Core Execution Mechanic (Dummy Translation):** A court case that was thrown out is not a company going bust.


* **Boundary / Memory Constraint:** 1,199 of the 1,991 companies whose first default was legal protection entered insolvency later, which is why protection counts as default.


* **Boundary / Memory Constraint (Dummy Translation):** Most firms that ask the court to hold creditors off end up insolvent anyway.


* **Failure Mode / Downstream Impact:** Keeping the 649 dismissed 2008-12 insolvency applications labels companies the court refused to open a case against as defaults, in the crisis years the model trains on.


* **Failure Mode / Downstream Impact (Dummy Translation):** Counting refused applications blames firms for a creditor's failed attempt.



---

**Question 7**: How do you label a company-year so that nothing known later leaks into the population or the label?

**Before Execution** (`[today's insolvency register, filtered by date]` | `[Hindsight Label]`):

```text
labelled defaults, as of each horizon's end: 12,133
now carrying a dismissal code: 105

```

**After Execution** (`[panel.observation_rows(..., year)]` | `[Point-In-Time Rows]`):

```text
company-years 2009-25: 2,354,336 | as of 2026-10-30
population on the eve of 1 October, label on the eve a year on
started before 1 October, entered after it: 119

```

**Code**:

```python
# Population and accounts as known on the eve; the label a year later
obs = observation_date(year)
rows = observation_rows(
    lake.companies(eve(obs)), lake.statements(eve(obs), obs),
    lake.proceedings(eve(obs)),               # who is already in default
    lake.proceedings(eve(horizon_end(year))), # what became known in the horizon
    year)

```

**Answer:** Build each year's population, accounts and already-defaulted list from the tables as they stood on the eve of 1 October, and the label from the insolvency register as it stood on the eve of the horizon's end.

**Notes**:

* **Core Execution Mechanic:** Each read is a Delta version found by batch date, so an application open at the horizon's end counts even if dismissed later.


* **Core Execution Mechanic (Dummy Translation):** Every question is answered with the register as it looked on that day, not as it looks now.


* **Boundary / Memory Constraint:** 119 defaults started just before 1 October but were entered after it; they stay in the population and count in the year they became known.


* **Boundary / Memory Constraint (Dummy Translation):** A case filed on Friday and typed in on Monday belongs to the year in which anyone could see it.


* **Failure Mode / Downstream Impact:** Today's register would flip 105 labels to non-default and drop the 119 from the population.


* **Failure Mode / Downstream Impact (Dummy Translation):** Grading old decisions with today's answers makes history look tidier than it was.



---

**Question 8**: How do you measure what an assumed filing date costs when the training years have no real filing dates?

**Before Execution** (`[test years 2021-25, real filing dates]` | `[Scored As Filed]`):

```text
scored: 560,508 | AUC 0.8176 | defaulters scored 90.2%
last-year accounts only: 517,620 | AUC 0.8080

```

**After Execution** (`[same years, every statement dated +9 months]` | `[Scored The Bulk Way]`):

```text
scored: 574,547 | AUC 0.8182 | defaulters scored 93.1%
14,084 added, default rate 0.33% vs 0.26%
last-year accounts only: 554,964 | AUC 0.8151
38,200 added, default rate 0.41% vs 0.23%

```

**Code**:

```python
# The sensitivity: the test years re-dated the way 2009-20 are dated
frame = frame.withColumn("available_on",
                         F.add_months("year_ended_on", config.EMBARGO_MONTHS))

bulk = build_rows(lake, range(2021, 2026), redate=True)
out["bulk_dating"] = bulk_dating_cost(card, rows, bulk)

```

**Answer:** Take the years that do have real filing dates, score them once as filed and once re-dated by the assumed rule with the same card, and read the gap as what the rule does to the years that have no real dates.

**Notes**:

* **Core Execution Mechanic:** Only `available_on` changes between the two builds, so every difference in coverage, default rate and AUC is the dating.


* **Core Execution Mechanic (Dummy Translation):** Pretend you never had the real dates for the years where you do, and see how much better the model looks.


* **Boundary / Memory Constraint:** With a two-year accounts window a late filer is still scored, on older accounts, so the leak costs 0.0006 of AUC; on last year's accounts only it costs 0.0071.


* **Boundary / Memory Constraint (Dummy Translation):** Letting late filers in under their older reports keeps the leak small.


* **Failure Mode / Downstream Impact:** Testing on years dated by the same rule hides the leak completely, because training and test then share it.


* **Failure Mode / Downstream Impact (Dummy Translation):** Grade with the blind spot you trained with and you will never see it.



---

**Question 9**: How do you fit a logistic regression with company-clustered standard errors on a Glue runtime that ships no statsmodels?

**Before Execution** (`[python3 inside aws-glue-libs:5]` | `[What The Runtime Ships]`):

```text
numpy 2.2.6 pandas 2.2.2 pyarrow 17.0.0
ModuleNotFoundError: No module named 'statsmodels'
ModuleNotFoundError: No module named 'scipy'

```

**After Execution** (`[logit.fit(X, y, groups=regcode)]` | `[Agreement With statsmodels]`):

```text
700,965 rows | 150,781 companies | 11 terms
numpy: 8 Newton steps, 1.3 s
max |coef difference| 3.3e-16
max |clustered SE difference| 8.3e-17

```

**Code**:

```python
# Sum each company's score, then the sandwich with statsmodels' factor
scores = pd.DataFrame(X * (y - p)[:, None]).groupby(groups).sum().to_numpy()
clusters = scores.shape[0]
meat = scores.T @ scores
cov = H_inv @ meat @ H_inv * clusters / (clusters - 1) * (n - 1) / (n - k)
bse = np.sqrt(np.diag(cov))

```

**Answer:** Write Newton's method in numpy, sum the score within each company before forming the sandwich, and apply the same small-sample factor statsmodels uses. Check the two against each other outside the job, where statsmodels can be installed.

**Notes**:

* **Core Execution Mechanic:** The `groupby` collapses each company's rows into one score vector, so the variance counts 150,781 companies, not 700,965 rows.


* **Core Execution Mechanic (Dummy Translation):** A firm watched for several years is one witness, not several.


* **Boundary / Memory Constraint:** The information matrix is 11 by 11, so each Newton step is one pass over X and the fit needs no extra library and no cluster.


* **Boundary / Memory Constraint (Dummy Translation):** The heavy lifting is one tiny matrix per step, easily done on a Glue driver.


* **Failure Mode / Downstream Impact:** Clustering widens eight of the ten characteristics' errors, by up to 12%; naive p-values would keep terms on evidence the panel does not have.


* **Failure Mode / Downstream Impact (Dummy Translation):** Without clustering the model is more confident than its data allow.



---

**Question 10**: How do you keep a characteristic that the IV screen would drop when you know why its training IV is low?

**Before Execution** (`[woe_table(accounts_lag, y) by window]` | `[Late Filing, IV]`):

```text
train 2009-17:    IV 0.0157 (screen 0.02)
validate 2018-20: IV 0.0179
test 2021-25:     IV 0.0962
test default rate, year-before accounts 0.60% | last year 0.23%

```

**After Execution** (`[scorecard.fit(train, forced=("accounts_lag",))]` | `[Kept By Design]`):

```text
accounts_lag coef -0.557 | clustered p 8.4e-08
AUC with:    0.8008 | 0.8027 | 0.8176
AUC without: 0.8001 | 0.8012 | 0.8151

```

**Code**:

```python
# Forced past the IV screen, never past the sign and p-value checks
FORCED = ("accounts_lag",)
selected = [n for n in characteristics if binnings[n].iv >= MIN_IV or n in forced]
...
bad = table[(table.coef > 0) | (table.p_value > MAX_P)]

```

**Answer:** Exempt it from the IV screen only, by name, and still make it pass the sign and clustered p-value checks. Report the model without it beside, so the cost of the decision is on the page.

**Notes**:

* **Core Execution Mechanic:** The forced list bypasses `MIN_IV` alone; elimination still drops a forced term with a positive coefficient or a p-value over 0.05.


* **Core Execution Mechanic (Dummy Translation):** One variable is let past the first gate, but it must clear the same later checks as everyone else.


* **Boundary / Memory Constraint:** In 2009-17 the bulk dating shows a two-year-late statement as on time, so 5.3% of training rows look late against 7.7% in test.


* **Boundary / Memory Constraint (Dummy Translation):** The training years hide many late filers, so the signal looks weak there and strong where dates are real.


* **Failure Mode / Downstream Impact:** Letting the screen drop it costs 0.0025 of test AUC and loses a flag anyone can see without reading the accounts.


* **Failure Mode / Downstream Impact (Dummy Translation):** Dropping late filing because old data blurred it discards a warning sign that works today.



---

**Question 11**: How do you judge a PD model's ranking out of time when the default rate falls by two thirds?

**Before Execution** (`[windows by observation year]` | `[Three Windows]`):

```text
train 2009-17:    700,965 scored | 5,149 defaults | 0.73%
validate 2018-20: 346,494 scored | 1,253 defaults | 0.36%
test 2021-25:     560,508 scored | 1,455 defaults | 0.26%

```

**After Execution** (`[metrics.discrimination(y, score)]` | `[AUC With DeLong Intervals]`):

```text
train    0.8008 [0.7949, 0.8066] | equity ratio 0.6465
validate 0.8027 [0.7905, 0.8149] | equity ratio 0.6514
test     0.8176 [0.8066, 0.8285] | equity ratio 0.6542
test years one by one: 0.799 - 0.834

```

**Code**:

```python
# Fit on the first window only; every later number is out of time
card = scorecard.fit(covered[covered.window == "train"])
card.calibrate(covered[covered.window == "validate"])
for name in ("train", "validate", "test"):
    part = scored[scored.window == name]
    model = metrics.discrimination(part.y, part.score)
    bench = metrics.discrimination(part.y, part.benchmark)

```

**Answer:** Split by observation year, never at random, fit on the oldest window, and judge ranking with AUC and its DeLong interval against a one-ratio benchmark. Judge the level separately, year by year, because a shifting default rate moves calibration and leaves ranking alone.

**Notes**:

* **Core Execution Mechanic:** DeLong's variance comes from each default's and survivor's placement among the other group's scores, so no bootstrap is needed.


* **Core Execution Mechanic (Dummy Translation):** The interval says how much the score could wobble if the same years were drawn again.


* **Boundary / Memory Constraint:** The test interval is 0.022 wide; its 1,455 defaults, not its 560,508 rows, set the width.


* **Boundary / Memory Constraint (Dummy Translation):** A few hundred failures a year is what limits how precisely the model can be graded.


* **Failure Mode / Downstream Impact:** A random split puts one company's 2015 and 2016 rows on both sides, so the holdout scores firms the model has seen.


* **Failure Mode / Downstream Impact (Dummy Translation):** Testing on next year's copy of the same firms is marking homework already seen.



---

**Question 12**: How do you report calibration when the test years default at about a third of the training rate?

**Before Execution** (`[card.pd(frame, "ttc")]` | `[Through The Cycle]`):

```text
test mean PD 0.51% | observed 0.26%
O/E 2011-14: 0.92 - 1.10
O/E 2019-25: 0.44 - 0.58

```

**After Execution** (`[card.calibrate(validate)]` | `[Validation Level Beside It]`):

```text
log-odds shift: -0.5056
test mean PD 0.31% | observed 0.26%
O/E 2019-25: 0.73 - 0.95 | AUC unchanged

```

**Code**:

```python
# One constant on the log-odds: the mean PD over 2018-20 hits its rate
def intercept_shift(eta, target_rate, tol=1e-12):
    shift = 0.0
    for _ in range(100):
        p = sigmoid(eta + shift)
        gap = p.mean() - target_rate
        if abs(gap) < tol:
            break
        shift -= gap / max((p * (1 - p)).mean(), 1e-12)
    return shift

```

**Answer:** Keep the fitted level as through the cycle and add a second level, shifted by one constant so the validation years' mean PD equals their observed rate. Report both, with observed over expected by year, and never let the shift touch the ranking.

**Notes**:

* **Core Execution Mechanic:** Newton's method on one scalar moves every log-odds equally, so every score keeps its order and the AUC is identical.


* **Core Execution Mechanic (Dummy Translation):** The whole ladder is lowered by one rung; nobody changes places on it.


* **Boundary / Memory Constraint:** Score PSI stays under 0.10 every year but 2023, at 0.102: the companies look the same, and the rate moved.


* **Boundary / Memory Constraint (Dummy Translation):** The firms being scored resemble the ones the model learned from; they just fail less often now.


* **Failure Mode / Downstream Impact:** Booking only the fitted level would put 576 expected defaults on today's book where the recent years point nearer 350.


* **Failure Mode / Downstream Impact (Dummy Translation):** A model tuned to the crisis years rings the alarm too loudly for today.



---

**Question 13**: How do you replay a source that publishes only today's state as dated batches whose Delta MERGE restates rows?

**Before Execution** (`[data/raw/, companies only]` | `[Today's State Only]`):

```text
register: 347,053 companies, 203,938 terminated
insolvency: 17,548 proceedings, 806 still open
no earlier value of any row in any file

```

**After Execution** (`[run_chain.py --data raw]` | `[73 Quarterly MERGEs Per Table]`):

```text
companies: 73 batches, 498530 rows, 151477 restated (30.38%)
proceedings: 73 batches, 33303 rows, 15755 restated (47.31%)
statements: 72 batches, 1703441 rows, 547 restated (0.03%)
companies 2016-09-30 v31: 4971 rows, 2470 inserted, 2501 restated

```

**Code**:

```python
# A row whose state changed after it arrived is sent twice
arrival = batch_of(frame[arrive_on], last_batch, cadence).values
change = batch_of(frame[change_on], last_batch, cadence).values
later = frame[change_on].notna().values & (change > arrival)

first = frame.copy()
first.loc[later, list(changed_columns)] = None   # as it stood on arrival
restated = frame.loc[later].copy()                # as it stands after the change

```

**Answer:** Place every row in the batch of a date the file itself carries and send it again, changed, in the batch of the date it changed. A terminated company or an ended proceeding then comes back under its key, and the MERGE updates it instead of adding it.

**Notes**:

* **Core Execution Mechanic:** `registered` and `entry_created_on` date the arrival, `terminated` and `proceeding_ended_on` the restatement; the arrival copy has the changed columns blanked.


* **Core Execution Mechanic (Dummy Translation):** A company is delivered when it opens and again when it closes, so the table must update it.


* **Boundary / Memory Constraint:** A change inside its arrival quarter is sent once, already final, so 151,477 of 203,938 terminations come back as restatements.


* **Boundary / Memory Constraint (Dummy Translation):** Only changes that cross a quarter's end show up as updates.


* **Failure Mode / Downstream Impact:** An append-only load would hold 15,755 proceedings twice, open and closed, with nothing saying which was current.


* **Failure Mode / Downstream Impact (Dummy Translation):** Without updates the table keeps old and new side by side.



---

**Question 14**: How do you find the Delta version of a business date when every batch was committed on the same afternoon?

**Before Execution** (`[DeltaTable.forPath(spark, path).history()]` | `[Commit Times Of A Replay]`):

```text
proceedings: 73 commits, 13:56:22 to 14:00:26 (244 s)
timestampAsOf 2016-09-30: AnalysisException: The provided timestamp
(2016-09-30 00:00:00.0) is before the earliest version available ...

```

**After Execution** (`[lh.version_as_of(versions, day)]` | `[Versions By Batch Date]`):

```text
version_as_of(2016-09-30) = 31: 12,473 rows, 1,165 open
sample, as of 2016-09-30: version 8, 743 rows, 105 open
after merging 2017-09-30: version 8, 743 rows, identical: True
latest version: 831 rows; of the 105, 57 now ended

```

**Code**:

```python
# The batch date rides in each commit; look versions up by it
spark.conf.set("spark.databricks.delta.commitInfo.userMetadata", note)

rows = DeltaTable.forPath(spark, path).history().select(
    "version", "userMetadata").collect()
pairs = sorted((date.fromisoformat(json.loads(r.userMetadata)["batch_date"]),
                r.version) for r in rows if r.userMetadata)
version = max(v for batch, v in pairs if batch <= day)
frame = spark.read.format("delta").option("versionAsOf", version).load(path)

```

**Answer:** Write the batch date into every commit's `userMetadata` and pick the newest version dated on or before the day asked for. A version is a fixed snapshot, so a later MERGE adds versions and never changes an earlier view.

**Notes**:

* **Core Execution Mechanic:** `history()` returns each commit's `userMetadata`; a re-run's second version under one batch date wins as the later.


* **Core Execution Mechanic (Dummy Translation):** Each delivery is stamped with its business day, and the reader asks for that day.


* **Boundary / Memory Constraint:** Both retention periods are 36,500 days; Delta's 30-day default would lose the early versions a month after the replay.


* **Boundary / Memory Constraint (Dummy Translation):** The history is kept for a century, so the 2008 view stays readable.


* **Failure Mode / Downstream Impact:** `timestampAsOf` refuses every business date before the first commit, and after a backfill returns whatever committed at that wall-clock time.


* **Failure Mode / Downstream Impact (Dummy Translation):** The clock says when the data was loaded, not which day it describes.



---

**Question 15**: How do you make a Glue MERGE job refuse a batch with a file missing or a header reordered?

**Before Execution** (`[stage_batches.py --local --data sample]` | `[Two Broken Batches]`):

```text
statements to 2012-09-30: 22 files staged
deleted income_statements_2011-09-30.csv
register to 2010-09-30, batch 2010-09-30 header:
regcode;type;terminated;registered;closed

```

**After Execution** (`[glue-delta-merge.py --local]` | `[Refused, Left In raw_zone]`):

```text
FileNotFoundError: batch 2011-09-30: income_statements_2011-09-30.csv
missing from raw_zone | 21 files left, 0 archived, no table
companies v0, v1 committed, then IllegalArgumentException:
CSV header does not conform to the schema.
Expected: registered but found: terminated

```

**Code**:

```python
# Every batch must hold all its files before the first MERGE runs
missing = [s for s in sources if day not in pending[s]]
if missing:
    raise FileNotFoundError(
        f"batch {day}: {', '.join(f'{m}_{day}.csv' for m in missing)} missing "
        f"from raw_zone; the batch is left where it is")

return spark.read.csv(uri, schema=SCHEMAS[source], sep=";", header=True,
                      enforceSchema=False, mode="FAILFAST")

```

**Answer:** Check every waiting batch for all its files before merging any, and read each file with a declared schema whose header must match by name. Either failure raises, so nothing past it is merged or archived.

**Notes**:

* **Core Execution Mechanic:** `enforceSchema=False` compares the header with the schema's field names, so swapped columns fail at the first action.


* **Core Execution Mechanic (Dummy Translation):** The job reads the column labels and stops if they are not in the order it expects.


* **Boundary / Memory Constraint:** The file check covers every pending batch up front, so one missing file holds back all 21, earlier complete batches included.


* **Boundary / Memory Constraint (Dummy Translation):** A gap anywhere stops the run before it starts.


* **Failure Mode / Downstream Impact:** With the default `enforceSchema=True` Spark reads by position, so a truly reordered extract stores termination dates as registration dates, silently.


* **Failure Mode / Downstream Impact (Dummy Translation):** Reading by position alone swaps birth and death dates without an error.



---

**Question 16**: How do you MERGE a batch that holds two rows for one key, scanning only its partitions?

**Before Execution** (`[curate_statements(), batch 2015-09-30]` | `[Repeated Keys In One Batch]`):

```text
copy restored to v25: 355,378 rows | batch: 104,695
93 keys twice, 0 of them already in the table

```

**After Execution** (`[bare MERGE vs glue-delta-merge.py]` | `[Three Outcomes]`):

```text
bare MERGE: committed, keys held twice: 93
the job: inserted 104,584, duplicates_dropped 93, twice: 0
stored key repeated, bare: UnsupportedOperationException:
Cannot perform Merge as multiple source rows matched ...
2025-09-30, IN-list on / off: scan 2.3-2.4 s / 2.2 s

```

**Code**:

```python
# One winner per key, by the MERGE's own ranking, before the MERGE
window = Window.partitionBy(*key).orderBy(*[t.desc_nulls_last() for t in terms])
kept = frame.withColumn("_rank", F.row_number().over(window)).filter("_rank = 1")

# Name the batch's partitions so the match scan can skip the rest
years = sorted({r[0] for r in frame.select("fiscal_year").distinct().collect()})
condition += f" AND t.fiscal_year IN ({', '.join(str(y) for y in years)})"

```

**Answer:** Cut each batch to one row per key with the ranking the MERGE uses, because Delta refuses repeated keys only when they match a stored row and inserts new ones twice. Name the batch's partitions in the join condition.

**Notes**:

* **Core Execution Mechanic:** `row_number()` over (regcode, fiscal_year) by period end, filing time and id keeps rank 1; the dropped count goes in the commit note.


* **Core Execution Mechanic (Dummy Translation):** Two reports for one year in one delivery: keep the newest first.


* **Boundary / Memory Constraint:** On the 165-file copy the IN-list saved nothing measurable, and the MERGE removed 2 files and added 12 either way.


* **Boundary / Memory Constraint (Dummy Translation):** At this size skipping folders saves nothing; the list is there for when the table grows.


* **Failure Mode / Downstream Impact:** The bare MERGE commits cleanly and leaves 93 company-years with two statements, which every later read of the table sees.


* **Failure Mode / Downstream Impact (Dummy Translation):** The table accepts the double delivery without a word.



---

**Question 17**: How do you archive a merged batch so that a crash after the Delta commit is safe to re-run?

**Before Execution** (`[archived/ replaced by a file]` | `[Move Blocked After Commit]`):

```text
proceedings to 2011-09-30: v0-v3 committed, archived
batch 2012-09-30 staged | archived/ is now a file
NotADirectoryError: [Errno 20] Not a directory
commits: v0 ... v4 | raw_zone: ..._2012-09-30.csv

```

**After Execution** (`[glue-delta-merge.py --local, re-run]` | `[No-Op MERGE, Then Archive]`):

```text
proceedings 2012-09-30 v5: rows 141, inserted 0
restated 0, unchanged 141
raw_zone: [] | archived: ..._2012-09-30.csv

```

**Code**:

```python
# Commit first, archive after, and let a failed move raise
version, metrics = lh.merge_batch(spark, path, frame, spec["key"],
                                  spec["order"], spec["values"], note)
for source, key in keys.items():
    bucket.move(key, f"{lh.ARCHIVED}/{source}/{key.rsplit('/', 1)[-1]}")

# S3: no rename, so a move is a copy and then a delete
self.client.copy_object(Bucket=self.bucket, Key=target,
                        CopySource={"Bucket": self.bucket, "Key": source})
self.client.delete_object(Bucket=self.bucket, Key=source)

```

**Answer:** Move a batch only after its MERGE has committed and let a failed move raise. A batch left behind then MERGEs again as a no-op, because the conditional update finds nothing newer, and is archived on that run.

**Notes**:

* **Core Execution Mechanic:** All 141 rows match stored rows of equal rank and equal values, so the update condition is false and the commit changes nothing.


* **Core Execution Mechanic (Dummy Translation):** Delivering the same box twice leaves the shelf as it was.


* **Boundary / Memory Constraint:** On S3 a crash between `copy_object` and `delete_object` leaves the batch in both prefixes; the re-run copies it onto the same archive key.


* **Boundary / Memory Constraint (Dummy Translation):** The cloud cannot move a file in one step, so the second attempt simply finishes the first.


* **Failure Mode / Downstream Impact:** Swallowing the move error leaves a merged batch in `raw_zone/` looking unmerged, with nothing saying archiving has stopped.


* **Failure Mode / Downstream Impact (Dummy Translation):** A silent failure keeps the job green while the to-do pile quietly grows.