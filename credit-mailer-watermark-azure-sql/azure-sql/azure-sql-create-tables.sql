-- =============================================================================================
-- credit-mailer-watermark-azure-sql -- the warehouse schema in T-SQL, and the arm grid it seeds.
--
-- This is ../credit-mailer-watermark-glue-redshift/redshift/redshift-create-tables.sql moved to
-- Azure SQL Database. Same two zones, same nine tables, same columns in the same order, same
-- natural keys, same 18 seeded arms. Every difference below is one T-SQL forces, and each is
-- marked where it happens with a `T-SQL:` comment so the two files can be read side by side.
--
--   raw_zone        mirrors the published extract's column names VERBATIM -- `offer4`, `edhi`,
--                   `waved3`, `amountbrw_unc` -- so a landed row can be diffed against the source
--                   file character for character.
--   processed_zone  uses business names. The MERGE column list in the processed-layer job is the
--                   only place the translation happens.
--
-- NO `create database` IN THIS FILE, which is the first departure from the Redshift DDL. Azure SQL
-- provisions the database outside T-SQL -- portal, CLI or Bicep, with a service tier attached --
-- and a connection is always opened against one named database, so there is nothing for a
-- `create database` line to do here. The local harness (local-development/apply_ddl.py) creates
-- db_credit_mailer in the SQL Server container before it runs this file, which is the container's
-- stand-in for that provisioning step.
--
-- GO SEPARATES BATCHES, AND IT IS NOT T-SQL. `GO` is a client-side batch separator understood by
-- sqlcmd and SSMS; the server never sees it, and pyodbc sends it through as a syntax error. It is
-- here because `CREATE SCHEMA` must open its batch -- measured on SQL Server 2022, a CREATE TABLE
-- after `CREATE SCHEMA raw_zone;` in the same batch fails with error 156 -- and apply_ddl.py
-- splits this file on lines holding only GO, exactly as sqlcmd would.
--
-- A note that belongs everywhere client_id appears, including here: CLIENT_ID IS MINTED. The
-- deposit publishes no client identifier of any kind. `client_id` is the 1-based row number of
-- the published extract and exists only to give the two source tables a join key. It is not a
-- lender account number, and nothing may be inferred from its ordering.
-- =============================================================================================

-- ---------------------------------------------------------------------------------------------
-- RAW ZONE
-- ---------------------------------------------------------------------------------------------
CREATE SCHEMA raw_zone;
GO

-- SMALLINT, NOT BIT, ON THE 0/1 FLAGS -- the load-bearing decision of the Redshift file, kept.
-- Fourteen of the treatment columns are NULL on all 4,974 wave-1 rows, because wave 1 was a
-- price-only experiment and those arms did not exist yet. That NULL is data: it is the refinery's
-- Step 4 latent state and it reaches processed_zone.fact_mailer unfilled.
--
-- T-SQL has no BOOLEAN, and BIT is the obvious substitute, which is why this is restated rather
-- than inherited. BIT would hold the NULL. What it would not do is refuse a value that is not a
-- flag: T-SQL converts any non-zero integer to BIT 1 without complaint, so a column that ever
-- carried a 2 would load it as "true". SMALLINT keeps the value the source published, and the
-- processed layer decides what it means.
--
-- T-SQL: one more reason NULL needs guarding here and did not on Redshift. `CAST('' AS SMALLINT)`
-- is 0 in T-SQL -- measured, not remembered -- so an empty CSV field that reached the server as a
-- string would become a zero, and wave 1 would claim it was shown fourteen treatments it never
-- saw. The raw-ingestion job turns every empty field into a NULL parameter before it is bound.
CREATE TABLE raw_zone.mail_offers (
  client_id            BIGINT   NOT NULL,
  wave                 SMALLINT NOT NULL,
  offer4               DECIMAL(5,2),
  prize                SMALLINT,
  intshown             SMALLINT,
  dphoto_female        SMALLINT,
  dphoto_none          SMALLINT,
  dphoto_black         SMALLINT,
  gender_match         SMALLINT,
  race_match           SMALLINT,
  nspeakeligible       SMALLINT,
  speak_trt            SMALLINT,
  oneln_trt            SMALLINT,
  comploss_n           SMALLINT,
  use_any              SMALLINT,
  stripany             SMALLINT,
  comp_n               SMALLINT,
  deadlinemed          SMALLINT,
  deadlinelong         SMALLINT,
  deadlong_elig        SMALLINT,
  deadshort_elig       SMALLINT,
  deadlineshortext     SMALLINT,
  waved3               SMALLINT,
  applied              SMALLINT,
  tookup               SMALLINT,
  amountbrw_unc        INTEGER,
  badacct_last         SMALLINT,
  applied_2weeks       SMALLINT,
  tookup_after_short   SMALLINT,
  tookup_after_med     SMALLINT,
  tookup_after_long    SMALLINT,
  tookup_outside_only  SMALLINT,
  PRIMARY KEY (client_id, wave)
);

