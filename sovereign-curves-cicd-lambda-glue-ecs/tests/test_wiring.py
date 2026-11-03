"""The wiring between the pieces: workflows, EventBridge, the task definition and Athena.

None of these files runs in a unit test, so each is held to what the rest of the project
assumes of it: the deploy guard, the OIDC-only credentials, the path filters, the runtimes, the
event pattern against a real event's shape, and every Athena column against the Parquet schema.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from bond_analytics import schemas

PROJECT = Path(__file__).resolve().parents[1]
REPO = PROJECT.parent
WORKFLOWS = {name: REPO / ".github" / "workflows" / f"sovereign-curves-{name}.yml" for name in ("lambda", "glue", "ecs")}
CURRENT = {"actions/checkout": "v7", "actions/setup-python": "v7", "aws-actions/configure-aws-credentials": "v6",
           "aws-actions/amazon-ecr-login": "v2", "aws-actions/amazon-ecs-render-task-definition": "v1",
           "aws-actions/amazon-ecs-deploy-task-definition": "v2"}


def workflow(name):
    doc = yaml.safe_load(WORKFLOWS[name].read_text())
    doc["on"] = doc.pop(True, doc.get("on"))  # YAML 1.1 reads the key `on` as true
    return doc


@pytest.mark.parametrize("name", WORKFLOWS)
def test_push_to_main_and_pull_requests_share_one_path_filter(name):
    on = workflow(name)["on"]
    assert on["push"]["branches"] == ["main"]
    assert on["push"]["paths"] == on["pull_request"]["paths"]
    paths = on["push"]["paths"]
    assert f".github/workflows/sovereign-curves-{name}.yml" in paths
    assert all(p.startswith(("sovereign-curves-cicd-lambda-glue-ecs/", ".github/workflows/sovereign-curves-")) for p in paths)


@pytest.mark.parametrize("name", WORKFLOWS)
def test_deploy_runs_only_on_a_push_to_main_with_its_own_role_variable(name):
    jobs = workflow(name)["jobs"]
    deploy = jobs["deploy"]
    var = f"SOVEREIGN_CURVES_{name.upper()}_ROLE_ARN"
    assert deploy["if"] == f"github.event_name == 'push' && github.ref == 'refs/heads/main' && vars.{var} != ''"
    assert deploy["needs"] == "test"
    assert deploy["permissions"] == {"id-token": "write", "contents": "read"}
    assert workflow(name)["permissions"] == {"contents": "read"}
    assert "if" not in jobs["test"]
    creds = [s for s in deploy["steps"] if s.get("uses", "").startswith("aws-actions/configure-aws-credentials")]
    assert len(creds) == 1 and set(creds[0]["with"]) == {"role-to-assume", "aws-region"}
    assert creds[0]["with"]["role-to-assume"] == "${{ vars.%s }}" % var


@pytest.mark.parametrize("name", WORKFLOWS)
def test_no_deprecated_output_command_no_keys_and_current_actions(name):
    text = WORKFLOWS[name].read_text()
    assert "::set-output" not in text
    assert "aws-access-key-id" not in text and "aws-secret-access-key" not in text and "secrets." not in text
    for action, version in re.findall(r"uses:\s*([\w.-]+/[\w.-]+)@(\S+)", text):
        assert CURRENT[action] == version, action


def test_runtimes():
    assert "python-version: \"3.13\"" in WORKFLOWS["lambda"].read_text()
    assert "--runtime python3.13" in WORKFLOWS["lambda"].read_text()
    assert "FROM public.ecr.aws/docker/library/python:3.13-slim" in (PROJECT / "ecs" / "Dockerfile").read_text()
    glue = workflow("glue")
    assert glue["jobs"]["test"]["container"] == "public.ecr.aws/docker/library/python:3.9-slim"
    text = WORKFLOWS["glue"].read_text()
    assert '"Command": {"Name": "pythonshell", "PythonVersion": "3.9"' in text
    assert '"MaxCapacity": 0.0625' in text and '"library-set": "analytics"' in text
    pins = dict(line.split("==") for line in (PROJECT / "glue-pythonshell" / "requirements.txt").read_text().splitlines()
                if "==" in line)
    assert pins == {"pandas": "1.4.2", "numpy": "1.22.3", "boto3": "1.21.21", "pyarrow": "7.0.0"}
    assert f'"--additional-python-modules": "pyarrow=={pins["pyarrow"]}"' in text


def test_ecs_registers_a_task_definition_and_retargets_the_rule_without_a_service():
    steps = workflow("ecs")["jobs"]["deploy"]["steps"]
    register = next(s for s in steps if s.get("uses", "").startswith("aws-actions/amazon-ecs-deploy-task-definition"))
    assert set(register["with"]) == {"task-definition"}  # no service, so nothing to wait on
    render = next(s for s in steps if s.get("uses", "").startswith("aws-actions/amazon-ecs-render-task-definition"))
    taskdef = json.loads((PROJECT / "ecs" / "task-definition.json").read_text())
    assert render["with"]["container-name"] == taskdef["containerDefinitions"][0]["name"]
    assert "${{ github.sha }}" in WORKFLOWS["ecs"].read_text()
    rule = json.loads((PROJECT / "eventbridge" / "glue-succeeded-rule.json").read_text())
    assert f"--rule {rule['Name']}" in WORKFLOWS["ecs"].read_text()
    targets = json.loads((PROJECT / "eventbridge" / "glue-succeeded-targets.json").read_text())
    assert f"task-definition/{taskdef['family']}:" in targets[0]["EcsParameters"]["TaskDefinitionArn"]


def matches(pattern, event):
    """EventBridge's exact-value matching: every key in the pattern, one of its listed values."""
    for k, want in pattern.items():
        if isinstance(want, dict):
            if not isinstance(event.get(k), dict) or not matches(want, event[k]):
                return False
        elif event.get(k) not in want:
            return False
    return True


