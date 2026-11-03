-- =============================================================================================
-- voucher-float-kinesis-redshift -- the warehouse: six views over what Firehose loads.
--
--   voucher_event              one row per event_id: the relay and Firehose both deliver at
--                              least once, so copies are expected and dropped here
--   voucher_state              one row per voucher: its issue, its exit if it has one, and how
--                              old it is at as_of if it has not
--   daily_liability            outstanding cents and vouchers at the end of every UTC day
--   redemption_life_table      by days since issue: redeemed and expired as competing exits,
--                              open vouchers censored at as_of -- pooled, and for each cohort
--   breakage_by_issue_month    the cohort's breakage: realised once it is closed, projected from
--                              the pooled life table while vouchers are still open
--   reconciliation             the warehouse's liability against the service's own total
--
-- EVERYTHING IS BUILT FROM occurred_at, NEVER FROM ARRIVAL ORDER
--
-- An expired event is dated at its voucher's expires_at, but it is written when the sweep runs,
-- so a sweep that runs late puts it on the stream after events dated later than it -- a week
-- late, if the sweep was down for a week. The relay's retries reorder records too. So no view
-- here depends on the order rows arrived in or on when they were loaded: state, flows and
-- exposure come from the dates on the events. A late event therefore corrects every past day it
-- belongs to the next time a view is read, and a daily liability built after a late sweep is the
-- one an on-time sweep would have produced. tests/test_warehouse.py runs both and compares them.
--
-- ONE TEXT, TWO ENGINES
--
-- The same file runs on the local DuckDB and on Redshift. It keeps to SQL both accept: no
-- generate_series, no QUALIFY, no datediff (Redshift and DuckDB spell it differently), every
-- window aggregate with an explicit frame (Redshift requires one with ORDER BY), division cast to
-- double precision (Redshift divides integers as integers), and ages in days from
-- extract(epoch from ...). On Redshift, run redshift/redshift-create-tables.sql first, in the
-- same session, so the unqualified names resolve to its schema.
-- =============================================================================================

create or replace view voucher_event as
select event_id, voucher_code, event_type, amount_cents, occurred_at, issued_at, expires_at,
       issue_month, days_since_issue, days_to_redeem
from (
    select r.event_id, r.voucher_code, r.event_type, r.amount_cents, r.occurred_at,
           r.issued_at, r.expires_at, r.issue_month, r.days_since_issue, r.days_to_redeem,
           row_number() over (partition by r.event_id order by r.occurred_at) as copy_number
    from voucher_events_raw r
    cross join warehouse_as_of a
    where r.occurred_at <= a.as_of
) copies
where copy_number = 1;


-- A voucher exits at most once; the service's guards make sure of it. If two exits ever arrived,
-- the earlier would win here and reconciliation.vouchers_with_two_exits would say so.
create or replace view voucher_state as
with exits as (
    select voucher_code, event_type, occurred_at, days_since_issue,
           row_number() over (partition by voucher_code
                              order by occurred_at, event_type) as exit_rank
    from voucher_event
    where event_type <> 'issued'
)
select i.voucher_code,
       i.issue_month,
       i.amount_cents,
       i.issued_at,
       i.expires_at,
       coalesce(x.event_type, 'unredeemed')                                   as status,
       x.occurred_at                                                          as exited_at,
       x.days_since_issue                                                     as exit_day,
       case when x.event_type is null
            then cast(floor((extract(epoch from a.as_of) - extract(epoch from i.issued_at))
                            / 86400.0) as integer)
       end                                                                    as censor_day
from voucher_event i
cross join warehouse_as_of a
left join exits x on x.voucher_code = i.voucher_code and x.exit_rank = 1
where i.event_type = 'issued';


create or replace view daily_liability as
with flows as (
    select cast(issued_at as date) as day, amount_cents as issued_cents,
           0 as redeemed_cents, 0 as expired_cents, 1 as vouchers_in, 0 as vouchers_out
    from voucher_state
    union all
    select cast(exited_at as date),
           0,
           case when status = 'redeemed' then amount_cents else 0 end,
           case when status = 'expired' then amount_cents else 0 end,
           0, 1
    from voucher_state
    where exited_at is not null
),
daily as (
    select day, sum(issued_cents) as issued_cents, sum(redeemed_cents) as redeemed_cents,
           sum(expired_cents) as expired_cents, sum(vouchers_in) as vouchers_in,
           sum(vouchers_out) as vouchers_out
    from flows
    group by day
)
select day, issued_cents, redeemed_cents, expired_cents,
       sum(issued_cents - redeemed_cents - expired_cents)
           over (order by day rows between unbounded preceding and current row)
           as outstanding_cents,
       sum(vouchers_in - vouchers_out)
           over (order by day rows between unbounded preceding and current row)
           as outstanding_vouchers
