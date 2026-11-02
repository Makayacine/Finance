"""Replay today's files as the dated batches a nightly extract would have delivered.

Each source row is placed in the quarterly batch (or, with ``cadence="year"``, the batch ending
30 September) of a date the file itself carries, and a row
whose state changed later is sent twice: once as it stood when it arrived, and again, restated,
in the batch of the change. Nothing else is derived and no value is made up.

    register                  arrives on ``registered``; restated on ``terminated``, which is
                              when ``terminated`` and ``closed`` first appear
    insolvency proceeding     arrives on ``entry_created_on``, open; restated on
                              ``proceeding_ended_on``, which is when the end date and the
                              resolution code first appear
    statement + its balance   arrives on its availability date (``config``): ``created_at``, or
    sheet and income          for the bulk-loaded ones nine months after the period end, never
    statement                 later than the stamp

A row that changed in the same quarter it arrived in is sent once, already in its final state.
Everything dated on or before ``FIRST_BATCH`` arrives in that first batch, and the last batch is
dated with the newest stamp in the files rather than with the end of a quarter that has not
happened.

The replay rebuilds only what today's files still show. A proceeding whose resolution code was
corrected, or a statement replaced in place, left no trace of its earlier value, so neither can
be replayed.
"""

import numpy as np
import pandas as pd

from . import config


def quarter_end(dates):
    """The last day of each date's calendar quarter, as a datetime64 Series."""
    dates = pd.to_datetime(pd.Series(dates))
    return (dates.dt.to_period("Q").dt.end_time.dt.normalize()).astype("datetime64[ns]")


def september_end(dates):
    """The 30 September on or after each date: the eve of the next observation date."""
    dates = pd.to_datetime(pd.Series(dates))
    year = dates.dt.year + (dates.dt.month > config.OBSERVATION_MONTH - 1).astype(int)
    return pd.to_datetime(year.astype("Int64").astype(str) + "-09-30", errors="coerce")


CADENCES = {"quarter": quarter_end, "year": september_end}


def batch_of(dates, last_batch, cadence="quarter"):
    """The batch a date falls in: the end of its quarter, or with ``cadence="year"`` its
    30 September, held between FIRST_BATCH and ``last_batch``.

    Every observation date is a 1 October, so both cadences put a batch boundary on its eve and
    the tables read as of it are the same rows either way: the yearly cadence builds identical
    training rows in a quarter of the commits.
    """
    dates = pd.to_datetime(pd.Series(dates))
    first = pd.Timestamp(config.FIRST_BATCH)
    batch = CADENCES[cadence](dates)
    # Anything dated on or before the first batch arrives in it, whatever its period would end on.
    batch = batch.where(~(dates <= first), first)
    return batch.where(batch < pd.Timestamp(last_batch), pd.Timestamp(last_batch))


def available_on(created_at, year_ended_on):
    """The day a statement can first be read, by the rule in ``config``.

    ``created_at`` is a filing time from BULK_LOAD_END on. Before it, the stamp is the bulk load,
    an upper bound on the filing date, so the statement is dated EMBARGO_MONTHS after its period
    end unless the stamp is earlier still.
    """
    created = pd.to_datetime(pd.Series(created_at), format="mixed").dt.normalize()
    ended = pd.to_datetime(pd.Series(year_ended_on))
    assumed = ended + pd.DateOffset(months=config.EMBARGO_MONTHS)
    bulk = created < pd.Timestamp(config.BULK_LOAD_END)
    return created.where(~bulk, np.minimum(created, assumed))


def last_batch_date(register, proceedings, statements):
    """The newest date stamp in any of the files: the date of the final, partial batch."""
    stamps = [
        pd.to_datetime(register.registered).max(),
        pd.to_datetime(register.terminated).max(),
        pd.to_datetime(proceedings.entry_created_on).max(),
        pd.to_datetime(proceedings.proceeding_ended_on).max(),
        pd.to_datetime(statements.created_at, format="mixed").max(),
    ]
    return max(s for s in stamps if pd.notna(s)).normalize()


def _arrive_then_restate(frame, arrive_on, change_on, changed_columns, last_batch, cadence):
    """Split each row into its arrival state and, where the change came later, its restatement."""
    arrival = batch_of(frame[arrive_on], last_batch, cadence).values
    change = batch_of(frame[change_on], last_batch, cadence).values
    has_change = frame[change_on].notna().values
    later = has_change & (change > arrival)

    first = frame.copy()
    first.loc[later, list(changed_columns)] = None
    first.insert(0, "batch_date", arrival)

    restated = frame.loc[later].copy()
    restated.insert(0, "batch_date", change[later])
    return pd.concat([first, restated], ignore_index=True)


def register_batches(register, last_batch, cadence="quarter"):
    return _arrive_then_restate(
        register, "registered", "terminated", ("terminated", "closed"), last_batch, cadence
    )


def proceeding_batches(proceedings, last_batch, cadence="quarter"):
    # A proceeding that ended inside its arrival quarter arrives closed. One with a resolution
    # code but no end date (the file has a handful) has no date to restate it on, so it arrives
    # as it stands today.
    return _arrive_then_restate(
        proceedings, "entry_created_on", "proceeding_ended_on",
        ("proceeding_ended_on", "proceeding_resolution_type"), last_batch, cadence,
    )


def statement_batches(statements, balances, incomes, last_batch, cadence="quarter"):
    """One batch date per statement, carried by its balance sheet and income statement rows."""
    batch = batch_of(available_on(statements.created_at, statements.year_ended_on), last_batch,
                     cadence)
    statements = statements.assign(batch_date=batch.values)
    dates = statements.set_index("id").batch_date
    balances = balances.assign(batch_date=balances.statement_id.map(dates).values)
    incomes = incomes.assign(batch_date=incomes.statement_id.map(dates).values)
    # A balance sheet or income statement whose statement row is not in the extract has no date
    # to arrive on; the curated table would drop it on the join anyway.
    return (
        _first(statements),
        _first(balances.dropna(subset=["batch_date"])),
        _first(incomes.dropna(subset=["batch_date"])),
    )


def _first(frame):
    columns = ["batch_date"] + [c for c in frame.columns if c != "batch_date"]
    return frame[columns]


def restatement_share(batches, key):
    """Per batch: rows, and the share whose key arrived in an earlier batch."""
    seen = batches.sort_values("batch_date").drop_duplicates(list(key), keep="first")
    first = seen.set_index(list(key)).batch_date
    arrived = batches.set_index(list(key)).index.map(first)
    earlier = pd.Series(np.asarray(arrived) < batches.batch_date.values, index=batches.index)
    table = batches.assign(restated=earlier.values).groupby("batch_date").restated.agg(
        rows="size", restated="sum"
    )
    table["share"] = table.restated / table.rows
    return table
