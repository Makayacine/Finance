**Question 1**: How do you read a 762 MB multi-file CSV extract so that personal identifiers never reach memory?

**Before Execution** (`[pd.read_csv(path, nrows=0)]` | `[Header Inspection]`):

```text
3 CSV files | 42 columns each | 319 + 255 + 188 MB
borrower identity columns in each header: 4

```

**After Execution** (`[sba.read_loans(paths)]` | `[Materialized Loan Frame]`):

```text
rows: 1,639,728 | columns: 19 (18 read + loan_id)
memory: 1.18 GB
borrower identity columns in frame: 0

```

**Code**:

```python
# Header first, then usecols: the identity columns are never parsed
header = pd.read_csv(path, nrows=0).columns
assert not set(READ_COLUMNS) & set(BORROWER_COLUMNS)
frame = pd.read_csv(path, usecols=list(READ_COLUMNS),
                    dtype={"FranchiseCode": "str"}, low_memory=False)

```

**Answer:** Read only the header, confirm your column allow-list holds no borrower field, and pass that list to `usecols`. The parser skips the identity columns, so they never exist as Python objects.

**Notes**:

* **Core Execution Mechanic:** `nrows=0` parses the header line alone, and `usecols` then tells the CSV tokenizer to convert only the 18 listed fields on every row.


* **Core Execution Mechanic (Dummy Translation):** You read the label on the box, tick the items you want, and the reader never takes the names and addresses out at all.


* **Boundary / Memory Constraint:** Keeping 18 of 42 columns holds 1,639,728 rows in 1.18 GB, small enough for one Fargate task.


* **Boundary / Memory Constraint (Dummy Translation):** Taking fewer than half the columns is what lets a 762 MB extract fit on one small cloud machine.


* **Failure Mode / Downstream Impact:** Calling `drop(columns=...)` after a full read loads the identifying values first, where a debug dump or traceback can expose them.


* **Failure Mode / Downstream Impact (Dummy Translation):** Some borrowers are sole proprietors, so deleting names after loading is too late; they were already sitting in memory.



---

**Question 2**: How do you validate inferred dtypes exactly while still accepting nullable integers that pandas widens to float?

**Before Execution** (`[pd.read_csv(file)]` | `[Inferred Dtypes Per File]`):

```text
LocationID   FY2000_FY2009    float64   16 blanks
LocationID   FY2020_Present   int64      0 blanks
CERT         failures.csv     float64  488 blanks

```

**After Execution** (`[validate_file()]` | `[Landed File Verdicts]`):

```text
8 of 8 landed files valid
new/fdic_failures/failures.csv: valid, 4,117 rows, ok

one CERT overwritten with text: CERT is str, declared float64

```

**Code**:

```python
# Exact, except that a float64 declaration also accepts int64
def dtype_matches(inferred, declared):
    return inferred == declared or (declared == "float64" and inferred == "int64")

wrong = [
    f"{column} is {frame[column].dtype}, declared {declared}"
    for column, declared in entity.dtypes.items()
    if not dtype_matches(str(frame[column].dtype), declared)
]

```

**Answer:** Compare each inferred dtype with its declaration as an exact string, allowing one widening: `float64` also accepts `int64`. Declare any integer column that can hold a blank as `float64`.

**Notes**:

* **Core Execution Mechanic:** `low_memory=False` infers each column once over the whole file, and the dtype string is then compared with the entity's dictionary.


* **Core Execution Mechanic (Dummy Translation):** pandas guesses each column's type, and we check that guess against a written contract.


* **Boundary / Memory Constraint:** NumPy `int64` has no slot for a missing value, so 488 blank `CERT` cells turn that whole column into `float64`.


* **Boundary / Memory Constraint (Dummy Translation):** One empty cell makes pandas store every number as a decimal, so declaring it a decimal stops good files being rejected.


* **Failure Mode / Downstream Impact:** The widening runs one way only, so text in a numeric column infers `str`, fails, and leaves the file in `new/`.


* **Failure Mode / Downstream Impact (Dummy Translation):** Whole numbers may pass as decimals, but a word in a number column is stopped at the door.



---

**Question 3**: How do you prove that a field which looks like an origination attribute is leaking the outcome?

**Before Execution** (`[sba.fit_pd(book)]` | `[Scorecard With And Without The Field]`):

```text
holdout AUC, 10 origination characteristics:        0.623
holdout AUC, term length and whole-year flag added: 0.915

```

**After Execution** (`[whole_year.groupby(company.LoanStatus)]` | `[Leak Evidence]`):