-- VARCHAR(16) ON TWO COLUMNS WHOSE MEASURED MAXIMA ARE 8 AND 6.
--   race  longest value is 'coloured', 8 characters
--   risk  longest value is 'MEDIUM',   6 characters
-- The width is headroom, written down with the maxima so it reads as a decision. SQL Server
-- ENFORCES it -- a 17-character value fails the statement with error 2628 rather than being
-- truncated -- which DuckDB, the AWS sibling's local warehouse, does not. So the headroom is
-- asserted by a test here: 16 characters load, 17 fail the whole load and leave the table as it
-- was. See tests/test_warehouse.py.
CREATE TABLE raw_zone.client_attributes (
  client_id  BIGINT NOT NULL,
  race       VARCHAR(16),
  risk       VARCHAR(16),
  female     SMALLINT,
  edhi       SMALLINT,
  dormancy   SMALLINT,
  trcount    SMALLINT,
  PRIMARY KEY (client_id)
);

-- Staging copies. T-SQL: Redshift's `CREATE TABLE ... AS SELECT *` becomes `SELECT ... INTO ...`,
-- and `WHERE 1 = 0` makes the copy empty. SELECT ... INTO carries column names, order, types and
-- nullability, so `client_id` and `wave` stay NOT NULL in staging; Redshift's CTAS carries names,
-- order and types but not NOT NULL, so its staging columns were all nullable. Neither engine
-- carries the PRIMARY KEY -- and that matters more here than it did there, because SQL Server
-- ENFORCES the key on the target. A staging table holding one (client_id, wave)
-- twice therefore reaches the MERGE, and the MERGE's insert of the second copy fails with error
-- 2627 instead of landing a duplicate. On Redshift the same file would load both.
SELECT * INTO raw_zone.tmp_mail_offers       FROM raw_zone.mail_offers       WHERE 1 = 0;
SELECT * INTO raw_zone.tmp_client_attributes FROM raw_zone.client_attributes WHERE 1 = 0;
GO

-- ---------------------------------------------------------------------------------------------
-- PROCESSED ZONE
-- ---------------------------------------------------------------------------------------------
CREATE SCHEMA processed_zone;
GO

-- NO IDENTITY COLUMN ANYWHERE IN THIS SCHEMA. The fact's grain is (client_id, wave) and that pair
-- is a natural key; an IDENTITY surrogate beside it would give the MERGE a second thing to match
-- on, generated fresh on every insert and therefore matching nothing.
--
-- T-SQL: is_female and is_more_educated are BOOLEAN on Redshift and nullable BIT here. That is
-- the one place BIT is right, and the reason is the same argument as SMALLINT above read from the
-- other side: these two are never loaded from a CSV. They are COMPUTED by the processed-layer
-- MERGE from the SMALLINT raw flags, so no loader is ever asked what an empty field means. The
-- Redshift expression `(ca.female = 1)` is not valid T-SQL -- a comparison is not a value there --
-- so the job writes it as a CASE with no ELSE branch, which keeps its three-valued meaning:
-- 1 -> 1, any other number -> 0, and NULL -> NULL, because neither WHEN is true for a NULL and
-- there is no ELSE to supply a default. "Did not say" stays distinct from "no".
CREATE TABLE processed_zone.dim_client (
  client_id         BIGINT PRIMARY KEY,
  risk_band         VARCHAR(16),
  race              VARCHAR(16),
  is_female         BIT,
  is_more_educated  BIT,
  months_dormant    SMALLINT,
  prior_loans       SMALLINT
);

