-- =============================================================================================
-- voucher-float-kinesis-redshift -- the Redshift side: a schema, the landing table Firehose
-- COPYs into, and the two one-row tables the views read.
--
-- Run order, in one session:
--
--   1. this file
--   2. warehouse/views.sql, unchanged -- the same text the local DuckDB runs. The
--      `set search_path` below is what lets its unqualified names find these tables.
--
-- The tables match warehouse/landing-tables.sql column for column and type for type; this file
-- adds only what Redshift does with them.
--
--   voucher_events_raw      DISTKEY voucher_code: voucher_state partitions every window by
--                           voucher, so each voucher's events sit on one slice and the views
--                           never redistribute them. SORTKEY occurred_at: every view filters on
--                           as_of and the daily liability groups by day, so the zone maps skip
--                           whatever is after as_of.
--   warehouse_as_of         DISTSTYLE ALL, one row, joined everywhere.
--   service_control_total   DISTSTYLE ALL, one row.
--
-- THE DELIVERY STREAM'S COPY
--
-- Firehose writes each buffer to S3 and issues the COPY itself. Give the delivery stream this
-- table, this column list and these COPY options:
--
--   table     voucher_float.voucher_events_raw
--   columns   event_id,voucher_code,event_type,amount_cents,occurred_at,issued_at,expires_at,
--             issue_month,days_since_issue,days_to_redeem
--   options   FORMAT AS JSON 'auto' TIMEFORMAT 'YYYY-MM-DD HH:MI:SS' DATEFORMAT 'YYYY-MM-DD'
--
-- JSON 'auto' matches the transform's keys to the columns by name, so the order the keys come
-- out in does not matter, and a null days_to_redeem loads as NULL.
--
-- THE NIGHTLY CLOSE
--
-- The warehouse reports as of the last close, and a close is the nightly expiry sweep. Straight
-- after the sweep, take the service's count and sum of unredeemed vouchers; once the relay has
-- drained and Firehose's buffer has passed the sweep, run these three in one transaction:
--
--   delete from voucher_float.service_control_total;
--   insert into voucher_float.service_control_total values ('<sweep-time>', <vouchers>, <cents>);
--   update voucher_float.warehouse_as_of set as_of = '<sweep-time>';
--
-- Closing on the sweep is what makes the reconciliation compare like with like: until a lapsed
-- voucher is swept, the service still counts it unredeemed, while the warehouse, which dates the
-- lapse at expiry, has already taken it off.
-- =============================================================================================

create schema if not exists voucher_float;
set search_path to voucher_float;

create table if not exists voucher_events_raw (
    event_id          varchar(36)   not null  encode zstd,
    voucher_code      varchar(12)   not null  encode zstd,
    event_type        varchar(8)    not null  encode bytedict,
    amount_cents      bigint        not null  encode az64,
    occurred_at       timestamp     not null  encode raw,
    issued_at         timestamp     not null  encode az64,
    expires_at        timestamp     not null  encode az64,
    issue_month       date          not null  encode az64,
    days_since_issue  integer       not null  encode az64,
    days_to_redeem    integer                 encode az64
)
diststyle key
distkey (voucher_code)
compound sortkey (occurred_at);

create table if not exists warehouse_as_of (
    as_of             timestamp     not null
)
diststyle all;

insert into warehouse_as_of (as_of)
select getdate() where not exists (select 1 from warehouse_as_of);

create table if not exists service_control_total (
    taken_at             timestamp  not null,
    unredeemed_vouchers  bigint     not null,
    unredeemed_cents     bigint     not null
)
diststyle all;
