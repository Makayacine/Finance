-- =============================================================================================
-- voucher-float-kinesis-redshift -- the three tables the warehouse views read, for local DuckDB.
--
-- These are the tables in redshift/redshift-create-tables.sql, column for column and type for
-- type, without the distribution and sort keys DuckDB has no use for. tests/test_warehouse.py
-- holds the two files to that.
--
--   voucher_events_raw      what Firehose COPYs in: one row per Ok record from the transform,
--                           duplicates and all. Nothing reads it except voucher_event.
--   warehouse_as_of         one row: the moment the warehouse reports as of. Events dated
--                           after it are invisible, and an open voucher is censored at it.
--   service_control_total   one row: the service database's own count and sum of unredeemed
--                           vouchers, taken at the same moment -- the other side of the
--                           reconciliation.
-- =============================================================================================

create table if not exists voucher_events_raw (
    event_id          varchar(36)   not null,
    voucher_code      varchar(12)   not null,
    event_type        varchar(8)    not null,
    amount_cents      bigint        not null,
    occurred_at       timestamp     not null,
    issued_at         timestamp     not null,
    expires_at        timestamp     not null,
    issue_month       date          not null,
    days_since_issue  integer       not null,
    days_to_redeem    integer
);

create table if not exists warehouse_as_of (
    as_of             timestamp     not null
);

create table if not exists service_control_total (
    taken_at             timestamp  not null,
    unredeemed_vouchers  bigint     not null,
    unredeemed_cents     bigint     not null
);