```text
LoanStatus   whole-year term share
CHGOFF       0.0892
EXEMPT       0.7597
P I F        0.8342

resolved loans charged off:
term exactly 120 or 240:     0.53%
term 85-119 or 181-239:     31.53%

```

**Code**:

```python
# A term fixed at approval cannot depend on how the loan ended
company = extra[extra.BusinessType.isin(sba.COMPANY) & (extra.ApprovalFY <= 2019)
                & extra.LoanStatus.isin(["P I F", "CHGOFF", "EXEMPT"])]
whole_year = (company.TermInMonths % 12 == 0) & (company.TermInMonths > 0)
display(whole_year.groupby(company.LoanStatus).mean().rename("whole-year term share").to_frame())

```

**Answer:** Compare the field's distribution across final outcomes instead of trusting its name. `TermInMonths` is a whole number of years on 83.4% of paid-off loans and 8.9% of charged-off ones, so charge-off rewrites it.

**Notes**:

* **Core Execution Mechanic:** Groups a boolean "divides by 12" flag by final status, because a value frozen at approval cannot vary with a status assigned years later.


* **Core Execution Mechanic (Dummy Translation):** Loans are written for round terms like 10 or 20 years, so if almost no failed loan has one, someone changed it afterwards.


* **Boundary / Memory Constraint:** The check needs five columns through `usecols` and no model.


* **Boundary / Memory Constraint (Dummy Translation):** You train nothing to catch this; a quick count on a handful of columns shows it.


* **Failure Mode / Downstream Impact:** Left in, the field lifts holdout AUC from 0.623 to 0.915, yet a new loan still carries its approval term, so the score fails on live data.


* **Failure Mode / Downstream Impact (Dummy Translation):** The model looks brilliant in testing because it is reading the answer, then falls apart on new loans.



---

**Question 4**: How do you fit a points scorecard that ranks loans within a vintage while keeping crisis-year levels out of the score?

**Before Execution** (`[resolved.groupby("vintage")]` | `[Vintage Level Swing]`):

```text
charge-off rate, resolved loans: FY2002 13.4% | FY2007 35.5% | FY2009 14.7%
train 319,213 | holdout 136,807 | 10 characteristics

```

**After Execution** (`[sba.fit_pd(book)]` | `[Holdout Discrimination]`):

```text
converged in 7 iterations | training scores 335 to 800

holdout FY2000-2009   AUC 0.623 | Gini 0.246 | KS 0.174
single vintages       AUC 0.560 to 0.639
FY2010, next vintage  AUC 0.601 | Gini 0.202 | KS 0.144

```

**Code**:

```python
# The vintage dummies are fitted, then left out of the points
if level is not None:
    values = sorted(frame[level].astype(str).unique())
    card.level_column, card.level_reference, card.level_values = level, values[0], values[1:]
X = card.design(frame)
model = sm.Logit(np.asarray(y, dtype=float), X)

```

**Answer:** Add the approval vintage to the logistic regression as level dummies, then leave those coefficients out of the 300-850 points. The hazard curve supplies the level instead.

**Notes**:

* **Core Execution Mechanic:** `design()` builds WoE-ordered dummies against each feature's riskiest bin plus the vintage dummies, and `_scale_points` converts only the feature coefficients.


* **Core Execution Mechanic (Dummy Translation):** While fitting, the year takes the blame for the crisis, and that part is thrown away before scoring.


* **Boundary / Memory Constraint:** Ordered bins under 0.2% of training rows fold into their nearest neighbour, so the 33 training loans above $2m score as `1-2m`.


* **Boundary / Memory Constraint (Dummy Translation):** A few dozen loans cannot support their own coefficient, so a tiny bin joins the size next to it.


* **Failure Mode / Downstream Impact:** Without the level dummies, a feature common in FY2007 absorbs the crisis, so the score rewards the calendar rather than the borrower.


* **Failure Mode / Downstream Impact (Dummy Translation):** Anything popular in 2007 would look dangerous just because of when those loans were made.



---

**Question 5**: How do you detect that the population being scored has drifted away from the one a scorecard was built on?

**Before Execution** (`[PDModel.band_edges]` | `[Frozen Development Bins]`):

```text
reference: 319,213 training scores
edges: 480, 501, 516, 529, 542, 558, 580

```

**After Execution** (`[sba.psi_by_vintage(model, book)]` | `[Score PSI By Vintage]`):

```text
vintage   loans    psi
2007      67,117   0.017
2014      40,532   0.190
2017      51,065   0.055
2021      42,697   0.190
2025      58,464   0.020

```

**Code**:

```python
# Same edges for both populations, floored so an empty bin cannot blow up the log
e = np.histogram(np.asarray(expected, dtype=float), bins=edges)[0].astype(float)
a = np.histogram(np.asarray(actual, dtype=float), bins=edges)[0].astype(float)
e = np.clip(e / e.sum(), 1e-6, None)
a = np.clip(a / a.sum(), 1e-6, None)
return float(np.sum((a - e) * np.log(a / e)))

```

**Answer:** Compute the Population Stability Index of each vintage's scores against the training scores on fixed bins. Under 0.10 is stable and above 0.25 is a population the model was not built on.

**Notes**:

* **Core Execution Mechanic:** PSI sums `(a - e) * ln(a / e)` over bin shares, with edges frozen from the training scores so every vintage faces one reference.


* **Core Execution Mechanic (Dummy Translation):** You compare the share of loans in each score bucket then and now; bigger shifts mean a bigger number.


* **Boundary / Memory Constraint:** PSI needs scores and no outcomes, so it works on FY2025 and FY2026 loans that have barely begun to charge off.


* **Boundary / Memory Constraint (Dummy Translation):** You do not wait years to learn who failed; the alarm can ring on approval day.


* **Failure Mode / Downstream Impact:** No vintage reaches 0.25, but seven sit between 0.10 and 0.19 and hold 16% of open exposure, so their ranking needs watching.


* **Failure Mode / Downstream Impact (Dummy Translation):** About a sixth of the money sits in loans noticeably unlike the ones the model learned from, so trust their order less.



---

**Question 6**: How do you estimate a lifetime charge-off probability when loans can also leave by paying off?

**Before Execution** (`[book.outcome]` | `[Three Exit Doors]`):

```text
loans in the life table: 1,152,055
left before 2005-08-01, excluded: 46,277
event      CHGOFF    139,784
competing  P I F     731,194
censored   EXEMPT    281,077

```

**After Execution** (`[curve.conditional_incidence(zero)]` | `[Lifetime PD From Origination]`):

```text
months on the curve: 277
lifetime PD, payoff competing: 15.8%
lifetime PD, payoff censored:  49.1%
median months to charge-off:   49.8

```

**Code**:

```python
# Both exits shrink survival; only charge-offs add to incidence
he, hc = self.scaled(event_multiplier, competing_multiplier)
survival = np.concatenate([[1.0], np.cumprod(1.0 - he - hc)])
incidence = np.concatenate([[0.0], np.cumsum(survival[:-1] * he)])
return survival, incidence

```

**Answer:** Build a monthly life table with charge-off as the event, payoff as the competing exit and open loans censored, entering loans open on 2005-08-01 at their age then. Cumulative incidence is the running sum of survival times event hazard.

**Notes**:

* **Core Execution Mechanic:** `np.bincount` counts entries and exits per month of age, and at-risk is the entries so far minus the exits before that month.


* **Core Execution Mechanic (Dummy Translation):** Each month you count the loans that have joined and not yet left, and a loan older than the records joins on the day they start.


* **Boundary / Memory Constraint:** The curve stops at month 277, where 499 loans remain, under the floor of 500, so one charge-off would move the hazard 0.2 points.


* **Boundary / Memory Constraint (Dummy Translation):** So few very old loans remain that one failure would swing the numbers, so the chart ends rather than guesses.


* **Failure Mode / Downstream Impact:** Censoring payoff assumes repaid loans keep carrying hazard, which inflates the lifetime PD from 15.8% to 49.1%.


* **Failure Mode / Downstream Impact (Dummy Translation):** Pretending repaid loans could still fail triples the estimate, counting losses on loans that no longer exist.



---

**Question 7**: How do you turn one pooled hazard curve into a 12-month PD and a lifetime PD for every score band?

**Before Execution** (`[hazard.band_multipliers()]` | `[Observed Over Expected]`):

```text
band   observed   expected   multiplier
A        12,781     24,604   0.52
D        16,263     16,667   0.98
H        24,050     13,036   1.84

```

**After Execution** (`[curve.conditional_incidence()]` | `[Band D By Loan Age]`):

```text
months open   12-month PD   remaining lifetime PD
  0            0.15%         15.49%
 48            3.47%         14.16%
120            4.06%         13.02%

```

**Code**:

```python
# Incidence gained over the window, given the loan is still open at this age
survival, incidence = self.incidence_table(event_multiplier, competing_multiplier)
age = np.clip(np.asarray(age, dtype=int), 0, self.max_age)
end = self.max_age if horizon is None else np.clip(age + horizon, 0, self.max_age)
base = survival[age]
return np.divide(incidence[end] - incidence[age], base, out=np.zeros(len(age)),
                 where=base > 0)

```

