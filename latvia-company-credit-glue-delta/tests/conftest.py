"""Shared fixtures: the project on sys.path, one local Spark session with Delta, a scratch bucket.

No AWS and no mocks: the Delta tests write real Delta tables into pytest's temporary folders with
the same session builder the Glue jobs use under ``--local``. delta-spark fetches the Delta Lake
jar from Maven Central the first time Spark starts and serves it from the local Ivy cache after.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
for path in (PROJECT, PROJECT / "glue-jobs", PROJECT / "local-development"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_job(name):
    """Import a Glue job whose file name has hyphens in it."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"),
                                                  PROJECT / "glue-jobs" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def spark():
    import lakehouse

    session = lakehouse.build_session("tests", local=True, shuffle_partitions=2,
                                      driver_memory="2g")
    yield session
    session.stop()


@pytest.fixture()
def bucket(tmp_path):
    import lakehouse

    return lakehouse.LocalBucket(tmp_path / "bucket")
