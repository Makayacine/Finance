**Question 1**: How do you fetch a price page that answers a bare POST with 403?

**Before Execution** (`[urllib POST priceDate=2026-11-02]` | `[Bare Request]`):

```text
POST, no session cookie, no _csrf field
HTTP 403

```

**After Execution** (`[fetch.fetch_fedinvest(day)]` | `[Session Fetch]`):

```text
GET the form -> _csrf token + session cookie
POST priceDate in the same session: HTTP 200
113,624 bytes in 1.57 s | 464 rows parsed

```

**Code**:

```python
    opener = opener or _opener()
    with opener.open(FEDINVEST_URL, timeout=TIMEOUT) as resp:
        form = resp.read().decode("latin-1")
    m = _CSRF.search(form)
    if not m:
        raise FetchError("no _csrf token on the FedInvest form")
    body = urllib.parse.urlencode({"priceDate": day.isoformat(), "submit": "Show Prices", "_csrf": m.group(1)}).encode()
    with opener.open(urllib.request.Request(FEDINVEST_URL, data=body, method="POST"), timeout=TIMEOUT) as resp:
        return resp.read()

```

**Answer:** GET the form first, read its `_csrf` token, and POST the date through the same cookie-carrying opener, so the server sees one session asking twice.

**Notes**:

* **Core Execution Mechanic:** `HTTPCookieProcessor` stores the session cookie the GET sets and sends it back with the POST, and the token is lifted from the form's hidden input with a regex.


* **Core Execution Mechanic (Dummy Translation):** You queue at the counter for a ticket first; the clerk only serves people holding one from this queue.


* **Boundary / Memory Constraint:** That is one GET and one POST per price date, and the backfill sleeps two seconds between dates: the test sees pauses of `[2.0, 2.0]` for three dates.


* **Boundary / Memory Constraint (Dummy Translation):** A whole history fetch stays polite because it never asks the server for more than one day at a time.


* **Failure Mode / Downstream Impact:** A form with no token raises `FetchError` after one request, before any POST, and the source fails alone: GSW, auctions and the NY Fed still land.


* **Failure Mode / Downstream Impact (Dummy Translation):** If the ticket machine is broken, only that counter's errand fails; the other three errands still get done.



---

**Question 2**: How do you decide a price's settlement date when the source never states it?

**Before Execution** (`[days counted from the price date]` | `[T+0 Bill Yields, 30 Oct 2026]`):

```text
3 Nov 0.95% | 5 Nov 1.94% | 10 Nov 2.82%
12 Nov 2.98% | 17 Nov 3.23% | 19 Nov 3.30%

```

**After Execution** (`[dates.settlement_date(price_date)]` | `[T+1 Bill Yields]`):

```text
settles Monday 2026-11-02
3 Nov 3.82% | 5 Nov 3.88% | 10 Nov 3.88%
12 Nov 3.88% | 17 Nov 3.88% | 19 Nov 3.88%

```

**Code**:

```python
def settlement_date(price_date: dt.date) -> dt.date:
    """FedInvest's end-of-day price is for settlement the next business day.

    Decided from the bills: on 2026-10-30 the bills maturing 3, 5 and 10 November price at
    discount yields of 3.82%, 3.88% and 3.88% counted from Monday 2 November, and of 0.95%,
    1.94% and 2.82% counted from the price date itself.
    """
    return add_business_days(price_date, 1)

```

**Answer:** Price the shortest bills under each convention; only the right one leaves the short end flat, because a wrong day count dominates the yield of a bill days from maturity.

**Notes**:

* **Core Execution Mechanic:** A discount yield divides the price gap by the days to maturity, so a 1-day bill counted as 4 days shows a quarter of its rate.


* **Core Execution Mechanic (Dummy Translation):** If you think a one-night loan ran a whole weekend, its interest looks four times cheaper than it was.


* **Boundary / Memory Constraint:** The test fades with maturity: by the 19 November bill the two conventions differ by 0.58 points.


* **Boundary / Memory Constraint (Dummy Translation):** Only the very short bills can tell the two calendars apart; long ones barely notice a day.


* **Failure Mode / Downstream Impact:** A coupon-date check cannot decide this, because a clean price is continuous across the coupon whichever settlement it was struck for.


* **Failure Mode / Downstream Impact (Dummy Translation):** The obvious check passes either way, so it proves nothing; you need the bills to break the tie.



---

**Question 3**: How do you turn a clean quote into the dirty price a pricing library discounts?

