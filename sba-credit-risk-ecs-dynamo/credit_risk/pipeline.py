"""The model container's whole job: read the ready folders, fit both chains, write, archive.

    <entity>/ ready folders  ->  run_sba()   ->  results/sba/*.csv     +  items, chain "sba"
                             ->  run_fdic()  ->  results/fdic/*.csv    +  items, chain "fdic"
                             ->  archived/<entity>/                       once both have written

The order of the last three is the safety. Results are written before anything moves, so a crash
mid-model leaves the batch in the ready folders, the Step Functions execution red, and a re-run
able to start again from exactly the files that failed. Archiving first would leave a batch that
had moved and no results to show for it.

The archive step moves the keys this run read and nothing else, and each only while it still
holds what was read. Nothing stops a second execution's validator promoting a file into a ready
folder while this run is modelling, and re-listing the folders at the end would archive that
file unread. So a new file is left where it is, and so is a file replaced under a key that was
read, both for the next run to pick up.

Nothing here knows whether ``store`` is a folder or a bucket, or whether ``table`` is a JSON file
or DynamoDB. The two container entrypoints decide that and pass them in.
"""

import contextlib
import io
from datetime import datetime, timezone

import pandas as pd

from credit_risk import fdic, results, sba
from credit_risk.config import ENTITIES, RESULTS


@contextlib.contextmanager
def ready_files(store, entities=ENTITIES):
    """``({entity: [local paths]}, {entity: {key: version}})`` for every ready file.

    Each key's version is taken before the file is read, so a file replaced mid-run can only
    look changed when the archive step checks it, never unchanged.
    """
    with contextlib.ExitStack() as stack:
        paths, read = {}, {}
        for entity in entities:
            keys = store.list(f"{entity.ready}/")
            read[entity.name] = {key: store.version(key) for key in keys}
            paths[entity.name] = [stack.enter_context(store.local_copy(k)) for k in keys]
        yield paths, read


def pick(paths, fragment):
    matches = [p for p in paths if fragment in str(p)]
    if len(matches) != 1:
        raise FileNotFoundError(f"expected one file matching {fragment!r}, found {len(matches)}")
    return matches[0]


def run_sba(loan_paths, recovery_path, n_boot=500, log=print):
    loans = sba.read_loans(loan_paths)
    book = sba.company_book(loans)
    log(f"sba: {len(loans):,} loans read, {len(book):,} in the company book")
    pd_model = sba.fit_pd(book, n_boot)
    log(f"sba: scorecard holdout AUC {pd_model.holdout_overall['auc']:.3f}")
    hazard_model = sba.fit_hazard(pd_model, book)
    severity_model = sba.fit_severity(pd_model, book)
    live = sba.live_book(pd_model, hazard_model, severity_model, book)
    stages = live.groupby("stage").agg(loans=("loan_id", "size"), exposure=("exposure", "sum"),
                                       el_12m=("el_12m", "sum"), el_lifetime=("el_lifetime", "sum"),
                                       ecl=("ecl", "sum"), ecl_pit=("ecl_pit", "sum")).reset_index()
    recovery = pd.read_csv(recovery_path)
    out = {
        "book": sba.book_summary(live),
        "holdout_overall": pd_model.holdout_overall,
        "next_vintage": {"vintage": sba.NEXT_VINTAGE, **pd_model.next_vintage},
        "holdout_by_vintage": pd_model.holdout_by_vintage,
        "bands": hazard_model.bands,
        "experience": hazard_model.experience,
        "stage2": hazard_model.stage2,
        "stages": stages,
        "severity": severity_model.out_of_time,
        "severity_rules": sba.severity_sensitivity(pd_model, hazard_model, severity_model, book),
        "psi": sba.psi_by_vintage(pd_model, book),
        "recovery": sba.recovery_context(recovery),
    }
    files = {
        "live_book.csv": live,
        "scorecard_points.csv": pd_model.card.points,
        "scorecard_coefficients.csv": pd_model.card.coefficients(),
        "hazard_curve.csv": pd.DataFrame({"age_month": range(hazard_model.curve.max_age),
                                          "at_risk": hazard_model.curve.at_risk,
                                          "h_charge_off": hazard_model.curve.h_event,
                                          "h_paid_in_full": hazard_model.curve.h_competing}),
    }
    log(f"sba: {len(live):,} open loans, 12-month EL {out['book']['el_12m']:,.0f}")
    return out, files