-- THIS TABLE IS SEED DATA, NOT PIPELINE OUTPUT. It is declared and populated by this file, and no
-- job writes to it. The processed-layer job asserts it holds 18 rows before it joins to it.
--
-- The cut points are declared constants, never quantiles of the arriving data: bandit_posterior
-- accumulates (pulls, rewards) per arm ACROSS waves, so a grid recomputed per run would add counts
-- from two different price bands into one arm_id.
--
-- An arm is  rate_floor <= offer_rate < rate_ceil,  with the top arm of each band open above.
-- arm_id = base + arm_index + 1, base 0 for HIGH, 6 for MEDIUM, 12 for LOW.
CREATE TABLE processed_zone.dim_offer_arm (
  arm_id      SMALLINT PRIMARY KEY,
  risk_band   VARCHAR(16)  NOT NULL,
  arm_index   SMALLINT     NOT NULL,
  rate_floor  DECIMAL(5,2) NOT NULL,
  rate_ceil   DECIMAL(5,2),            -- NULL on the top arm: open above
  arm_label   VARCHAR(32)  NOT NULL
);

-- The bottom arm of each band is floored at 0.00 rather than at the lowest rate the source
-- happens to contain, so no mailer priced below an observed minimum can fall through the grid.
INSERT INTO processed_zone.dim_offer_arm
  (arm_id, risk_band, arm_index, rate_floor, rate_ceil, arm_label)
VALUES
  ( 1, 'HIGH',   0,  0.00,  5.50, 'HIGH 0 [0.00, 5.50)'),
  ( 2, 'HIGH',   1,  5.50,  7.50, 'HIGH 1 [5.50, 7.50)'),
  ( 3, 'HIGH',   2,  7.50,  9.00, 'HIGH 2 [7.50, 9.00)'),
  ( 4, 'HIGH',   3,  9.00, 10.00, 'HIGH 3 [9.00, 10.00)'),
  ( 5, 'HIGH',   4, 10.00, 11.00, 'HIGH 4 [10.00, 11.00)'),
  ( 6, 'HIGH',   5, 11.00,  NULL, 'HIGH 5 [11.00, +inf)'),
  ( 7, 'MEDIUM', 0,  0.00,  5.00, 'MEDIUM 0 [0.00, 5.00)'),
  ( 8, 'MEDIUM', 1,  5.00,  6.75, 'MEDIUM 1 [5.00, 6.75)'),
  ( 9, 'MEDIUM', 2,  6.75,  7.50, 'MEDIUM 2 [6.75, 7.50)'),
  (10, 'MEDIUM', 3,  7.50,  8.25, 'MEDIUM 3 [7.50, 8.25)'),
  (11, 'MEDIUM', 4,  8.25,  9.25, 'MEDIUM 4 [8.25, 9.25)'),
  (12, 'MEDIUM', 5,  9.25,  NULL, 'MEDIUM 5 [9.25, +inf)'),
  (13, 'LOW',    0,  0.00,  4.50, 'LOW 0 [0.00, 4.50)'),
  (14, 'LOW',    1,  4.50,  5.50, 'LOW 1 [4.50, 5.50)'),
  (15, 'LOW',    2,  5.50,  6.00, 'LOW 2 [5.50, 6.00)'),
  (16, 'LOW',    3,  6.00,  6.75, 'LOW 3 [6.00, 6.75)'),
  (17, 'LOW',    4,  6.75,  7.50, 'LOW 4 [6.75, 7.50)'),
  (18, 'LOW',    5,  7.50,  NULL, 'LOW 5 [7.50, +inf)');

