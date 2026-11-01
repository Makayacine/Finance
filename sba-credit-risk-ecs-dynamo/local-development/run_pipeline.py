"""Walk ``step-functions/step-function.json`` on a laptop, state by state, with folders for S3.

    stage_landing.py --local    ->    run_pipeline.py --local
                                       ValidateData      Task    -> data-validity.py --local
                                       AllEntitiesReady  Choice  -> the JSON's own rule
                                       RunCreditModels   Task    -> credit-model.py --local
                                       Done / GateFailed / ...   -> exit 0 / exit 1

The runner reads the state machine and does what it says, rather than calling the two containers
in a hard-coded order. That is what makes the local run a rehearsal of the deployed flow: the
Choice is evaluated from its ``Variable`` and ``BooleanEquals`` against the validator's real
output at the real ``ResultPath``, a failed gate lands in the real Fail state with the
``CausePath`` the console would show, and a mistyped path in the JSON breaks this run the same
way it would break the execution.

It interprets the subset of the Amazon States Language the definition uses -- Task, Choice with
BooleanEquals, Fail with CausePath, Succeed, ResultPath, Catch on States.ALL -- and refuses any
other state type rather than guessing at it. A task token has no meaning locally: the validator's
``--local`` path returns its answer directly, which is the payload ``SendTaskSuccess`` carries on
AWS.

    python local-development/run_pipeline.py --local
    python local-development/run_pipeline.py --local --engine docker   # the two built images
"""

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEFINITION = PROJECT / "step-functions" / "step-function.json"
ENTRYPOINTS = {
    "data-validity": PROJECT / "docker-data-validity" / "data-validity.py",
    "credit-model": PROJECT / "docker-credit-model" / "credit-model.py",
}
IMAGES = {"data-validity": "sba-data-validity", "credit-model": "sba-credit-model"}


def resolve(data, path):
    """A ``$.a.b`` JSONPath into ``data``; the only form the definition uses."""
    node = data
    for part in path.removeprefix("$").strip(".").split("."):
        if part:
            node = node[part]
    return node


def assign(data, path, value):
    parts = path.removeprefix("$").strip(".").split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ENTRYPOINTS[name])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_container(name, root, table_file, engine, bootstrap):
    with tempfile.TemporaryDirectory(dir=root.parent) as scratch:
        answer = Path(scratch) / "answer.json"
        if engine == "python":
            if name == "data-validity":
                return load(name).main(["--local", "--root", str(root), "--output", str(answer)])
            return load(name).main(["--local", "--root", str(root), "--table-file",
                                    str(table_file), "--bootstrap", str(bootstrap)])
        work = root.parent.resolve()
        mount = ["docker", "run", "--rm", "-v", f"{work}:/work", IMAGES[name], "--local",
                 "--root", f"/work/{root.name}"]
        if name == "data-validity":
            subprocess.run(mount + ["--output", f"/work/{Path(scratch).name}/answer.json"],
                           check=True)
            return json.loads(answer.read_text(encoding="utf-8"))
        table = table_file.resolve().relative_to(work).as_posix()
        subprocess.run(mount + ["--table-file", f"/work/{table}", "--bootstrap",
                                str(bootstrap)], check=True)
        return {"exit_code": 0}


def run(root, table_file, engine="python", bootstrap=500, log=print):
    """Execute the definition; returns ``(terminal state name, state data)``."""
    definition = json.loads(DEFINITION.read_text(encoding="utf-8"))
    data = {"detail": {"bucket": {"name": "local"}, "object": {"key": "new/_READY"}}}
    name = definition["StartAt"]
    while True:
        state = definition["States"][name]
        kind = state["Type"]
        log(f"-> {name} ({kind})")
        if kind == "Task":
            container = state["Parameters"]["Overrides"]["ContainerOverrides"][0]["Name"]
            try:
                result = run_container(container, root, table_file, engine, bootstrap)
                assign(data, state["ResultPath"], result)
                name = state["Next"]
            except Exception as error:  # the Catch on States.ALL
                catch = state["Catch"][0]
                assign(data, catch["ResultPath"], {"Error": type(error).__name__,
                                                   "Cause": str(error)})
                name = catch["Next"]
        elif kind == "Choice":
            name = state["Default"]
            for rule in state["Choices"]:
                if resolve(data, rule["Variable"]) is rule["BooleanEquals"]:
                    name = rule["Next"]
                    break
        elif kind == "Fail":
            log(f"   FAILED {state['Error']}: {resolve(data, state['CausePath'])}")
            return name, data
        elif kind == "Succeed":
            return name, data
        else:
            raise NotImplementedError(f"state type {kind} is not in this definition's subset")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", required=True)
    parser.add_argument("--root", default=str(Path("_localrun") / "bucket"))
    parser.add_argument("--table-file", default=str(Path("_localrun") / "dynamodb"
                                                    / "sba_credit_risk_results.json"))
    parser.add_argument("--engine", choices=["python", "docker"], default="python")
    parser.add_argument("--bootstrap", type=int, default=500)
    args = parser.parse_args(argv)
    terminal, _ = run(Path(args.root), Path(args.table_file), args.engine, args.bootstrap)
    sys.exit(0 if definition_type(terminal) == "Succeed" else 1)


def definition_type(name):
    return json.loads(DEFINITION.read_text(encoding="utf-8"))["States"][name]["Type"]


if __name__ == "__main__":
    main()