**Answer:** Scale the pooled hazard by each band's observed-over-expected ratio, then take incidence over the window given the loan is still open. Twelve months gives the Basel PD; the end of the curve gives the IFRS 9 lifetime PD.

**Notes**:

* **Core Execution Mechanic:** `cumulative_hazard` gives each loan's expected charge-offs, and a band's observed total over that expected total is its `event_multiplier`.


* **Core Execution Mechanic (Dummy Translation):** A multiplier of 0.52 means band A fails at 52% of the average rate.


* **Boundary / Memory Constraint:** This is a proportional-hazards assumption: every band shares one curve shape and only the level moves.


* **Boundary / Memory Constraint (Dummy Translation):** All eight bands use one curve stretched up or down, which keeps thin bands stable.


* **Failure Mode / Downstream Impact:** Reading PD at origination gives 0.15% by exposure, because charge-off lags default, while the open book at its current age averages 2.86%.


* **Failure Mode / Downstream Impact (Dummy Translation):** A new loan almost never reaches charge-off in year one, so treating every loan as new understates the risk about nineteen times.



---

**Question 8**: How do you apply IFRS 9 staging when a file shows no payment history for any open loan?

**Before Execution** (`[hazard_model.experience]` | `[Vintages Against The Curve]`):

```text
vintage   open   observed   expected   O/E    p-value
2006       384     20,068     10,615   1.89   <0.0001
2007       470     23,667     11,038   2.14   <0.0001
2008       547     14,692      8,616   1.71   <0.0001

```

**After Execution** (`[sba.live_book()]` | `[Collective Staging]`):

```text
stage 2 vintages: [2007]
stage 1   280,607 loans   ECL $3,267.8m   12-month
stage 2       470 loans   ECL $5.0m       lifetime
stage 3         0 loans

```

**Code**:

```python
# At least twice the expected charge-offs, and too many to be chance
hit = (experience.observed_over_expected >= STAGE2_RELATIVE) & (
    experience.p_value < STAGE2_P_VALUE)
return sorted(int(v) for v in experience.index[hit])

```

**Answer:** Assess the increase in credit risk collectively, on the loan's vintage. A vintage whose charge-offs ran at least twice the calibrated expectation, with a Poisson p-value under 0.01, moves its open loans to stage 2.

**Notes**:

* **Core Execution Mechanic:** `poisson.sf(observed - 1, expected)` is the chance of at least this many charge-offs if the curve were right.


* **Core Execution Mechanic (Dummy Translation):** The curve expected about 11,000 FY2007 failures and 23,667 happened, which is far too many for luck.


* **Boundary / Memory Constraint:** Every open loan is `EXEMPT`, with no days past due or modification flag, so IFRS 9's collective route (B5.5.1-B5.5.6) is the one available.


* **Boundary / Memory Constraint (Dummy Translation):** We cannot see how any single open loan is paying, so we judge it by its year group.


* **Failure Mode / Downstream Impact:** A score-band rule cannot move a loan to stage 2, because a band is fixed at origination and shows no increase since then.


* **Failure Mode / Downstream Impact (Dummy Translation):** Staging asks whether risk got worse, not whether it was high, and a day-one grade cannot say.



---

**Question 9**: How do you model a loss ratio bounded between 0 and 1 when some raw values break the bound?

**Before Execution** (`[sba.severity_frame(book)]` | `[Raw Severity]`):

```text
company charge-offs with an amount: 143,789
above 1: 4,482 (q99 1.62, max 464.0)
capped at 1: mean 0.744, median 0.820

```

**After Execution** (`[sba.fit_severity()]` | `[Scored Out Of Time]`):

```text
trained on FY2000-09, scored on 30,536 FY2010-19 charge-offs

                       MAE     RMSE    R2 vs constant
constant 0.746         0.208   0.256   -
fractional logit       0.190   0.238   0.135
  + age at charge-off  0.168   0.218   0.275

```

**Code**:

```python
# Age at charge-off is one more term
X["age_band"] = age_band(charged.exit_age)
model.result = sm.GLM(np.asarray(y, float), design,
                      family=sm.families.Binomial()).fit(cov_type="HC1")

```

**Answer:** Cap the ratio at 1 under one stated rule, then fit a fractional logit, with the age at charge-off as a term: a binomial GLM on a 0-1 outcome with robust errors. Judge it out of time.

**Notes**:

* **Core Execution Mechanic:** The Papke-Wooldridge fractional logit is a binomial GLM maximising a Bernoulli quasi-likelihood, `cov_type="HC1"` gives sandwich errors, and the age enters as ten bands.