**Before Execution** (`[FedInvest END OF DAY, 13 -> 14 Oct 2026]` | `[Clean Prices]`):

```text
5 notes paying a coupon on 15 Oct | median half coupon 1.9375
payers' median clean change: -0.0312

```

**After Execution** (`[dates.dirty_price(sec, settle, clean)]` | `[Dirty Prices at T+1]`):

```text
payers' median dirty change: -1.8960
dirty + coupon paid: -0.0210
other 186 notes' dirty change: +0.0096

```

**Code**:

```python
    if sec.kind == "bill" or sec.coupon == 0.0 or settle >= sec.maturity:
        return 0.0
    if _odd_first(sec, settle):
        return _odd_first_amounts(sec, settle)[1]
    if sec.dated_date is not None and settle <= sec.dated_date:
        return 0.0
    prev, nxt = coupon_period(sec, settle)
    return (sec.coupon / 2.0) * (settle - prev).days / (nxt - prev).days

```

**Answer:** Add actual/actual accrued interest at the settlement date; the dirty price then drops by the coupon on the day settlement reaches the coupon date, and dirty plus coupon runs on like its neighbours.

**Notes**:

* **Core Execution Mechanic:** Accrued is half the coupon times the days since the last coupon over the days in the period, and a coupon falling on the settlement date belongs to the seller.


* **Core Execution Mechanic (Dummy Translation):** The buyer pays the seller for the interest already earned since the last payment, day by day.


* **Boundary / Memory Constraint:** Odd first coupons follow the Treasury's own short and long rules, which need the dated date and first coupon date from the auction record.


* **Boundary / Memory Constraint (Dummy Translation):** A brand-new bond's first payment can be bigger or smaller than usual, and you need its birth certificate to know.


* **Failure Mode / Downstream Impact:** An accrued wrong by a half coupon would put prices off by about 1.9 per 100, and the truth check's slope on the period elapsed would show it.


* **Failure Mode / Downstream Impact (Dummy Translation):** Get the interest-so-far wrong and every bond looks cheap or dear by exactly that amount.



---

**Question 4**: How do you feed calendar dates to a library that only knows float years?

**Before Execution** (`[FIA.bond_factory([date(2027, 11, 15)], [100.0])]` | `[FIA's Date Path]`):

```text
NameError: name 'dt' is not defined

```

**After Execution** (`[dates.to_relative_bond(sec, settle, clean)]` | `[FIA relative_bond]`):

```text
4.5% note maturing 2027-05-15, clean 100.09375 on 23 Oct 2026
settle 2026-10-26 | times [0.0548, 0.5503] | flows [2.25, 102.25]
accrued 2.0054 | dirty 102.0992 | FIA YTM 4.3546%

```

**Code**:

```python
def to_relative_bond(sec: Security, settle: dt.date, clean: Optional[float] = None):
    """An FIA ``relative_bond``: times in years from settlement, payments per 100, dirty price."""
    from . import FIA

    flows = cash_flows(sec, settle)
    bond = FIA.relative_bond([float(a) for _, a in flows], [year_fraction(settle, d) for d, _ in flows])
    if clean is not None:
        bond.set_price(float(dirty_price(sec, settle, clean)))
    return bond

```

**Answer:** Build the dates outside the library and hand it what it understands: payment times in years from settlement, payment amounts per 100, and the dirty price.

**Notes**:

* **Core Execution Mechanic:** `cash_flows` dates every payment from maturity backwards with the end-of-month rule, and `year_fraction` turns each into actual days over 365.25.


* **Core Execution Mechanic (Dummy Translation):** You translate the calendar into the library's language of "years from now" before it ever sees a date.


* **Boundary / Memory Constraint:** Changing the axis to 365 days or to T+0 moved the truth check's mean absolute error by under 0.003 per 100.


* **Boundary / Memory Constraint (Dummy Translation):** The exact day-count choice barely matters here, which is itself a useful thing to have measured.


* **Failure Mode / Downstream Impact:** FIA's own date branch refers to `dt`, which the module never imports, so any `datetime.date` input fails before its `pass` stub.


* **Failure Mode / Downstream Impact (Dummy Translation):** The library's calendar door is painted on the wall; walking into it hurts, so you go round it.



---

**Question 5**: How do you bootstrap with a solver that divides by zero on two bonds of one maturity?

**Before Execution** (`[FIA.curve_factory(bondlist=...)]` | `[Two Notes, One Maturity]`):