def run_fdic(failure_path, prefailure_path, yearend_path, institution_path, n_boot=500,
             log=print):
    failures = pd.read_csv(failure_path)
    prefailure = pd.read_csv(prefailure_path)
    yearend = pd.read_csv(yearend_path)
    institutions = pd.read_csv(institution_path)
    _, _, counts = fdic.failure_events(failures)
    panel = fdic.pd_panel(yearend, failures)
    evaluation = fdic.evaluate_pd(panel, n_boot)
    kept = fdic.evaluate_pd(fdic.pd_panel(yearend, failures, exits="keep"), n_boot)
    log(f"fdic: {(panel.drop_reason == 'kept').sum():,} bank-years labelled")
    sample = fdic.loss_sample(failures, prefailure)
    loss_model, out_of_time = fdic.loss_out_of_time(sample)
    runoff, _ = fdic.runoff_sample(failures, prefailure)
    runoff_ratio = float(runoff[runoff.FAILYR <= 2007].runoff_ratio.mean())
    top, summary, _, _ = fdic.counterparty_el(panel, failures, prefailure, institutions,
                                              runoff_ratio)
    out = {
        "pd": evaluation["discrimination"],
        "pd_exits_kept": kept["discrimination"],
        "loss_out_of_time": out_of_time,
        "loss_cross_validated": fdic.loss_cross_validated(sample),
        "loss_backtest": fdic.loss_backtest(loss_model, sample),
        "runoff": fdic.runoff_table(runoff),
        "el_backtest": fdic.el_backtest(evaluation["model"], loss_model, panel, failures,
                                        runoff_ratio),
        "counterparty_summary": summary,
        "counterparty_top": top,
        "event_counts": counts,
    }
    files = {
        "pd_coefficients.csv": evaluation["model"].coefficients(),
        "loss_coefficients.csv": loss_model.result.summary2().tables[1].reset_index(names="term"),
        "counterparty_top.csv": top,
        "el_backtest.csv": out["el_backtest"],
    }
    log(f"fdic: {summary['banks']:,} banks scored at {summary['as_of']}")
    return out, files


def run_models(store, table, n_boot=500, log=print):
    """Fit both chains from the ready folders, write results and items, then archive the batch."""
    with ready_files(store) as (paths, read):
        missing = [e.name for e in ENTITIES if len(paths[e.name]) != e.expected_files]
        if missing:
            raise RuntimeError(f"ready folders short of their expected files: {missing}")
        sba_out, sba_files = run_sba(paths["sba_loans"], paths["sba_recovery_rates"][0],
                                     n_boot, log)
        fin = paths["fdic_financials"]
        fdic_out, fdic_files = run_fdic(paths["fdic_failures"][0], pick(fin, "prefailure"),
                                        pick(fin, "yearend"), paths["fdic_institutions"][0],
                                        n_boot, log)
        counts = {e.name: len(paths[e.name]) for e in ENTITIES}

    for chain, files in (("sba", sba_files), ("fdic", fdic_files)):
        for name, frame in files.items():
            buffer = io.StringIO()
            frame.to_csv(buffer, index=False)
            store.put_text(f"{RESULTS}/{chain}/{name}", buffer.getvalue())
    items = results.sba_items(sba_out) + results.fdic_items(fdic_out)
    items.append(results.to_item("run", "latest", {
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{f"files_{k}": v for k, v in counts.items()}}))
    for item in items:
        table.put_item(item)
    log(f"{len(items)} items written")

    for entity in ENTITIES:
        for key, version in read[entity.name].items():
            if not store.move(key, f"{entity.archived}/{key.split('/')[-1]}", version):
                log(f"{key}: replaced or moved since it was read, left for the next run")
    return {"items": len(items), "sba": sba_out["book"], "fdic": fdic_out["counterparty_summary"]}
