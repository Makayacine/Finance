"""The label rule and the company-year rows, from tables as they stood on a given day.

Every function takes plain DataFrames: the Glue job passes it the Delta tables read as of a
batch date, and the tests pass it invented companies. Nothing in here knows what day it is
except through its arguments, which is what keeps a row free of hindsight.

THE LABEL RULE, STATED ONCE
---------------------------
An observation date is 1 October. Its 12-month horizon runs to the next 1 October.

* **Population**: every company registered before the observation date and not terminated before
  it, as the register stood on the eve of the observation date, that is not already in default
  as the insolvency register stood that same day.
* **Default** (``y``): the company's first non-dismissed proceeding, as the insolvency register
  stood on the eve of the horizon's end, started before that end. A proceeding entered during
  the horizon but started a few days before the observation date counts: it was not known on the
  observation date, and the horizon is when it became known. An application still undecided at
  the horizon's end counts as it stood then, even if it was dismissed later.
* **Accounts**: the latest statement available on the eve of the observation date whose period
  ended within the 24 months before it -- the last fiscal year or the one before. ``accounts_lag``
  says which. A company with neither is in the population but not scored, and ``covered`` is
  False.
* A horizon is labelled only when it has ended by the as-of date, the newest ``entry_created_on``
  in the insolvency file.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import config


def observation_date(year):
    return date(year, config.OBSERVATION_MONTH, config.OBSERVATION_DAY)


def horizon_end(year):
    return observation_date(year + config.HORIZON_MONTHS // 12)


def eve(day):
    """The batch date an as-of read uses for ``day``: everything known before it began."""
    return day - timedelta(days=1)


def labelled_years(as_of):
    """Observation years whose whole horizon has been seen by ``as_of``."""
    years, year = [], config.FIRST_OBSERVATION_YEAR
    while horizon_end(year) <= as_of:
        years.append(year)
        year += 1
    return years


def first_defaults(proceedings, insolvency_only=False):
    """regcode -> the start of its first non-dismissed proceeding."""
    kept = proceedings[~proceedings.proceeding_resolution_type.isin(config.DISMISSED)]
    if insolvency_only:
        kept = kept[kept.proceeding_form == "INSOLVENCY"]
    return pd.to_datetime(kept.groupby("regcode").proceeding_started_on.min())


def population(companies, proceedings_at_obs, obs):
    """Companies live on ``obs`` and not already in default as known then."""
    obs = pd.Timestamp(obs)
    registered = pd.to_datetime(companies.registered)
    terminated = pd.to_datetime(companies.terminated)
    live = companies[(registered < obs) & (terminated.isna() | (terminated >= obs))]
    known = first_defaults(proceedings_at_obs)
    already = known[known < obs].index
    return live[~live.regcode.isin(already)].reset_index(drop=True)


def labels(regcodes, proceedings_at_end, end):
    """y and y_insolvency for each regcode: a first default known by ``end`` and before it."""
    end = pd.Timestamp(end)
    out = pd.DataFrame({"regcode": regcodes})
    for column, only in (("y", False), ("y_insolvency", True)):
        first = first_defaults(proceedings_at_end, insolvency_only=only)
        start = out.regcode.map(first)
        out[column] = (start < end).astype(int)
        if column == "y":
            out["default_start"] = start.where(start < end)
    return out


def latest_statements(statements, obs):
    """Each company's newest statement available before ``obs`` and at most 24 months old."""
    obs = pd.Timestamp(obs)
    ended = pd.to_datetime(statements.year_ended_on)
    available = pd.to_datetime(statements.available_on)
    oldest = obs - pd.DateOffset(months=config.MAX_ACCOUNTS_AGE_MONTHS)
    usable = statements[(available < obs) & (ended < obs) & (ended >= oldest)]
    usable = usable.sort_values(["regcode", "year_ended_on", "created_at", "statement_id"])
    return usable.drop_duplicates("regcode", keep="last").reset_index(drop=True)


def accounts_lag(year_ended_on, obs):
    """'current' for the fiscal year that ended in the 12 months before ``obs``, else 'previous'."""
    months = (pd.Timestamp(obs) - pd.to_datetime(year_ended_on)).dt.days / 30.4375
    return pd.Series(np.where(months <= 12, "current", "previous"), index=months.index)


def observation_rows(companies, statements, proceedings_at_obs, proceedings_at_end, year,
                     labelled=True):
    """One row per company in the population on 1 October of ``year``."""
    obs = observation_date(year)
    live = population(companies, proceedings_at_obs, obs)
    rows = live[["regcode", "type", "registered"]].copy()
    rows.insert(0, "observation_year", year)
    accounts = latest_statements(statements, obs)
    rows = rows.merge(accounts.drop(columns=["type"], errors="ignore"), on="regcode", how="left")
    rows["covered"] = rows.statement_id.notna()
    rows["accounts_lag"] = accounts_lag(rows.year_ended_on, obs).where(rows.covered)
    if labelled:
        rows = rows.merge(labels(rows.regcode, proceedings_at_end, horizon_end(year)),
                          on="regcode", how="left")
    return rows