```text
23 Oct 2026: 353 notes and bonds, 223 distinct maturities
five notes, two maturing 2027-10-31
ZeroDivisionError: float division by zero

```

**After Execution** (`[bootstrap.select(...); bootstrap.build(picked)]` | `[Bucketed Bootstrap]`):

```text
45 knots: 7 bills, 38 notes and bonds
0.022 s in FIA's pure-Python solver
max reprice error on the knots: 2.9e-09 per 100

```

**Code**:

```python
        short = t < 1.0
        if short != (c.kind == "bill") or c.kind not in ("bill", "note", "bond"):
            continue
        i = max(j for j in range(len(edges) - 1) if edges[j] <= t)
        cur = chosen.get(i)
        if cur is None or _preference(c) < _preference(cur):
            chosen[i] = c

```

**Answer:** Cut the maturity axis into buckets, keep one bond per bucket, and check the picks have distinct maturities before FIA ever sees them.

**Notes**:

* **Core Execution Mechanic:** FIA adds a knot at each bond's maturity and solves for its zero rate by Newton, so two knots at one abscissa make the interpolation weight divide by zero.


* **Core Execution Mechanic (Dummy Translation):** Two posts in the same hole leave the fence-builder with no gap to stretch a wire across.


* **Boundary / Memory Constraint:** Over the sample the bootstrap averaged 0.021 s a day and never above 0.035 s, so the course's pure-Python solver stays in the engine.


* **Boundary / Memory Constraint (Dummy Translation):** The slow-looking textbook code is fast enough because it only ever sees about 45 bonds.


* **Failure Mode / Downstream Impact:** Without the preference order an on-the-run issue could set a knot, and its liquidity premium would kink the curve at that tenor.


* **Failure Mode / Downstream Impact (Dummy Translation):** Let the most popular, overpriced bond anchor a post and the fence bends there.



---

**Question 6**: How do you check your own curve fit against a published one?

**Before Execution** (`[gsw.params_of(row)]` | `[GSW, 23 Oct 2026]`):

```text
(0.000, 4.010, 4.512, 16.154, 1.923, 15.833)
the file's last date: 2026-10-23

```

**After Execution** (`[svensson.fit(bonds)]` | `[Our Svensson Fit]`):

```text
301 notes and bonds | RMSE 3.14 bp | on a bound: beta0
(0.000, 4.046, 4.465, 16.115, 1.964, 16.046)
less GSW: 2y +0.9 | 5y +0.5 | 10y -0.6 | 30y +0.2 bp

```

**Code**:

```python
    def residuals(theta):
        model = cf.price(theta)
        weights = cf.modified_duration(theta)
        return (model - cf.prices) / weights

```

**Answer:** Fit the same objective to the same kind of sample, then compare zero rates rather than parameters, and record any parameter that stops on a bound.

**Notes**:

* **Core Execution Mechanic:** Each price error is divided by the bond's duration, so the least-squares sum is roughly one of yield errors, as GSW weight theirs.


* **Core Execution Mechanic (Dummy Translation):** You score every miss in the same units, so a long bond's big price swing does not drown the short ones.


* **Boundary / Memory Constraint:** On 15 September 2008 the yields agree within 1.2 bp from two to twenty years while the parameters do not: the humps' decay terms nearly coincide.


* **Boundary / Memory Constraint (Dummy Translation):** Two dials can turn against each other and still give the same curve, so you compare the curve, not the dials.


* **Failure Mode / Downstream Impact:** A parameter left on a bound is a constrained fit; `params_at_bound` writes it into every row, 22 of 23 sample days.


* **Failure Mode / Downstream Impact (Dummy Translation):** If the dial is pinned at the end of its range, the log says so instead of pretending it chose that.



---

**Question 7**: How do you tell a wrong accrued or time axis from market noise in pricing errors?

**Before Execution** (`[truth.price_errors(quotes, gsw)]` | `[Raw Errors, 3 Dates]`):

```text
notes and bonds, three months out or more
max |price error|: 1.661 | 4.346 | 1.086

```

**After Execution** (`[np.polyfit(elapsed, error_price, 1)]` | `[Mechanics Slopes]`):

```text
median half coupon:        2.250 | 1.125 | 1.938
slope per period elapsed:  0.040 | 0.068 | 0.027
slope per 10y of maturity: -0.006 | -0.097 | -0.018

```

**Code**:

```python
        elapsed = day.accrued / (day.coupon / 2)
        checks.append({"price_date": d, "n": len(day), "max_abs_price": round(day.error_price.abs().max(), 3),
                       "median_half_coupon": round(float((day.coupon / 2).median()), 3),
                       "slope_per_period_elapsed": round(float(np.polyfit(elapsed, day.error_price, 1)[0]), 3),
                       "slope_per_10y_maturity": round(float(np.polyfit(day.years, day.error_price, 1)[0]) * 10, 3)})

```

**Answer:** Regress the price error on each mechanic's fingerprint: the share of the coupon period elapsed for the accrued, and maturity for the time axis.

**Notes**:

* **Core Execution Mechanic:** A wrong accrued is zero at the start of a period and a half coupon at its end, so it shows as a slope near the half coupon on the share elapsed.


* **Core Execution Mechanic (Dummy Translation):** A mistake that grows through the coupon period leaves a ramp, and you look for the ramp.


* **Boundary / Memory Constraint:** The slopes are 0.027 to 0.068 per 100 against half coupons of 1.125 to 2.250: at most about a sixteenth of what a broken accrued would leave.


* **Boundary / Memory Constraint (Dummy Translation):** The ramp you would expect from the bug is at least sixteen times taller than the one you find.


* **Failure Mode / Downstream Impact:** The large errors that remain are single issues, such as the 30-year on-the-run 4.35 rich in March 2020, not patterns across the period.


* **Failure Mode / Downstream Impact (Dummy Translation):** A few loud outliers are market events; a steady tilt would have been the code.



---

**Question 8**: How do you compare on-the-run and off-the-run issues without the maturity mix lying?

**Before Execution** (`[truth.summarise(nb, ["price_date", "run"])]` | `[Pooled Over Maturities]`):

```text
mean absolute error, price | bp
off-the-run: 0.244 | 5.7, 0.235 | 3.4, 0.109 | 2.0
on-the-run:  0.370 | 5.3, 1.054 | 10.2, 0.133 | 1.5

```

**After Execution** (`[truth.run_status_within_buckets(nb)]` | `[Inside Buckets, bp]`):

```text
2020-03-16 on / off: 1-2y 15.3 / 2.4 | 3-5y 11.6 / 2.4
                    25-30y 16.5 / 3.4 | worst in 6 of 6
2008-09-15 7-10y: 19.6 / 8.8
2026-10-23: on and off within 1.9 in every bucket

```

**Code**:

```python
    nb = df[df["kind"].isin(["note", "bond"])]
    with_otr = nb[nb["run"] == "on-the-run"][["price_date", "bucket"]].drop_duplicates()
    nb = nb.merge(with_otr, on=["price_date", "bucket"])
    t = nb.pivot_table(index=["price_date", "bucket"], columns="run", values="error_bp",
                       aggfunc=lambda s: s.abs().mean(), observed=True)

```

**Answer:** Compare them only inside maturity buckets that hold an on-the-run issue, so each group meets its own maturities.

**Notes**:

* **Core Execution Mechanic:** The merge keeps only the (date, bucket) pairs where an on-the-run issue exists, and the pivot takes mean absolute basis points by run status inside each.


* **Core Execution Mechanic (Dummy Translation):** You only race runners on the same track length.


* **Boundary / Memory Constraint:** Every note under a year is off-the-run, and there a few cents of price is many basis points, which is what the pooled bp figures carry.


* **Boundary / Memory Constraint (Dummy Translation):** The short-distance runners all wear one colour, so that colour looks slow on a stopwatch measured per metre.


* **Failure Mode / Downstream Impact:** Pooled, off-the-run issues looked among the worst in basis points while being the best priced per 100, the opposite of what the buckets show.


* **Failure Mode / Downstream Impact (Dummy Translation):** An average over unlike things can reverse the true ranking.



---

**Question 9**: How do you tell old-bond liquidity from wrong cash flows when bonds and notes split in a bucket?

**Before Execution** (`[truth.summarise(day, ["bucket", "kind"])]` | `[Same Bucket, Mean bp]`):

```text
2008 5-7y: bonds +4.0, notes -10.5
2008 7-10y: bonds +5.8, notes -12.3
2026 1-2y: bonds +17.3, notes +1.4

```

**After Execution** (`[truth.coupon_slope(day, ...)]` | `[Coupon Slope Within Buckets]`):

```text
price per coupon point: +0.165 (0.016) | +0.012 (0.016) | +0.005 (0.006)
bp per point, age held: +0.95 (0.69) | -0.11 (0.47) | +0.47 (0.13)
bp per year since issue: +0.45 (0.20) | +0.16 (0.10) | +0.18 (0.03)

```

