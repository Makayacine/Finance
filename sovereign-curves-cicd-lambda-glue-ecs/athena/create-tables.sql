-- Athena tables over the lake. Replace <lake-bucket> with the bucket the loader and the engine
-- write to. Partitions are projected, so no MSCK REPAIR or crawler is needed: a new date is
-- readable as soon as its file lands. The column lists are bond_analytics/schemas.py, and
-- tests/test_wiring.py holds the two to each other.

CREATE DATABASE IF NOT EXISTS sovereign_curves;

-- FedInvest's end-of-day prices, clean as quoted, with accrued at T+1 and the dirty price; one partition per price date
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.prices (
  `price_date` date,
  `settle_date` date,
  `cusip` string,
  `kind` string,
  `security_type` string,
  `coupon` double,
  `maturity` date,
  `call_date` date,
  `callable` boolean,
  `buy` double,
  `sell` double,
  `clean` double,
  `accrued` double,
  `dirty` double,
  `years` double,
  `original_term` string,
  `issue_date` date,
  `first_period` string,
  `bill_curve_gap` double,
  `neighbour_gap_ticks` double,
  `quality_flags` string
)
PARTITIONED BY (`date` string)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/prices/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.date.type' = 'date',
  'projection.date.format' = 'yyyy-MM-dd',
  'projection.date.range' = '2008-06-01,NOW',
  'projection.date.interval' = '1',
  'projection.date.interval.unit' = 'DAYS',
  'storage.location.template' = 's3://<lake-bucket>/prices/date=${date}/'
);

-- one row per CUSIP from its original auction: original term, issue and dated dates, first coupon
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.securities (
  `cusip` string,
  `auction_type` string,
  `original_term` string,
  `issue_date` date,
  `dated_date` date,
  `first_coupon_date` date,
  `first_period` string,
  `inflation_indexed` boolean,
  `floating` boolean
)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/securities/';

-- the Fed Board's GSW parameters and zero yields, one file per year
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.reference_curves (
  `date` date,
  `beta0` double,
  `beta1` double,
  `beta2` double,
  `beta3` double,
  `tau1` double,
  `tau2` double,
  `sveny01` double,
  `sveny02` double,
  `sveny03` double,
  `sveny04` double,
  `sveny05` double,
  `sveny06` double,
  `sveny07` double,
  `sveny08` double,
  `sveny09` double,
  `sveny10` double,
  `sveny11` double,
  `sveny12` double,
  `sveny13` double,
  `sveny14` double,
  `sveny15` double,
  `sveny16` double,
  `sveny17` double,
  `sveny18` double,
  `sveny19` double,
  `sveny20` double,
  `sveny21` double,
  `sveny22` double,
  `sveny23` double,
  `sveny24` double,
  `sveny25` double,
  `sveny26` double,
  `sveny27` double,
  `sveny28` double,
  `sveny29` double,
  `sveny30` double
)
PARTITIONED BY (`year` int)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/reference_curves/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.year.type' = 'integer',
  'projection.year.range' = '1961,2100',
  'storage.location.template' = 's3://<lake-bucket>/reference_curves/year=${year}/'
);

-- SOFR, BGCR and TGCR from the New York Fed, one file per year
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.funding (
  `date` date,
  `rate_type` string,
  `percent_rate` double,
  `volume_bn` double
)
PARTITIONED BY (`year` int)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/funding/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.year.type' = 'integer',
  'projection.year.range' = '2018,2100',
  'storage.location.template' = 's3://<lake-bucket>/funding/year=${year}/'
);

-- zero and par yields on a tenor grid for the FIA bootstrap, our Svensson fit and GSW
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.curves (
  `price_date` date,
  `curve` string,
  `tenor` double,
  `zero_rate` double,
  `par_yield` double,
  `discount_factor` double
)
PARTITIONED BY (`date` string)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/curves/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.date.type' = 'date',
  'projection.date.format' = 'yyyy-MM-dd',
  'projection.date.range' = '2008-06-01,NOW',
  'projection.date.interval' = '1',
  'projection.date.interval.unit' = 'DAYS',
  'storage.location.template' = 's3://<lake-bucket>/curves/date=${date}/'
);

-- Svensson parameters, fit size, RMSE in bp, any parameter on a bound, and the day's funding rate
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.curve_params (
  `price_date` date,
  `curve` string,
  `beta0` double,
  `beta1` double,
  `beta2` double,
  `beta3` double,
  `tau1` double,
  `tau2` double,
  `rmse_bp` double,
  `n_bonds` double,
  `params_at_bound` string,
  `seconds` double,
  `funding_rate` double,
  `funding_source` string
)
PARTITIONED BY (`date` string)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/curve_params/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.date.type' = 'date',
  'projection.date.format' = 'yyyy-MM-dd',
  'projection.date.range' = '2008-06-01,NOW',
  'projection.date.interval' = '1',
  'projection.date.interval.unit' = 'DAYS',
  'storage.location.template' = 's3://<lake-bucket>/curve_params/date=${date}/'
);

-- model less market for every bill, note and bond against each curve
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.fit_errors (
  `price_date` date,
  `cusip` string,
  `kind` string,
  `coupon` double,
  `maturity` date,
  `years` double,
  `original_term` string,
  `issue_date` date,
  `run` string,
  `callable` boolean,
  `clean` double,
  `accrued` double,
  `dirty` double,
  `model_dirty` double,
  `error_price` double,
  `ytm` double,
  `error_bp` double,
  `bucket` string,
  `curve` string,
  `in_fit` boolean,
  `params_at_bound` string
)
PARTITIONED BY (`date` string)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/fit_errors/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.date.type' = 'date',
  'projection.date.format' = 'yyyy-MM-dd',
  'projection.date.range' = '2008-06-01,NOW',
  'projection.date.interval' = '1',
  'projection.date.interval.unit' = 'DAYS',
  'storage.location.template' = 's3://<lake-bucket>/fit_errors/date=${date}/'
);