-- risk_band is denormalised onto the fact: it is the bandit's context, and a later correction to
-- a client's grade in dim_client must not move a 2003 mailer into a different arm.
--
-- bad_account is NULL unless took_up = 1, and is never coalesced to 0. In the source, badacct_last
-- is non-null on exactly the 4,381 rows where tookup = 1.
--
-- T-SQL: the PRIMARY KEY below is enforced here. On Redshift it is informational -- accepted,
-- read by the planner, never checked -- while NOT NULL is enforced on both engines. So a
-- duplicate (client_id, wave) that got past the processed-layer job's pre-MERGE checks would have
-- merged into the Redshift fact without a word; here the key is a second line behind those
-- checks, one the Redshift star never had.
CREATE TABLE processed_zone.fact_mailer (
  client_id            BIGINT   NOT NULL,
  wave                 SMALLINT NOT NULL,
  arm_id               SMALLINT NOT NULL,
  risk_band            VARCHAR(16) NOT NULL,   -- the bandit's context, denormalised
  offer_rate           DECIMAL(5,2) NOT NULL,
  applied              SMALLINT NOT NULL,
  took_up              SMALLINT NOT NULL,
  amount_borrowed      INTEGER  NOT NULL,
  bad_account          SMALLINT,               -- NULL unless took_up = 1
  -- the treatment flags, carried with their native missingness (Step 4)
  prize                SMALLINT,
  intshown             SMALLINT,
  dphoto_female        SMALLINT,
  dphoto_none          SMALLINT,
  dphoto_black         SMALLINT,
  gender_match         SMALLINT,
  race_match           SMALLINT,
  nspeakeligible       SMALLINT,
  speak_trt            SMALLINT,
  oneln_trt            SMALLINT,
  comploss_n           SMALLINT,
  use_any              SMALLINT,
  stripany             SMALLINT,
  comp_n               SMALLINT,
  deadlinemed          SMALLINT,
  deadlinelong         SMALLINT,
  deadlong_elig        SMALLINT,
  deadshort_elig       SMALLINT,
  deadlineshortext     SMALLINT,
  PRIMARY KEY (client_id, wave)
);

-- The refinery's two output tables. through_wave and eval_wave are ordinal batch indices, not
-- dates. alpha and beta are DECIMAL(12,2) because they are Beta parameters that happen to be
-- integral under whole-count updates, not counts.
CREATE TABLE processed_zone.bandit_posterior (
  run_id        VARCHAR(32)  NOT NULL,
  through_wave  SMALLINT     NOT NULL,
  arm_id        SMALLINT     NOT NULL,
  risk_band     VARCHAR(16)  NOT NULL,
  arm_index     SMALLINT     NOT NULL,
  pulls         INTEGER      NOT NULL,
  rewards       INTEGER      NOT NULL,
  alpha         DECIMAL(12,2) NOT NULL,
  beta          DECIMAL(12,2) NOT NULL,
  posterior_mean DECIMAL(10,8) NOT NULL,
  posterior_sd   DECIMAL(10,8) NOT NULL,
  ts_probability DECIMAL(10,8) NOT NULL,   -- P(this arm wins a Thompson draw)
  PRIMARY KEY (run_id, through_wave, arm_id)
);

-- diff, ci_low and ci_high are signed: the evaluated policy is allowed to come out WORSE than the
-- mailer's own randomisation, and when it does that is a result, not a fault to be clamped.
CREATE TABLE processed_zone.bandit_policy_value (
  run_id           VARCHAR(32) NOT NULL,
  eval_wave        SMALLINT    NOT NULL,
  risk_band        VARCHAR(16) NOT NULL,
  n_rows           INTEGER     NOT NULL,
  logging_value    DECIMAL(10,8) NOT NULL,
  snips_value      DECIMAL(10,8) NOT NULL,
  diff             DECIMAL(10,8) NOT NULL,
  ci_low           DECIMAL(10,8) NOT NULL,
  ci_high          DECIMAL(10,8) NOT NULL,
  p_diff_positive  DECIMAL(10,8) NOT NULL,
  PRIMARY KEY (run_id, eval_wave, risk_band)
);
GO

-- ---------------------------------------------------------------------------------------------
-- Seed check. The same count the processed-layer job asserts before it merges the fact.
--   expected:  18 arms, 3 bands, 6 arms per band, and exactly 3 open-topped arms.
-- ---------------------------------------------------------------------------------------------
SELECT risk_band,
       count(*)                                           AS arms,
       sum(CASE WHEN rate_ceil IS NULL THEN 1 ELSE 0 END) AS open_topped,
       min(rate_floor)                                    AS lowest_floor,
       max(rate_floor)                                    AS highest_floor
FROM processed_zone.dim_offer_arm
GROUP BY risk_band
ORDER BY risk_band;