def test_the_glue_rule_fires_on_success_only():
    pattern = json.loads(json.loads((PROJECT / "eventbridge" / "glue-succeeded-rule.json").read_text())["EventPattern"])
    event = json.loads((PROJECT / "eventbridge" / "sample-glue-succeeded-event.json").read_text())
    assert matches(pattern, event)
    for state in ("FAILED", "TIMEOUT", "STOPPED"):
        assert not matches(pattern, {**event, "detail": {**event["detail"], "state": state}})
    assert not matches(pattern, {**event, "detail": {**event["detail"], "jobName": "another-job"}})


def test_the_schedule_is_weekday_evenings_utc():
    schedule = json.loads((PROJECT / "eventbridge" / "fetch-schedule.json").read_text())
    assert schedule["ScheduleExpression"] == "cron(30 23 ? * MON-FRI *)"


def ddl_tables():
    text = (PROJECT / "athena" / "create-tables.sql").read_text()
    out = {}
    for name, body in re.findall(r"CREATE EXTERNAL TABLE IF NOT EXISTS sovereign_curves\.(\w+) \((.*?)\n\)", text, re.S):
        out[name] = [tuple(re.match(r"\s*`(\w+)` (\w+)", line).groups()) for line in body.strip().split(",\n")]
    return out


def test_athena_columns_are_the_parquet_schema():
    ddl = ddl_tables()
    assert set(ddl) == set(schemas.TABLES)
    for table, cols in schemas.TABLES.items():
        assert ddl[table] == cols, table


def test_every_s3_key_is_built_posix():
    for path in [PROJECT / "glue-pythonshell" / "load.py", PROJECT / "lambda-functions" / "fetch.py", PROJECT / "ecs" / "app.py"]:
        text = path.read_text()
        assert "str(Path(" not in text and "os.path.join" not in text, path.name