* **Core Execution Mechanic (Dummy Translation):** Ordinary regression can predict losing 130% of a loan; the logit curve keeps every prediction between 0% and 100%.


* **Boundary / Memory Constraint:** The target is conditional on charge-off and gross of recoveries, because the file holds no balance at default.


* **Boundary / Memory Constraint (Dummy Translation):** This says how much was written off once a loan had failed, before any money came back; a young failure loses more.


* **Failure Mode / Downstream Impact:** A Bernoulli variance has no room above 1, so the sensitivity recalibrates the level: uncapped values move 12-month EL from $3.270bn to $3.383bn.


* **Failure Mode / Downstream Impact (Dummy Translation):** A few strange records reach 464 times the loan, so they are capped, and changing that rule moves total loss by only 3.5%.



---

**Question 10**: How do you compute expected loss and IRB capital for every open loan when balance at default is unknown?

**Before Execution** (`[sba.live_book()]` | `[One Median-Sized Open Loan]`):

```text
FY2022 | band D | $250,000 original
pd_12m 3.412% | severity 0.672
el_12m $5,728 | capital_k 0.159

```

**After Execution** (`[sba.book_summary(live)]` | `[Open Book]`):

```text
open loans 281,077 | exposure $165.06bn
12-month EL $3.27bn | lifetime EL $15.61bn
capital $25.3bn | RWA $316.1bn
formula check, PD 1% and 45% severity: risk weight 92.32%

```

**Code**:

```python
# Each age band prices its own severity
open_["pd_12m"], loss_12m = priced(12)
open_["severity"] = np.divide(loss_12m, open_.pd_12m.values, out=today,
                              where=open_.pd_12m.values > 0)
open_["el_12m"] = loss_12m * open_.exposure
open_["capital_k"] = capital.irb_capital(open_.pd_12m, open_.severity)
open_["rwa"] = 12.5 * open_.capital_k * open_.exposure

```

**Answer:** Split each loan's PD by the age its charge-off would come at, price each slice with that age's severity, and multiply by the original amount. The implied 12-month severity feeds the IRB formula.

**Notes**:

* **Core Execution Mechanic:** EL sums, over ten age bands, the band's charge-off PD times its severity; `severity` is that sum over the 12-month PD, so `el_12m = pd_12m * severity * exposure` still holds.


* **Core Execution Mechanic (Dummy Translation):** Chance it fails at each age, times the share lost then, times the loan size; capital is the cushion for a very bad year.


* **Boundary / Memory Constraint:** Each formula slot takes what the file has: PD floored at 0.05%, the implied severity, the original amount, and M = 2.5.


* **Boundary / Memory Constraint (Dummy Translation):** Basel's formula wants inputs this file lacks, so each slot takes the closest measured quantity.


* **Failure Mode / Downstream Impact:** The SME adjustment needs sales the file lacks and SBA's guarantee on 75.1% of exposure is not applied, so RWA runs at 191.5% of exposure.


* **Failure Mode / Downstream Impact (Dummy Translation):** A small shop is treated like a big company, and the guaranteed part is charged in full, so capital comes out high.



---

**Question 11**: How do you label bank failures on a yearly panel without leaking the outcome through filing lag?

**Before Execution** (`[fdic.failure_events(failures)]` | `[Raw Event Tables]`):

```text
year-end filings: 413,302
failures table: 3,524 FAILURE, 593 ASSISTANCE rows

```

**After Execution** (`[panel.drop_reason.value_counts()]` | `[Label Waterfall]`):

```text
kept                       390,443   (2,710 failures)
censored exit               15,986
horizon not yet observed     4,403
assisted                     1,503
embargo                        749
after failure                  218

```

**Code**:

```python
# Later lines win, so the order below is the precedence, weakest first.
reasons = pd.Series("kept", index=panel.index)
reasons[end > observed_through] = "horizon not yet observed"
reasons[panel.assist_date.notna() & (panel.assist_date <= end)] = "assisted"
reasons[panel.fail_date.notna() & (panel.fail_date <= panel.repdte)] = "after failure"
reasons[(panel.fail_date > panel.repdte) & (panel.fail_date <= start)] = "embargo"

```

**Answer:** Start each filing's 12-month horizon one quarter after its report date, and give every excluded row a named reason. Only a `FAILURE` row is a default.

**Notes**:

* **Core Execution Mechanic:** `hit` tests the failure date against `(REPDTE + 3m, REPDTE + 15m]`, and the ordered assignments give each row one `drop_reason`.


* **Core Execution Mechanic (Dummy Translation):** Each row asks whether the bank failed in the year after its report went public, and every skipped row records why.


