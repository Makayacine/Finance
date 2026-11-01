"""Turn model outputs into DynamoDB items: one table, ``chain`` + ``item``, Decimal numbers.

    chain = "sba"    book#open, scorecard#holdout, scorecard#vintage#2006, band#A, vintage#2007,
                     stage#2, severity#out_of_time, severity_rule#cap, psi#2024, recovery#2018
    chain = "fdic"   pd#validate#full, pd_exits_kept#test#full, loss#out_of_time#test#excluded,
                     loss#backtest#validate#included, runoff#2008-12, el_backtest#test#excluded,
                     counterparty#summary, counterparty#rank#01, events#counts
    chain = "run"    latest: when, and how many rows each entity delivered

DynamoDB has no float. boto3 refuses one outright, so every number is converted with
``Decimal(str(round(x, 10)))`` -- through ``str`` because ``Decimal(0.1)`` is the binary
expansion of 0.1, 55 digits long, and DynamoDB caps a number at 38. NaN and infinity have no
DynamoDB representation either, so a field whose value is not finite is left out of the item
rather than stored as a sentinel a reader could mistake for a number.
"""

import math
from decimal import Decimal

import numpy as np
import pandas as pd


def to_number(value):
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return Decimal(int(value))
    if isinstance(value, (float, np.floating)):
        value = float(value)
        if not math.isfinite(value):
            return None
        return Decimal(str(round(value, 10)))
    if isinstance(value, pd.Timestamp):
        return str(value.date())
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return str(value)


def to_item(chain, item, fields):
    out = {"chain": chain, "item": item}
    for key, value in fields.items():
        converted = to_number(value)
        if converted is not None:
            out[str(key)] = converted
    return out


def table_items(chain, prefix, frame, key_columns):
    """One item per row, keyed ``prefix#<key>[#<key>...]``."""
    items = []
    for row in frame.to_dict("records"):
        key = "#".join(str(row[k]) for k in key_columns)
        items.append(to_item(chain, f"{prefix}#{key}", row))
    return items


def sba_items(out):
    items = [to_item("sba", "book#open", out["book"])]
    items.append(to_item("sba", "scorecard#holdout", out["holdout_overall"]))
    items.append(to_item("sba", "scorecard#next_vintage", out["next_vintage"]))
    items += table_items("sba", "scorecard#vintage", out["holdout_by_vintage"], ["vintage"])
    items += table_items("sba", "band", out["bands"].reset_index(names="band"), ["band"])
    experience = out["experience"].reset_index()
    experience["stage2"] = experience.vintage.isin(out["stage2"])
    items += table_items("sba", "vintage", experience, ["vintage"])
    items += table_items("sba", "stage", out["stages"], ["stage"])
    items.append(to_item("sba", "severity#out_of_time", out["severity"]))
    items += table_items("sba", "severity_rule", out["severity_rules"], ["rule"])
    items += table_items("sba", "psi", out["psi"], ["vintage"])
    items += table_items("sba", "recovery", out["recovery"].reset_index(), ["cohort_year"])
    return items


def fdic_items(out):
    items = table_items("fdic", "pd", out["pd"], ["window", "model"])
    items += table_items("fdic", "pd_exits_kept", out["pd_exits_kept"], ["window", "model"])
    items += table_items("fdic", "loss#out_of_time", out["loss_out_of_time"],
                         ["window", "systemic_2023"])
    items.append(to_item("fdic", "loss#cross_validated", out["loss_cross_validated"]))
    items += table_items("fdic", "loss#backtest", out["loss_backtest"],
                         ["window", "systemic_2023"])
    items += table_items("fdic", "runoff", out["runoff"].reset_index(names="era"), ["era"])
    items += table_items("fdic", "el_backtest", out["el_backtest"], ["window", "systemic_2023"])
    items.append(to_item("fdic", "counterparty#summary", out["counterparty_summary"]))
    ranked = out["counterparty_top"].assign(rank=lambda d: [f"{i:02d}" for i in
                                                            range(1, len(d) + 1)])
    items += table_items("fdic", "counterparty#rank", ranked, ["rank"])
    items.append(to_item("fdic", "events#counts", out["event_counts"]))
    return items
