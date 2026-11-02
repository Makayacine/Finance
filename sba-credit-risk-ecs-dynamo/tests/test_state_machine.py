"""The flow you deploy, read from its own JSON and run end to end on the committed sample.

``local-development/run_pipeline.py`` walks ``step-functions/step-function.json`` state by state,
so the end-to-end tests below exercise the definition itself: the Choice's ``Variable`` path, the
``ResultPath`` the validator's answer is written to, and the Fail state's ``CausePath``. A typo
in any of them fails these tests the same way it would fail an execution.

The static tests pin the three decisions the brief asked for: the validator's output comes back
through a task token, because ``runTask.sync`` cannot carry it; a failed gate is a Fail state,
not a Pass; and the definition names no entity, so the entity list in ``config.py`` stays the
only place one is declared.
"""

import json
import sys
from decimal import Decimal

import pytest

from conftest import PROJECT, load_module
from credit_risk import pipeline
from credit_risk.config import ENTITIES, READY_MARKER
from credit_risk.results import to_item
from credit_risk.stores import FolderStore, JsonTable
from credit_risk.validation import run_validation

DEFINITION = json.loads((PROJECT / "step-functions" / "step-function.json").read_text())
PATTERN = json.loads((PROJECT / "event-pattern.json").read_text())
runner = load_module("local-development/run_pipeline.py", "run_pipeline")
stager = load_module("local-development/stage_landing.py", "stage_landing")


def test_validator_answers_through_a_task_token():
    state = DEFINITION["States"]["ValidateData"]
    assert state["Resource"].endswith("ecs:runTask.waitForTaskToken")
    env = state["Parameters"]["Overrides"]["ContainerOverrides"][0]["Environment"]
    assert {"Name": "TASK_TOKEN", "Value.$": "$$.Task.Token"} in env


def test_a_validator_that_cannot_import_still_answers_its_token(monkeypatch):
    # An image whose pandas is broken: the validation module cannot be imported at all.
    monkeypatch.setitem(sys.modules, "credit_risk.validation", None)
    validator = load_module("docker-data-validity/data-validity.py", "data_validity_broken")
    answers = []
    monkeypatch.setattr(validator, "report_task", lambda token, **answer: answers.append(answer))
    monkeypatch.setenv("TASK_TOKEN", "token")
    monkeypatch.setenv("BUCKET", "bucket")
    with pytest.raises(ImportError):
        validator.main([])
    assert len(answers) == 1 and "error" in answers[0]


def test_the_choice_reads_the_key_the_validator_returns(tmp_path):
    result_path = DEFINITION["States"]["ValidateData"]["ResultPath"]
    choice = DEFINITION["States"]["AllEntitiesReady"]
    key = choice["Choices"][0]["Variable"].removeprefix(f"{result_path}.")
    answer = run_validation(FolderStore(tmp_path), log=lambda *_: None)  # an empty bucket
    assert answer[key] is False
    assert DEFINITION["States"][choice["Default"]]["Type"] == "Fail"
    cause = DEFINITION["States"][choice["Default"]]["CausePath"].removeprefix(f"{result_path}.")
    assert "sba_loans 0/3" in answer[cause]


def test_no_pass_state_and_no_entity_named_in_the_definition():
    assert all(s["Type"] != "Pass" for s in DEFINITION["States"].values())
    text = json.dumps(DEFINITION)
    for entity in ENTITIES:
        assert entity.name not in text


def test_the_event_rule_matches_only_the_marker():
    assert PATTERN["detail"]["object"]["key"] == [READY_MARKER]


def test_whole_sample_batch_runs_to_done_and_archives(tmp_path):
    root, table = tmp_path / "bucket", tmp_path / "dynamodb" / "results.json"
    stager.main(["--local", "--data", "sample", "--root", str(root)])
    terminal, data = runner.run(root, table, bootstrap=20, log=lambda *_: None)
    assert terminal == "Done"
    assert data["validation"]["gate_passed"] is True
    for entity in ENTITIES:
        assert len(list((root / entity.archived).iterdir())) == entity.expected_files
        assert not (root / entity.ready).exists() or not any((root / entity.ready).iterdir())
    for name in ("live_book.csv", "scorecard_points.csv", "hazard_curve.csv"):
        assert (root / "results" / "sba" / name).stat().st_size > 0
    items = json.loads(table.read_text())
    for key in ("sba|book#open", "sba|scorecard#holdout", "fdic|pd#test#full",
                "fdic|loss#out_of_time#test#excluded", "fdic|counterparty#summary", "run|latest"):
        assert key in items, key
    live = (root / "results" / "sba" / "live_book.csv").read_text().splitlines()[0]
    for borrower in ("BorrName", "BorrStreet", "BorrCity", "BorrZip"):
        assert borrower not in live


def test_files_promoted_mid_run_are_left_for_the_next_run_not_archived(tmp_path, monkeypatch):
    root = tmp_path / "bucket"
    stager.main(["--local", "--data", "sample", "--root", str(root)])
    run_validation(FolderStore(root), log=lambda *_: None)
    loans = next(e for e in ENTITIES if e.name == "sba_loans")
    replaced = sorted((root / loans.ready).iterdir())[0]
    fit_sba = pipeline.run_sba

    def second_execution_lands_while_modelling(*args, **kwargs):
        out = fit_sba(*args, **kwargs)
        lines = replaced.read_text().splitlines(keepends=True)
        replaced.write_text("".join(lines[:-1]))  # same name, new content
        (root / loans.ready / "FOIA_7a_extra.csv").write_text(lines[0])  # a new name
        return out

    monkeypatch.setattr(pipeline, "run_sba", second_execution_lands_while_modelling)
    pipeline.run_models(FolderStore(root), JsonTable(tmp_path / "items.json"), n_boot=20,
                        log=lambda *_: None)
    assert sorted(p.name for p in (root / loans.ready).iterdir()) == sorted(
        [replaced.name, "FOIA_7a_extra.csv"])
    assert len(list((root / loans.archived).iterdir())) == loans.expected_files - 1
    for entity in ENTITIES:
        if entity is not loans:
            assert len(list((root / entity.archived).iterdir())) == entity.expected_files


def test_short_batch_ends_in_the_fail_state_with_its_cause(tmp_path):
    root, table = tmp_path / "bucket", tmp_path / "dynamodb" / "results.json"
    stager.main(["--local", "--data", "sample", "--root", str(root)])
    next((root / "new" / "sba_loans").glob("*FY2020*")).unlink()
    terminal, data = runner.run(root, table, bootstrap=20, log=lambda *_: None)
    assert terminal == "GateFailed"
    assert "sba_loans 2/3" in data["validation"]["summary"]
    assert not table.exists()  # the model never ran


def test_items_carry_decimal_never_float_and_drop_nan():
    item = to_item("sba", "x", {"a": 0.1, "b": float("nan"), "c": 3, "d": "text"})
    assert item["a"] == Decimal("0.1") and isinstance(item["c"], Decimal)
    assert "b" not in item and item["d"] == "text"