* **Boundary / Memory Constraint:** The embargo exists because a December call report is filed well into the next quarter. Three of the 4,403 "horizon not yet observed" rows are banks that failed inside it.


* **Boundary / Memory Constraint (Dummy Translation):** A report filed in February cannot warn you about a bank that closed in January, and an unfinished year cannot say who survived.


* **Failure Mode / Downstream Impact:** Citibank N.A. and Bank of America N.A. appear as `ASSISTANCE` and are open, so counting assistance as failure labels survivors as defaults.


* **Failure Mode / Downstream Impact (Dummy Translation):** A rescue is not a collapse; treating rescued giants as failed banks teaches the model the wrong lesson.



---

**Question 12**: How do you backtest a failure model and its dollar expected loss out of time against what actually happened?

**Before Execution** (`[fdic.WINDOWS]` | `[Out-Of-Time Split]`):

```text
train      1985-2007   281,076 bank-years   2,167 failures
validate   2008-12      38,972 bank-years     466 failures
test       2013-25      70,395 bank-years      77 failures

```

**After Execution** (`[fdic.el_backtest()]` | `[Ranking And Dollars]`):

```text
window             AUC     equity only   expected   actual   EL / actual
2008-12            0.961   0.924         260.4      466      0.50
2013-25            0.963   0.951          97.8       77      0.45
2013-25, no 2023   -       -              97.8       74      3.93

```

**Code**:

```python
# Every input is out of sample for validate and test
rows["pd"] = pd_model.predict(rows)
rows["loss_rate_hat"] = loss_model.predict(rows)
rows["el"] = rows.pd * rows.loss_rate_hat.clip(lower=0) * rows.DEP * runoff_ratio

```

**Answer:** Fit on horizons ending by 2007, score the later windows, and compare summed expected loss with the `COST` the fund booked. Report AUC beside an equity-ratio benchmark, and the 2023 systemic failures both in and out.

**Notes**:

* **Core Execution Mechanic:** Each bank-year's expected loss is `pd * loss_rate_hat * DEP * runoff_ratio`, summed by window against the booked cost of failures inside it.


* **Core Execution Mechanic (Dummy Translation):** You add up what the model expected to lose and put it beside what the deposit insurer really paid.


* **Boundary / Memory Constraint:** With 77 test failures the AUC interval still runs from 0.932 to 0.985.


* **Boundary / Memory Constraint (Dummy Translation):** So few banks failed after 2013 that the score could easily sit a few points either way.


* **Failure Mode / Downstream Impact:** AUC alone hides the miss: 0.96 in the crisis while the model expected 260 failures against 466.


* **Failure Mode / Downstream Impact (Dummy Translation):** A model can order banks almost perfectly and still badly misjudge how many fail.



---

**Question 13**: How do you start exactly one Step Functions execution for a multi-file S3 batch using EventBridge?

**Before Execution** (`[event-pattern.json]` | `[Rule Definition]`):

```text
"source": ["aws.s3"]
"detail-type": ["Object Created"]
"object": {"key": ["new/_READY"]}

```

**After Execution** (`[stage_landing.py --local]` | `[Rule Tested Against Every Key]`):

```text
8 files landed, then new/_READY

no     new/sba_loans/FOIA_7a_FY2000_FY2009_asof_260930.csv
no     new/sba_loans/FOIA_7a_FY2010_FY2019_asof_260930.csv
no     new/sba_loans/FOIA_7a_FY2020_Present_asof_260930.csv
no     new/fdic_failures/failures.csv
no     new/fdic_financials/financials_prefailure.csv
no     new/fdic_financials/financials_yearend.csv
no     new/fdic_institutions/institutions.csv
no     new/sba_recovery_rates/sba_recovery_rates.csv
MATCH  new/_READY

objects 9 | rule matches 1
a run per arrival instead: 8 executions, 7 failed gates

```

**Code**:

```python
# Data files first, marker last: the rule sees nothing until the batch is whole
client = boto3.client("s3")
for path, key in pairs:
    client.upload_file(str(path), args.bucket, key)
    print(f"s3://{args.bucket}/{key}")
client.put_object(Bucket=args.bucket, Key=READY_MARKER, Body=b"")

```

**Answer:** Upload every data file first, then write one empty marker object, and match only that key in the EventBridge rule. Eight data files produce one match and one execution.

**Notes**:

* **Core Execution Mechanic:** S3 emits `Object Created` for each key, and the rule's exact key pattern forwards only `new/_READY` to the state machine.


* **Core Execution Mechanic (Dummy Translation):** Instead of ringing the doorbell for every box, the courier rings once, after the last box is on the porch.