-- DV01, modified duration, convexity, z-spread, key-rate DV01s and 30-day carry per security
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.risk (
  `price_date` date,
  `cusip` string,
  `kind` string,
  `coupon` double,
  `maturity` date,
  `years` double,
  `run` string,
  `dirty` double,
  `ytm` double,
  `dv01` double,
  `modified_duration` double,
  `convexity` double,
  `z_spread_bp` double,
  `curve_dv01` double,
  `carry_30d` double,
  `krd_2y` double,
  `krd_5y` double,
  `krd_10y` double,
  `krd_20y` double,
  `krd_30y` double
)
PARTITIONED BY (`date` string)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/risk/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.date.type' = 'date',
  'projection.date.format' = 'yyyy-MM-dd',
  'projection.date.range' = '2008-06-01,NOW',
  'projection.date.interval' = '1',
  'projection.date.interval.unit' = 'DAYS',
  'storage.location.template' = 's3://<lake-bucket>/risk/date=${date}/'
);

-- How far each curve sits from FedInvest, notes and bonds GSW would use, by date:
-- SELECT price_date, curve, count(*) AS n, avg(abs(error_bp)) AS mae_bp
--   FROM sovereign_curves.fit_errors
--  WHERE "date" BETWEEN '2026-10-01' AND '2026-10-30' AND kind IN ('note', 'bond') AND years >= 0.25
--  GROUP BY 1, 2 ORDER BY 1, 2;

-- South Africa

-- the National Treasury's fixed-rate bonds from its debt schedules: coupon, cycle, redemption, the leg it is priced on
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.sa_bonds (
  `bond_code` string,
  `coupon` double,
  `issue_date` date,
  `issue_price` double,
  `interest_dates_listed` string,
  `coupon_months` string,
  `coupon_days` string,
  `redemptions` string,
  `pricing_maturity` date,
  `legs` double,
  `nominal_rand` double,
  `schedule_date` date,
  `quality_flags` string,
  `source` string
)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/sa_bonds/';


-- the Treasury's fixed-rate bond auctions, figures as published, flags where a check fails; one file per year
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.sa_auctions (
  `auction_date` date,
  `settle_date` date,
  `bond_code` string,
  `bond_label` string,
  `coupon_quoted` double,
  `redemption_year_quoted` double,
  `offered_rand` double,
  `allocated_rand` double,
  `non_comps_rand` double,
  `bids_received_rand` double,
  `number_of_bids` double,
  `clearing_yield` double,
  `best_bid` double,
  `worst_bid` double,
  `bid_to_cover` double,
  `yield_unit` string,
  `source_file` string,
  `sheet` string,
  `quality_flags` string,
  `source` string
)
PARTITIONED BY (`year` int)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/sa_auctions/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.year.type' = 'integer',
  'projection.year.range' = '2010,2100',
  'storage.location.template' = 's3://<lake-bucket>/sa_auctions/year=${year}/'
);


-- the OECD's long-term rate and the IMF's government bond yield for South Africa, monthly, with their citations
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.sa_reference (
  `date` date,
  `series` string,
  `percent` double,
  `source` string
)
PARTITIONED BY (`year` int)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/sa_reference/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.year.type' = 'integer',
  'projection.year.range' = '2010,2100',
  'storage.location.template' = 's3://<lake-bucket>/sa_reference/year=${year}/'
);


-- each usable auction's bond priced with the JSE formula: all-in, accrued, clean, DV01, duration, convexity
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.sa_prices (
  `auction_date` date,
  `settle_date` date,
  `bond_code` string,
  `coupon` double,
  `maturity` date,
  `years` double,
  `clearing_yield` double,
  `cum` boolean,
  `all_in` double,
  `accrued` double,
  `clean` double,
  `dv01` double,
  `modified_duration` double,
  `convexity` double,
  `fia_ytm` double,
  `source` string
)
PARTITIONED BY (`year` int)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/sa_prices/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.year.type' = 'integer',
  'projection.year.range' = '2010,2100',
  'storage.location.template' = 's3://<lake-bucket>/sa_prices/year=${year}/'
);


-- zero and par yields per auction date from FIA's bootstrap of that day's bonds, extrapolation marked
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.sa_curves (
  `auction_date` date,
  `tenor` double,
  `zero_rate` double,
  `par_yield` double,
  `discount_factor` double,
  `extrapolated` boolean,
  `n_bonds` double,
  `shortest` double,
  `longest` double
)
PARTITIONED BY (`year` int)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/sa_curves/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.year.type' = 'integer',
  'projection.year.range' = '2010,2100',
  'storage.location.template' = 's3://<lake-bucket>/sa_curves/year=${year}/'
);


-- the 10-year par yield by month against the OECD and IMF series, in bp
CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves.sa_crosscheck (
  `month` date,
  `auction_dates` double,
  `inside_knots` double,
  `par_10y` double,
  `zero_10y` double,
  `oecd_irlt` double,
  `imf_ifs` double,
  `par_minus_oecd_bp` double,
  `par_minus_imf_bp` double
)
STORED AS PARQUET
LOCATION 's3://<lake-bucket>/sa_crosscheck/';