**Code**:

```python
    d = df[df["kind"].isin(["note", "bond"]) & (df["run"] != "on-the-run")].dropna(subset=[value, *extra])
    dummies = pd.get_dummies(d[list(controls)].astype(str), drop_first=False, dtype=float)
    X = np.column_stack([d["coupon"].to_numpy(float)] + [d[c].to_numpy(float) for c in extra] + [dummies.to_numpy()])
    y = d[value].to_numpy(float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)

```

**Answer:** Regress the error on the coupon with bucket dummies and on-the-run issues left out, then again with the years since issue beside it.

**Notes**:

* **Core Execution Mechanic:** Bucket dummies absorb each bucket's level, so the coupon slope comes only from bonds and notes sharing a bucket.


* **Core Execution Mechanic (Dummy Translation):** You compare old and new bonds of the same length, never a 2-year with a 30-year.


* **Boundary / Memory Constraint:** Wrong cash flows grow with the coupon in price; the price slope is a hundredth of a point or less per coupon point in 2020 and 2026.


* **Boundary / Memory Constraint (Dummy Translation):** If the payments were miscounted, big-coupon bonds would be off by visibly more money, and they are not.


* **Failure Mode / Downstream Impact:** In 2008 the slope falls by more than half and loses significance once age is held, so the split is old high-coupon bonds trading cheap, not arithmetic.


* **Failure Mode / Downstream Impact (Dummy Translation):** The expensive-looking difference was about how old the bonds are, not what they pay.



---

**Question 10**: How do you flag a bad quote without changing it?

**Before Execution** (`[|bill_curve_gap| > 2/32]` | `[Fixed Tolerance]`):

```text
1,188 quotes valued on the day's bills, 23 sample days
flagged: 450

```

**After Execution** (`[quality.flag_quotes(rows, settle)]` | `[Neighbour Check]`):

```text
flagged: 41, prices changed: 0
912810EY0 6.5% 2026-11-15: 18 days, median -6.14 32nds
912810FA1: 14 days | 912810EZ7: 8 | 9128285G1: 1

```

**Code**:

```python
    for r in checked:
        mat = r["security"].maturity
        peers = sorted(
            q["bill_curve_gap"] for q in checked if q is not r and abs((q["security"].maturity - mat).days) <= NEIGHBOUR_DAYS
        )
        if len(peers) < MIN_NEIGHBOURS:
            continue
        mid = len(peers) // 2
        median = peers[mid] if len(peers) % 2 else (peers[mid - 1] + peers[mid]) / 2.0
        r["neighbour_gap_ticks"] = (r["bill_curve_gap"] - median) / TICK
        if abs(r["neighbour_gap_ticks"]) > OFF_NEIGHBOURS_TICKS:
            r["flags"].append("off_neighbours")

```

**Answer:** Value each short note on the bills, then compare its gap with the median gap of its neighbours, and append a flag where it sits more than four 32nds away.

**Notes**:

* **Core Execution Mechanic:** A note whose payments all fall inside the bill maturities is worth those payments discounted on the bills, with no fitted curve involved.


* **Core Execution Mechanic (Dummy Translation):** You check a price against shops on the same street, not against a catalogue.


* **Boundary / Memory Constraint:** Notes trade a few 32nds cheap to bills as a group, so a fixed tolerance flags the basis itself: 450 of 1,188 quotes.


* **Boundary / Memory Constraint (Dummy Translation):** If every shop on the street charges a bit more, that is the street, not a mispriced item.


* **Failure Mode / Downstream Impact:** Changing the quote would hide it from the error tables; flagged, the 6.5% bond keeps 99-29 and the three-month rule leaves it out of the fit.


* **Failure Mode / Downstream Impact (Dummy Translation):** You put a sticker on the odd price tag rather than repainting it, so nobody loses the evidence.



---

**Question 11**: How do you measure key-rate risk with a library that only bumps its own knots?

**Before Execution** (`[FIA curve.bump(+-0.01, i)]` | `[Per-Knot DV01]`):

```text
10-year on-the-run 91282CRF0, 23 Oct 2026
bootstrap knots: 45 | non-zero DV01s: 22
sum 0.077258 = FIA shift DV01 0.077258

```

**After Execution** (`[risk.measure(bond, params)]` | `[Key-Rate DV01s]`):

```text
2y 0.0027 | 5y 0.0102 | 10y 0.0644 | 20y 0 | 30y 0
sum 0.077254 = curve DV01 0.077254 | yield DV01 0.075479
8,991 security-days: largest relative gap 6.7e-07

```

