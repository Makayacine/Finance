"""Check every landed file against its entity's dtype dict, promote the good ones, and gate.

    new/<entity>/<file>   ->  validate_file()  ->  <entity>/<file>   when it passes
                                               ->  stays in new/      when it does not, named in
                                                                      the output with its reason
    then, per entity:     files now in <entity>/  ==  expected_files  ->  gate_passed

This module is the validator container's whole decision. It takes a store (a folder on a laptop,
a bucket on AWS) and the entity list, and returns one JSON-able dict. The state machine's Choice
reads ``gate_passed`` from that dict and nothing else.

THE GATE COUNTS WHAT IS READY, NOT WHAT ARRIVED THIS TIME
---------------------------------------------------------

The reference lab returns the number of files each entity had validated on this run. That works
while every batch arrives whole, and breaks the first time one does not: fix the one bad file,
re-upload it, and the second run sees one valid file for an entity that needs three. Counting
the ready folder after the move makes a re-run after a fix pass, and makes a stray fourth file in
``sba_loans/`` fail the gate rather than be quietly modelled -- the count must equal
``expected_files`` exactly, not reach it.

A FAILED GATE IS A FAILED EXECUTION
-----------------------------------

The lab ends a short batch in a Pass state, which shows green in the console. Here the Choice's
default is a Fail state carrying ``summary`` as its cause, so an incomplete or malformed batch is
a red execution with the reason on it. The marker that started the run is consumed either way,
so a fix is a re-upload of the bad file plus a fresh marker.
"""

import pandas as pd

from credit_risk.config import ENTITIES, LANDING, READY_MARKER


def dtype_matches(inferred, declared):
    """Exact, except that a float64 declaration also accepts int64 (see config.py)."""
    return inferred == declared or (declared == "float64" and inferred == "int64")


def validate_file(path, entity):
    """``(ok, reason, rows)`` for one file against one entity's declaration."""
    try:
        header = list(pd.read_csv(path, nrows=0).columns)
    except (ValueError, UnicodeDecodeError, pd.errors.ParserError) as error:
        return False, f"unreadable header: {error}", 0
    missing = [column for column in entity.dtypes if column not in header]
    if missing:
        return False, f"missing columns {missing}", 0
    unexpected = [c for c in header if c not in entity.dtypes and c not in entity.never_read]
    if unexpected:
        return False, f"undeclared columns {unexpected}", 0
    try:
        frame = pd.read_csv(
            path,
            usecols=list(entity.dtypes),
            dtype={column: "str" for column in entity.string_columns},
            low_memory=False,  # one inference over the whole column, not one per chunk
        )
    except (ValueError, UnicodeDecodeError, pd.errors.ParserError) as error:
        return False, f"unreadable body: {error}", 0
    if frame.empty:
        return False, "no rows", 0
    wrong = [
        f"{column} is {frame[column].dtype}, declared {declared}"
        for column, declared in entity.dtypes.items()
        if not dtype_matches(str(frame[column].dtype), declared)
    ]
    if wrong:
        return False, "; ".join(wrong), len(frame)
    return True, "ok", len(frame)


def run_validation(store, entities=ENTITIES, log=print):
    """Validate everything under ``new/``, promote what passes, and return the gate's verdict."""
    store.delete(READY_MARKER)
    known = {entity.name: entity for entity in entities}
    invalid, rows = [], {}
    for key in store.list(f"{LANDING}/"):
        parts = key.split("/")
        if len(parts) != 3 or parts[1] not in known:
            invalid.append({"key": key, "reason": "not under new/<entity>/ for a known entity"})
            continue
        entity = known[parts[1]]
        with store.local_copy(key) as path:
            ok, reason, n_rows = validate_file(path, entity)
        log(f"{key}: {'valid' if ok else 'INVALID'}, {n_rows:,} rows, {reason}")
        if ok:
            store.move(key, f"{entity.ready}/{parts[2]}")
            rows[key] = n_rows
        else:
            invalid.append({"key": key, "reason": reason})

    ready = {e.name: len(store.list(f"{e.ready}/")) for e in entities}
    expected = {e.name: e.expected_files for e in entities}
    passed = all(ready[name] == expected[name] for name in expected)
    summary = ", ".join(f"{name} {ready[name]}/{expected[name]}" for name in expected)
    if invalid:
        summary += f"; {len(invalid)} invalid: " + "; ".join(i["key"] for i in invalid)
    return {
        "valid_files": ready,
        "expected_files": expected,
        "rows_validated": rows,
        "invalid_files": invalid,
        "gate_passed": passed,
        "summary": summary,
    }