from daily;


-- The actuarial life table, in cents. On day t a voucher is at risk if it has not left before
-- t; it leaves on its exit day, or on its censor day if it is still open at as_of. A voucher
-- censored on day t was watched for part of that day, so it counts half towards day t's
-- exposure. Hazards are cents leaving over cents exposed; survival is their running product;
-- redeemed_share and expired_share are the cumulative incidences of the two competing exits, so
-- redeemed_share on day t is the share of issued value redeemed within t + 1 days of issue, and
-- the last expired_share is the breakage rate.
--
-- Rows exist only for days on which something happened. Survival and both shares are constant
-- between rows, so the value for any day is the one on the latest row at or before it.
create or replace view redemption_life_table as
with observed as (
    select 'pooled' as scope, cast(null as date) as issue_month, status, amount_cents,
           coalesce(exit_day, censor_day) as day
    from voucher_state
    union all
    select 'cohort', issue_month, status, amount_cents, coalesce(exit_day, censor_day)
    from voucher_state
),
by_day as (
    select scope, issue_month, day,
           sum(case when status = 'redeemed' then 1 else 0 end)              as redeemed_vouchers,
           sum(case when status = 'redeemed' then amount_cents else 0 end)   as redeemed_cents,
           sum(case when status = 'expired' then 1 else 0 end)               as expired_vouchers,
           sum(case when status = 'expired' then amount_cents else 0 end)    as expired_cents,
           sum(case when status = 'unredeemed' then 1 else 0 end)            as censored_vouchers,
           sum(case when status = 'unredeemed' then amount_cents else 0 end) as censored_cents,
           count(*)                                                          as leaving_vouchers,
           sum(amount_cents)                                                 as leaving_cents
    from observed
    group by scope, issue_month, day
),
exposure as (
    select b.*,
           sum(leaving_vouchers) over (partition by scope, issue_month)
             - coalesce(sum(leaving_vouchers) over (
                   partition by scope, issue_month order by day
                   rows between unbounded preceding and 1 preceding), 0) as at_risk_vouchers,
           sum(leaving_cents) over (partition by scope, issue_month)
             - coalesce(sum(leaving_cents) over (
                   partition by scope, issue_month order by day
                   rows between unbounded preceding and 1 preceding), 0) as at_risk_cents
    from by_day b
),
hazards as (
    select e.*,
           cast(redeemed_cents as double precision)
               / nullif(at_risk_cents - censored_cents / 2.0, 0) as redeem_hazard,
           cast(expired_cents as double precision)
               / nullif(at_risk_cents - censored_cents / 2.0, 0) as expire_hazard
    from exposure e
),
steps as (
    select h.*, 1.0 - coalesce(redeem_hazard, 0) - coalesce(expire_hazard, 0) as step
    from hazards h
),
survival as (
    select s.*,
           case when sum(case when step <= 0 then 1 else 0 end) over (
                         partition by scope, issue_month order by day
                         rows between unbounded preceding and current row) > 0
                then 0.0
                else exp(sum(ln(case when step > 0 then step else 1.0 end)) over (
                         partition by scope, issue_month order by day
                         rows between unbounded preceding and current row))
           end as survival
    from steps s
),
lagged as (
    select v.*,
           coalesce(lag(survival) over (partition by scope, issue_month order by day), 1.0)
               as survival_before
    from survival v
)
select scope, issue_month, day,
       at_risk_vouchers, at_risk_cents,
       redeemed_vouchers, redeemed_cents,
       expired_vouchers, expired_cents,
       censored_vouchers, censored_cents,
       redeem_hazard, expire_hazard, survival,
       sum(survival_before * coalesce(redeem_hazard, 0)) over (
           partition by scope, issue_month order by day
           rows between unbounded preceding and current row) as redeemed_share,
       sum(survival_before * coalesce(expire_hazard, 0)) over (
           partition by scope, issue_month order by day
           rows between unbounded preceding and current row) as expired_share
from lagged;