**Code**:

```python
    for k, tenor in enumerate(KEY_TENORS):
        down = bond.price(fitted.with_bump(_bump(k, -BP)))
        up = bond.price(fitted.with_bump(_bump(k, BP)))
        krd[tenor] = (down - up) / 2.0

```

**Answer:** Bump the fitted curve by one basis point in triangles that peak at each key tenor and add up to one everywhere, then reprice the bond on each.

**Notes**:

* **Core Execution Mechanic:** Each bump is a function of time added to the zero rate, and `SvenssonCurve.discount_factor` applies it, so FIA's `price` runs on the bumped curve unchanged.


* **Core Execution Mechanic (Dummy Translation):** You nudge the curve at 2, 5, 10, 20 and 30 years only, one at a time, and see which nudge moves the bond.


* **Boundary / Memory Constraint:** FIA's knots move every day with the bonds picked, so its 45 per-knot numbers cannot be compared day to day; five key rates can.


* **Boundary / Memory Constraint (Dummy Translation):** Fixed reporting points let yesterday's risk sit beside today's.


* **Failure Mode / Downstream Impact:** If the triangles did not sum to one, the key rates would not add up to the parallel DV01, and a hedge built from them would miss.


* **Failure Mode / Downstream Impact (Dummy Translation):** Pieces that do not add back to the whole leave a hole in the hedge.



---

**Question 12**: How do you keep Parquet partitions readable by Athena when a column is empty on some days?

**Before Execution** (`[pa.Table.from_pandas(df)]` | `[Inferred Schema]`):

```text
call_date, 2026-10-23 (0 callable rows): null
callable rows on 2008-09-15: 3

```

**After Execution** (`[load.to_parquet(df, "prices")]` | `[Fixed Schema]`):

```text
call_date: date32[day] on every date
8 tables, each DDL column list tested against the schema
tests/test_wiring.py: 15 passed

```

**Code**:

```python
def to_parquet(df: pd.DataFrame, table: Optional[str] = None) -> bytes:
    """Parquet bytes; with ``table``, in that table's fixed schema from ``bond_analytics.schemas``."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if table is None:
        arrow = pa.Table.from_pandas(df, preserve_index=False)
    else:
        cols = schemas.columns(table)
        frame = df[cols].astype(object).where(df[cols].notna(), None)
        arrow = pa.Table.from_pandas(frame, schema=schemas.arrow_schema(table), preserve_index=False)
    buf = io.BytesIO()
    pq.write_table(arrow, buf)
    return buf.getvalue()

```

**Answer:** Write every partition with the table's declared schema, so an all-empty column keeps its type instead of becoming Parquet's null type.

**Notes**:

* **Core Execution Mechanic:** pyarrow infers each column from its values, and a column of only `None` has no values to infer from.


* **Core Execution Mechanic (Dummy Translation):** An empty box with no label gets filed under "unknown" unless you label it yourself.


* **Boundary / Memory Constraint:** `bond_analytics/schemas.py` is the one list both the writer and `athena/create-tables.sql` are held to, and a test compares the DDL with it column by column.


* **Boundary / Memory Constraint (Dummy Translation):** The label printer and the shelf sign read from the same sheet.


* **Failure Mode / Downstream Impact:** Athena reads a table whose partitions disagree on a column's type as an error, so one quiet day with no callable bond would break every query that spans it.


* **Failure Mode / Downstream Impact (Dummy Translation):** One mislabelled box can stop the whole shelf from being searched.



---

**Question 13**: How do you give a deploy job AWS access without storing a key in the repository?

**Before Execution** (`[aws-access-key-id + ::set-output, on purpose]` | `[Lab-Style Deploy Step]`):

```text
tests/test_wiring.py -k lambda: 2 failed, 1 passed
extra items: aws-access-key-id, aws-secret-access-key
'::set-output' found

```

**After Execution** (`[role-to-assume only, restored]` | `[OIDC Deploy Step]`):

```text
tests/test_wiring.py -k lambda: 3 passed
deploy permissions: id-token: write, contents: read
secrets read by the three workflows: 0

```

**Code**:

```yaml
    permissions:
      id-token: write
      contents: read
    env:
      FUNCTION: ${{ vars.SOVEREIGN_CURVES_FETCH_FUNCTION }}
    steps:
      - uses: actions/checkout@v7
      - uses: aws-actions/configure-aws-credentials@v6
        with:
          role-to-assume: ${{ vars.SOVEREIGN_CURVES_LAMBDA_ROLE_ARN }}
          aws-region: ${{ vars.SOVEREIGN_CURVES_AWS_REGION }}

```