* **Boundary / Memory Constraint:** S3 only sends these events once the bucket's EventBridge notifications are enabled; the marker itself is zero bytes.


* **Boundary / Memory Constraint (Dummy Translation):** The bucket must be told to report to EventBridge, and the marker is an empty file whose only job is to exist.


* **Failure Mode / Downstream Impact:** Firing on every arrival would start eight executions per batch and fail seven gates, because their sibling files had not landed yet.


* **Failure Mode / Downstream Impact (Dummy Translation):** Seven false alarms per upload teach people to ignore red runs, so a real failure gets missed.



---

**Question 14**: How do you pass a result computed inside an ECS Fargate task back into a Step Functions Choice state?

**Before Execution** (`[step-function.json]` | `[ValidateData Task State]`):

```text
"Resource": "arn:aws:states:::ecs:runTask.waitForTaskToken"
TASK_TOKEN <- $$.Task.Token
"TimeoutSeconds": 3600
"ResultPath": "$.validation"

```

**After Execution** (`[run_pipeline.py --local]` | `[State Data At $.validation]`):

```text
"gate_passed": true
"summary": "sba_loans 3/3, fdic_failures 1/1, fdic_financials 2/2, ..."
answer size: 871 bytes

-> AllEntitiesReady (Choice)
-> RunCreditModels (Task)
-> Done (Succeed)

```

**Code**:

```python
# The token goes back with the answer, or with the traceback
token = os.environ["TASK_TOKEN"]
try:
    from credit_risk.validation import run_validation

    output = run_validation(S3Store(os.environ["BUCKET"]))
except Exception:
    report_task(token, error=traceback.format_exc())
    raise
report_task(token, output=output)

```

**Answer:** Start the container with `ecs:runTask.waitForTaskToken`, pass `$$.Task.Token` in its environment, and answer through `SendTaskSuccess`. The JSON it sends lands at the state's `ResultPath`, where the Choice can read it.

**Notes**:

* **Core Execution Mechanic:** `.waitForTaskToken` pauses the state until a call returns its token, and `send_task_success` supplies the JSON written to `$.validation`.


* **Core Execution Mechanic (Dummy Translation):** The state machine hands the container a claim ticket, and the container brings it back with its results attached.


* **Boundary / Memory Constraint:** `TimeoutSeconds` bounds the wait, because a container killed before it sends its token would otherwise leave the state open; an exception, even from the import, fails it at once.


* **Boundary / Memory Constraint (Dummy Translation):** If the container dies without answering, the one-hour timeout ends the wait; if it can still answer, a crash is reported straight away.


* **Failure Mode / Downstream Impact:** `ecs:runTask.sync` returns the ECS task description of ARNs, exit codes and timings, so a Choice could never read the counts the container computed.


* **Failure Mode / Downstream Impact (Dummy Translation):** The simpler option only reports that the container finished, not what it found, so the gate would have nothing to decide on.



---

**Question 15**: How do you make a short or broken S3 batch fail loudly in Step Functions instead of finishing green?

**Before Execution** (`[step-function.json]` | `[Choice With A Failing Default]`):

```text
"Variable": "$.validation.gate_passed"
"Default": "GateFailed"
"CausePath": "$.validation.summary"

test batch: one SBA file removed, one CERT overwritten with text

```

**After Execution** (`[run_pipeline.py --local]` | `[Red Execution]`):

```text
-> GateFailed (Fail)
FAILED ValidationGateFailed: sba_loans 2/3, fdic_failures 0/1, ...; 1 invalid: new/fdic_failures/failures.csv

stray fourth file:          sba_loans 4/3 -> GateFailed
both fixed, fresh marker:   sba_loans 3/3 -> Done

```

**Code**:

```python
# Count the ready folder after the move, and require an exact match
ready = {e.name: len(store.list(f"{e.ready}/")) for e in entities}
expected = {e.name: e.expected_files for e in entities}
passed = not invalid and all(ready[name] == expected[name] for name in expected)

```

**Answer:** Count each entity's ready folder and require an exact match with no invalid file. Route the Choice's default to a Fail state whose `CausePath` reads the validator's summary.

**Notes**:

* **Core Execution Mechanic:** The Choice tests one boolean, and anything else falls to `Default`, a Fail state that resolves its cause from `$.validation.summary`.


* **Core Execution Mechanic (Dummy Translation):** The state machine asks one yes-or-no question, and any answer but yes ends the run in red with the reason attached.


* **Boundary / Memory Constraint:** Counting what is ready rather than this run's arrivals lets a re-upload of one fixed file pass.


* **Boundary / Memory Constraint (Dummy Translation):** Good files stay promoted, so you resend only the broken one.