-- A cohort's breakage, three ways:
--
--   realised_breakage_rate   expired cents over issued cents, once no voucher is open
--   naive_breakage_rate      expired over expired plus redeemed -- what a report that ignores
--                            open vouchers would say. Biased low while the cohort is young:
--                            the vouchers that redeem early are in it, the ones that will lapse
--                            are not yet. Kept to show the bias, never used.
--   breakage_rate            realised expiries plus, for every open voucher, its amount times
--                            the chance a voucher of its age goes on to lapse, read from the
--                            pooled life table:
--                                (final expired_share - expired_share before its age)
--                                / survival before its age
--                            It equals the realised rate once the cohort is closed.
create or replace view breakage_by_issue_month as
with pooled as (
    select day, survival, expired_share,
           coalesce(lead(day) over (order by day), 2147483647) as next_day
    from redemption_life_table
    where scope = 'pooled'
),
lapse_end as (
    select max(expired_share) as expired_share_end from pooled
),
projected as (
    select v.issue_month,
           sum(v.amount_cents * (e.expired_share_end - coalesce(p.expired_share, 0))
               / coalesce(nullif(p.survival, 0), 1.0)) as projected_expired_cents
    from voucher_state v
    cross join lapse_end e
    left join pooled p on v.censor_day - 1 >= p.day and v.censor_day - 1 < p.next_day
    where v.status = 'unredeemed'
    group by v.issue_month
),
cohorts as (
    select issue_month,
           count(*)                                                           as issued_vouchers,
           sum(amount_cents)                                                  as issued_cents,
           sum(case when status = 'redeemed' then 1 else 0 end)               as redeemed_vouchers,
           sum(case when status = 'redeemed' then amount_cents else 0 end)    as redeemed_cents,
           sum(case when status = 'expired' then 1 else 0 end)                as expired_vouchers,
           sum(case when status = 'expired' then amount_cents else 0 end)     as expired_cents,
           sum(case when status = 'unredeemed' then 1 else 0 end)             as open_vouchers,
           sum(case when status = 'unredeemed' then amount_cents else 0 end)  as open_cents
    from voucher_state
    group by issue_month
)
select c.issue_month,
       c.issued_vouchers, c.issued_cents,
       c.redeemed_vouchers, c.redeemed_cents,
       c.expired_vouchers, c.expired_cents,
       c.open_vouchers, c.open_cents,
       case when c.open_vouchers = 0
            then cast(c.expired_cents as double precision) / c.issued_cents
       end                                                                    as realised_breakage_rate,
       cast(c.expired_cents as double precision)
           / nullif(c.expired_cents + c.redeemed_cents, 0)                    as naive_breakage_rate,
       (c.expired_cents + coalesce(p.projected_expired_cents, 0))
           / cast(c.issued_cents as double precision)                         as breakage_rate
from cohorts c
left join projected p on p.issue_month = c.issue_month;


-- The warehouse's outstanding liability must equal the service's own sum of unredeemed cents,
-- taken at the same as_of. Beside the difference: how many copies the dedupe dropped, exits
-- whose issue never arrived, and vouchers with two exits -- each of which should be zero, and
-- each of which would explain a difference that is not.
create or replace view reconciliation as
with warehouse as (
    select coalesce(sum(case when status = 'unredeemed' then amount_cents else 0 end), 0)
               as outstanding_cents,
           sum(case when status = 'unredeemed' then 1 else 0 end) as outstanding_vouchers
    from voucher_state
),
landing as (
    select count(*) as raw_rows, count(distinct r.event_id) as distinct_events
    from voucher_events_raw r
    cross join warehouse_as_of a
    where r.occurred_at <= a.as_of
),
issued as (
    select distinct voucher_code from voucher_event where event_type = 'issued'
),
orphans as (
    select count(*) as orphan_exits
    from voucher_event e
    left join issued i on i.voucher_code = e.voucher_code
    where e.event_type <> 'issued' and i.voucher_code is null
),
doubles as (
    select count(*) as vouchers_with_two_exits
    from (select voucher_code from voucher_event where event_type <> 'issued'
          group by voucher_code having count(*) > 1) t
)
select c.taken_at,
       c.unredeemed_cents                          as service_cents,
       w.outstanding_cents                         as warehouse_cents,
       w.outstanding_cents - c.unredeemed_cents    as difference_cents,
       c.unredeemed_vouchers                       as service_vouchers,
       w.outstanding_vouchers                      as warehouse_vouchers,
       l.raw_rows - l.distinct_events              as duplicates_dropped,
       o.orphan_exits,
       d.vouchers_with_two_exits
from service_control_total c
cross join warehouse w
cross join landing l
cross join orphans o
cross join doubles d;