**Answer:** Let the job ask GitHub for a signed OIDC token and exchange it for a short-lived role session, with `id-token: write` on the deploy job only.

**Notes**:

* **Core Execution Mechanic:** `configure-aws-credentials` presents the token to STS, and the role's trust policy accepts it only for `repo:Makayacine/Finance:ref:refs/heads/main`.


* **Core Execution Mechanic (Dummy Translation):** GitHub vouches for the job each time it runs, and AWS lets in only jobs from the main branch of this repository.


* **Boundary / Memory Constraint:** The session lasts the job; nothing long-lived exists to leak, the workflow-level permission is `contents: read`, and the test job never gets `id-token` at all.


* **Boundary / Memory Constraint (Dummy Translation):** The pass expires when the job ends, and the testing job never gets one.


* **Failure Mode / Downstream Impact:** Static keys beside `role-to-assume` are a second credential to rotate and to leak; the wiring test refuses them and the deprecated `::set-output`.


* **Failure Mode / Downstream Impact (Dummy Translation):** Taping a spare key under the doormat defeats the visitor pass.



---

**Question 14**: How do you make a workflow in a shared repository run only for its own component?

**Before Execution** (`[no paths: filter]` | `[Every Workflow, Every Commit]`):

```text
13 commits on the branch before the CI fix
3 workflows each = 39 runs on push

```

**After Execution** (`[paths: on push and pull_request]` | `[Filtered Runs]`):

```text
lambda 3 | glue 6 | ecs 11 = 20 commit runs
README-only commit: 0
PR #5: every push ran all three (whole-PR diff)

```

**Code**:

```yaml
on:
  push:
    branches: [main]
    paths:
      - "sovereign-curves-cicd-lambda-glue-ecs/lambda-functions/**"
      - ".github/workflows/sovereign-curves-lambda.yml"

```

**Answer:** Give each workflow a `paths:` list of its component's folder and its own file, identical under `push` and `pull_request`.

**Notes**:

* **Core Execution Mechanic:** On a push GitHub matches the filter against the pushed commits' files; on a pull request, against the whole pull request's diff, which is why every push to PR #5 ran all three.


* **Core Execution Mechanic (Dummy Translation):** A doorbell rings only for the flat whose number is on the envelope.


* **Boundary / Memory Constraint:** The engine's filter also lists the loader, the fetcher and the sample, because its image and tests import them.


* **Boundary / Memory Constraint (Dummy Translation):** A flat that shares a kitchen hears the kitchen's bell too.


* **Failure Mode / Downstream Impact:** Without the workflow's own file in its list, an edit to the workflow would not run it, and a broken deploy step would wait for an unrelated change.


* **Failure Mode / Downstream Impact (Dummy Translation):** If changing the doorbell does not ring it, you find out it is broken only when a guest arrives.



---

**Question 15**: How do you keep deploy jobs green and inert on a public repository with no AWS account?

**Before Execution** (`[PR #5, first push]` | `[Check Runs]`):

```text
test: 2 success, 1 failure (moto's Glue backend: no pyparsing)
deploy: 3 skipped

```

**After Execution** (`[PR #5, after the fix]` | `[Check Runs]`):

```text
test: 3 success | deploy: 3 skipped
Glue tests in python:3.9-slim: 36 passed

```

**Code**:

```yaml
  deploy:
    needs: test
    if: github.event_name == 'push' && github.ref == 'refs/heads/main' && vars.SOVEREIGN_CURVES_LAMBDA_ROLE_ARN != ''
    runs-on: ubuntu-24.04
    permissions:
      id-token: write
      contents: read

```

**Answer:** Make the deploy job need the test job and run only on a push to `main` when a repository variable holds its role ARN.

**Notes**:

* **Core Execution Mechanic:** `vars` is available in a job-level `if` and reads as an empty string when unset, so the job is skipped, not failed.


* **Core Execution Mechanic (Dummy Translation):** The delivery van only leaves if the depot has been told where to drive.


* **Boundary / Memory Constraint:** `secrets` cannot be read in a job-level `if`, which is why the guard reads a variable; each workflow has its own, so another project's variables never switch these on.


* **Boundary / Memory Constraint (Dummy Translation):** You cannot check the safe from the gate, so the gate checks a sign on the wall instead.