* **Failure Mode / Downstream Impact:** Ending a short batch in a Pass state shows green in the console, so no alarm fires and the missing data goes unnoticed.


* **Failure Mode / Downstream Impact (Dummy Translation):** A green tick on an incomplete load is the worst outcome, because everyone believes the data arrived.



---

**Question 16**: How do you write model results to DynamoDB when boto3 refuses Python floats?

**Before Execution** (`[TypeSerializer().serialize()]` | `[boto3 Number Rules]`):

```text
0.1                            TypeError: Float types are not supported. Use Decimal types instead.
Decimal(0.1)                   Inexact: [<class 'decimal.Inexact'>, <class 'decimal.Rounded'>]
Decimal("NaN")                 TypeError: Infinity and NaN not supported
Decimal(str(round(0.1, 10)))   {'N': '0.1'}

```

**After Execution** (`[results.sba_items() + fdic_items()]` | `[Items Written]`):

```text
153 items written: sba 91 | fdic 61 | run 1
largest item: 263 bytes by DynamoDB's sizing rule
sba | book#open | el_12m 3269624741.94296

```

**Code**:

```python
# Through str, so the number keeps its short form and not its binary expansion
if isinstance(value, (float, np.floating)):
    value = float(value)
    if not math.isfinite(value):
        return None
    return Decimal(str(round(value, 10)))

```

**Answer:** Convert every number with `Decimal(str(round(x, 10)))` and leave non-finite fields out of the item. Key the one table on `chain` and `item` so a reader can fetch one result or a whole chain.

**Notes**:

* **Core Execution Mechanic:** `round` trims the float, `str` gives its short decimal text, and `Decimal` stores that text instead of the 55-digit binary expansion of 0.1.


* **Core Execution Mechanic (Dummy Translation):** Going through text keeps 0.1 as 0.1; going straight from a float drags in a long tail of rounding noise.


* **Boundary / Memory Constraint:** Items hold 400 KB, so summaries go to the table while the 281,077 loan rows stay in S3 as CSV.


* **Boundary / Memory Constraint (Dummy Translation):** The table holds 153 small results for quick lookup; the big loan-by-loan file stays in the bucket.


* **Failure Mode / Downstream Impact:** A raw float or `NaN` raises inside `put_item`, and a sentinel such as `-1` would read as a real number, so a missing value is simply absent.


* **Failure Mode / Downstream Impact (Dummy Translation):** A blank is left blank, because a fake placeholder would end up in someone's report as if it were true.



---

**Question 17**: How do you archive a processed S3 batch so that a crash at any step can be re-run safely?

**Before Execution** (`[run_pipeline.py --local]` | `[Crash Injected After The Results]`):

```text
-> ModelFailed (Fail)
new/ 0 | ready 8 | results 8 | archived 0

```

**After Execution** (`[run_pipeline.py --local]` | `[Re-Run, No Re-Upload]`):

```text
-> Done (Succeed)
new/ 0 | ready 0 | results 8 | archived 8

crash between copy and delete: re-validated sba_loans 3/3, new/ empty
file replaced and file added mid-run: sba_loans archived 2 | ready 2

```

**Code**:

```python
# S3 has no rename; the copy refuses a changed object
extra = {} if version is None else {"CopySourceIfMatch": version}
self.client.copy({"Bucket": self.bucket, "Key": source}, self.bucket, target,
                 ExtraArgs=extra)

for key, version in read[entity.name].items():
    if not store.move(key, f"{entity.archived}/{key.split('/')[-1]}", version):
        log(f"{key}: replaced or moved since it was read, left for the next run")

```

**Answer:** Write results and items first, archive last, so a failure leaves the batch in its ready folders. Move only the keys this run read, while unchanged; a re-run onto the same key is idempotent.

**Notes**:

* **Core Execution Mechanic:** `client.copy` copies server-side into `archived/<entity>/`, refused if the ETag changed since the read, then `delete_object` removes the source.


* **Core Execution Mechanic (Dummy Translation):** S3 copies the file to the archive only if it is still the one read, then deletes the original, all inside AWS.


* **Boundary / Memory Constraint:** Archiving moves no bytes through the container, so the 0.85 GB batch costs the task no memory here.


* **Boundary / Memory Constraint (Dummy Translation):** The container only sends commands here, so even large files cost it nothing.


* **Failure Mode / Downstream Impact:** Archiving before the results leaves a moved batch with nothing to show, and re-listing at the end would archive a file promoted mid-run, unread.


* **Failure Mode / Downstream Impact (Dummy Translation):** Tidy up only after the work is saved, and only what you used.