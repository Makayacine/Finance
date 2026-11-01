"""The gate: one entity list, exact dtypes with one widening, and a short batch that fails.

Every test here builds real files in a temporary folder and runs the validator's own code over
them through ``FolderStore`` -- the same class the ``--local`` path uses, with the same
``new/<entity>/`` keys the bucket would hold.

The two dtype rules get a test each, because each is a one-line decision that a later edit could
reverse without anything else failing: a nullable integer declared ``float64`` must pass with a
blank in it and without one, and an ``int64`` declaration must still reject the blank.
"""

import shutil

import pandas as pd
import pytest

from conftest import PROJECT, SAMPLE
from credit_risk.config import ENTITIES, READY_MARKER, entity
from credit_risk.stores import FolderStore
from credit_risk.validation import run_validation, validate_file
from credit_risk.config import Entity

TOY = Entity("toy", "test", 1, "toy/*.csv", {"id": "int64", "amount": "float64", "zip": "str"},
             string_columns=("zip",), never_read=("secret",))


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_every_entity_derives_its_folders_from_its_name():
    for item in ENTITIES:
        assert item.landing == f"new/{item.name}"
        assert item.ready == item.name
        assert item.archived == f"archived/{item.name}"
    assert entity("sba_loans").expected_files == 3
    assert entity("fdic_financials").expected_files == 2


def test_nullable_integer_declared_float_passes_with_and_without_a_blank(tmp_path):
    with_blank = write(tmp_path / "a.csv", "id,amount,zip\n1,10,02134\n2,,02135\n")
    without = write(tmp_path / "b.csv", "id,amount,zip\n1,10,02134\n2,20,02135\n")
    assert validate_file(with_blank, TOY)[0]
    assert validate_file(without, TOY)[0]  # int64 inferred, float64 declared: the one widening


def test_int64_declaration_rejects_a_blank(tmp_path):
    path = write(tmp_path / "a.csv", "id,amount,zip\n1,10,02134\n,20,02135\n")
    ok, reason, _ = validate_file(path, TOY)
    assert not ok and "id is float64, declared int64" in reason


def test_text_in_a_numeric_column_fails(tmp_path):
    path = write(tmp_path / "a.csv", "id,amount,zip\n1,ten,02134\n")
    ok, reason, _ = validate_file(path, TOY)
    assert not ok and "amount" in reason


def test_codes_are_read_as_strings_so_leading_zeros_survive(tmp_path):
    path = write(tmp_path / "a.csv", "id,amount,zip\n1,10,02134\n")
    assert validate_file(path, TOY)[0]
    assert pd.read_csv(path, dtype={"zip": "str"}).zip[0] == "02134"


def test_never_read_columns_may_be_present_and_others_may_not(tmp_path):
    allowed = write(tmp_path / "a.csv", "id,amount,zip,secret\n1,10,02134,x\n")
    extra = write(tmp_path / "b.csv", "id,amount,zip,surprise\n1,10,02134,x\n")
    assert validate_file(allowed, TOY)[0]
    ok, reason, _ = validate_file(extra, TOY)
    assert not ok and "surprise" in reason


def test_the_committed_sample_passes_its_own_validator():
    for item in ENTITIES:
        root = PROJECT / "data" if item.shared_source else SAMPLE
        files = sorted(root.glob(item.source_glob))
        assert len(files) == item.expected_files
        for path in files:
            ok, reason, rows = validate_file(path, item)
            assert ok, f"{path.name}: {reason}"
            assert rows > 0


def stage(root):
    for item in ENTITIES:
        source = PROJECT / "data" if item.shared_source else SAMPLE
        for path in sorted(source.glob(item.source_glob)):
            target = root / item.landing / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    write(root / READY_MARKER, "")


def test_a_whole_batch_passes_promotes_and_consumes_the_marker(tmp_path):
    stage(tmp_path)
    store = FolderStore(tmp_path)
    out = run_validation(store, log=lambda *_: None)
    assert out["gate_passed"]
    assert out["valid_files"] == {e.name: e.expected_files for e in ENTITIES}
    assert store.list("new/") == []
    assert not (tmp_path / READY_MARKER).exists()


def test_a_missing_file_fails_the_gate_and_says_which(tmp_path):
    stage(tmp_path)
    next((tmp_path / "new" / "sba_loans").glob("*FY2020*")).unlink()
    out = run_validation(FolderStore(tmp_path), log=lambda *_: None)
    assert not out["gate_passed"]
    assert "sba_loans 2/3" in out["summary"]


def test_a_stray_extra_file_also_fails_the_gate(tmp_path):
    stage(tmp_path)
    source = next((tmp_path / "new" / "sba_loans").glob("*FY2020*"))
    shutil.copyfile(source, source.with_name("FOIA_7a_extra_copy.csv"))
    out = run_validation(FolderStore(tmp_path), log=lambda *_: None)
    assert not out["gate_passed"] and "sba_loans 4/3" in out["summary"]


def test_an_invalid_file_stays_in_new_and_is_named(tmp_path):
    stage(tmp_path)
    write(tmp_path / "new" / "fdic_financials" / "bad.csv", "CERT,REPDTE\nabc,1\n")
    out = run_validation(FolderStore(tmp_path), log=lambda *_: None)
    assert (tmp_path / "new" / "fdic_financials" / "bad.csv").exists()
    assert out["invalid_files"][0]["key"] == "new/fdic_financials/bad.csv"
    assert out["valid_files"]["fdic_financials"] == 2  # the good two were still promoted
    assert not out["gate_passed"]