* **Failure Mode / Downstream Impact:** A red test skips its deploy through `needs: test`: on the first push the Lambda's test failed and its deploy still showed skipped.


* **Failure Mode / Downstream Impact (Dummy Translation):** If the inspection fails, the van never leaves, even when the address is known.



---

**Question 16**: How do you keep a Glue Python Shell job on its only runtime when the rest of the code is newer?

**Before Execution** (`[python3.9 -c "def f(x: int | None): pass"]` | `[3.10 Syntax on 3.9]`):

```text
TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'

```

**After Execution** (`[pytest glue-pythonshell on 3.9.25]` | `[Glue Pins]`):

```text
pandas 1.4.2 | numpy 1.22.3 | boto3 1.21.21 | pyarrow 7.0.0 | openpyxl 3.1.5 | xlrd 2.0.1 | pypdf 5.9.0
16 passed
job: pythonshell 3.9, 0.0625 DPU, library-set analytics

```

**Code**:

```yaml
    container: public.ecr.aws/docker/library/python:3.9-slim
    steps:
      - uses: actions/checkout@v7
      - run: pip install -r glue-pythonshell/requirements.txt
      - name: The loader and the modules it imports, on the analytics library set's versions
        run: python -m pytest glue-pythonshell tests/test_dates.py tests/test_sources.py tests/test_sa_sources.py -v

```

**Answer:** Write the loader and its imports for Python 3.9 and the `analytics` set's versions, and run their tests in a `python:3.9-slim` container with those pins.

**Notes**:

* **Core Execution Mechanic:** `from __future__ import annotations` keeps newer annotations as strings, but a runtime `int | None` still fails on 3.9.


* **Core Execution Mechanic (Dummy Translation):** You write in the dialect the old machine speaks, and test on that machine.


* **Boundary / Memory Constraint:** Python Shell takes 0.0625 or 1 DPU, ignores `--glue-version`, and adds pyarrow, the spreadsheet and PDF readers and the package only as pip pins and wheels.


* **Boundary / Memory Constraint (Dummy Translation):** The small machine comes with a fixed toolbox; you bring extra tools in its own format.


* **Failure Mode / Downstream Impact:** Tested only on 3.13, a loader can pass every check and fail at its first import on Glue.


* **Failure Mode / Downstream Impact (Dummy Translation):** A recipe that works in your kitchen can still fail in the one it is cooked in.



---

**Question 17**: How do you start the engine only when the load succeeds, without Step Functions?

**Before Execution** (`[sample-glue-succeeded-event.json]` | `[Glue Job State Change]`):

```text
states tried: SUCCEEDED, FAILED, TIMEOUT, STOPPED
plus SUCCEEDED from another job

```

**After Execution** (`[matches(pattern, event)]` | `[Rule Verdicts]`):

```text
SUCCEEDED, this job: match
FAILED, TIMEOUT, STOPPED, other job: no match
engine rerun on the same lake: 0 dates

```

**Code**:

```json
{
  "Name": "sovereign-curves-load-succeeded",
  "Description": "The Glue load finished: run the curve and risk engine over the new price dates",
  "EventPattern": "{\"source\": [\"aws.glue\"], \"detail-type\": [\"Glue Job State Change\"], \"detail\": {\"jobName\": [\"<glue-job-name>\"], \"state\": [\"SUCCEEDED\"]}}",
  "State": "ENABLED"
}

```

**Answer:** Put an EventBridge rule on the Glue job's `SUCCEEDED` state change and let it run the ECS task, which finds its own dates from the lake.

**Notes**:

* **Core Execution Mechanic:** The pattern matches source, detail type, job name and state exactly, and the target is the task definition revision the ECS workflow registered last and wrote into the rule with `put-targets`.


* **Core Execution Mechanic (Dummy Translation):** The doorbell rings only when this kitchen says the meal is ready.


* **Boundary / Memory Constraint:** Lambda stops at 15 minutes, and the backfill fetches every business day since June 2008 with a two-second pause between them, so it runs as the same ECS image with `--mode backfill`.


* **Boundary / Memory Constraint (Dummy Translation):** The short-order cook handles one plate; the banquet goes to the kitchen with no clock on the wall.


* **Failure Mode / Downstream Impact:** A duplicated or retried event finds nothing after the engine's watermark: the rerun on the sample did 0 dates.


* **Failure Mode / Downstream Impact (Dummy Translation):** If the bell rings twice, the second visit finds the table already set.