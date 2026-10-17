#!/usr/bin/env python3
"""Run a trusted standalone reproducer on recorded Click snapshots; never run agent patches."""
import argparse
import hashlib
import io
import json
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def run_snapshot(repo, revision, reproducer, python, repetitions, source_mode="import", *, archive=None):
    if repetitions < 1:
        raise ValueError("Repetitions must be positive")
    if source_mode not in {"import", "reproducer_import"}:
        raise ValueError("Unknown source mode: " + source_mode)
    runs = []
    with tempfile.TemporaryDirectory(prefix="click-record-") as folder:
        root = Path(folder)
        if archive is None:
            archive = subprocess.check_output(["git", "-C", str(repo), "archive", revision])
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(root, filter="data")
        source = root / "src" if (root / "src" / "click").is_dir() else root
        # Isolated Python ignores PYTHONPATH and the user site. Explicit source path wins.
        if source_mode == "import":
            provenance = (
                "import click; "
                f"assert pathlib.Path(click.__file__).is_relative_to({str(root)!r}); "
                "print('CLICK_SOURCE=' + click.__file__, flush=True); "
            )
        else:
            # Import-failure tests must catch their own exception. Locate the
            # selected source without claiming that it was imported successfully.
            entry = source / "click" / "__init__.py"
            if not entry.is_file():
                entry = source / "click.py"
            if not entry.is_file():
                raise ValueError("Snapshot has no Click import entry point")
            provenance = f"print('CLICK_SOURCE_LOCATION=' + {str(entry)!r}, flush=True); "
        code = (
            "import pathlib, runpy, sys; "
            f"sys.path.insert(0, {str(source)!r}); "
            + provenance +
            f"runpy.run_path({str(reproducer.resolve())!r}, run_name='__main__')"
        )
        for _ in range(repetitions):
            try:
                result = subprocess.run([str(python), "-I", "-c", code],
                                        capture_output=True, text=True, timeout=30, cwd=root)
                # Normalize temp paths so evidence remains stable across repeats.
                runs.append({"exit_code": result.returncode,
                             "stdout": result.stdout.replace(str(root), "<SNAPSHOT>"),
                             "stderr": result.stderr.replace(str(root), "<SNAPSHOT>"),
                             "timed_out": False})
            except subprocess.TimeoutExpired:
                runs.append({"exit_code": None, "stdout": "", "stderr": "timeout", "timed_out": True})
    return runs


def runtime_info(python, dependencies):
    if not isinstance(dependencies, list) or any(not isinstance(name, str) for name in dependencies):
        raise ValueError("Reproducer dependencies must be distribution names")
    code = (
        "import importlib.metadata, json, platform, sys; "
        f"names = {dependencies!r}; "
        "print(json.dumps({'version':platform.python_version(), "
        "'implementation':platform.python_implementation(), 'executable':sys.executable, "
        "'platform':platform.platform(), 'third_party_dependencies': "
        "[{'name':name, 'version':importlib.metadata.version(name)} for name in names]}))"
    )
    return json.loads(subprocess.check_output([str(python), "-I", "-c", code],
                                             text=True, stderr=subprocess.PIPE))


def verify(record_dir, repo, python, repetitions=3, *, runtime_loader=None,
           snapshot_runner=None, emit=True):
    if repetitions < 1:
        raise ValueError("Repetitions must be positive")
    # Keep a venv interpreter's symlink path: resolving it would bypass the venv.
    record_dir, repo, python = record_dir.resolve(), repo.resolve(), python.absolute()
    path = record_dir / "record.json"
    record = json.loads(path.read_text())
    reproducer = record_dir / record["reproducer"]["path"]
    if not record["reproducer"].get("failure_marker"):
        raise ValueError("Reproducer requires a specific expected failure marker before validation")
    evidence_dir = record_dir / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    source_mode = record["reproducer"].get("source_mode", "import")
    # Record requested distributions from the actual selected interpreter.
    # Standard-library-only reproductions retain an empty dependency list.
    dependencies = record["reproducer"].get("dependencies", [])
    runtime = dict((runtime_loader or runtime_info)(python, dependencies))
    runtime.update({"source_install": "direct snapshot import",
                    "source_mode": source_mode,
                    "reproducer_sha256": hashlib.sha256(reproducer.read_bytes()).hexdigest()})
    write_json(record_dir / "runtime.lock", runtime)
    runner = snapshot_runner or run_snapshot
    before = runner(repo, record["before_revision"], reproducer, python, repetitions, source_mode)
    fixed = runner(repo, record["fixed_revision"], reproducer, python, repetitions, source_mode)
    marker = record["reproducer"]["failure_marker"]
    provenance_marker = "CLICK_SOURCE=" if source_mode == "import" else "CLICK_SOURCE_LOCATION="
    passed = all(r["exit_code"] == 1 and marker in r["stderr"]
                 and provenance_marker in r["stdout"] and not r["timed_out"] for r in before)
    passed &= all(r["exit_code"] == 0 and "OK" in r["stderr"]
                  and provenance_marker in r["stdout"] for r in fixed)
    evidence = {"before_revision": record["before_revision"],
                "fixed_revision": record["fixed_revision"],
                "before": before, "fixed": fixed, "runtime": runtime,
                "repetitions": repetitions, "verified_fail_to_pass": passed}
    write_json(evidence_dir / "paired_runs.json", evidence)
    record["runtime"] = {"python": runtime["version"], "dependency_lock": "runtime.lock", "status": "verified"}
    record["verification"] = {"status": "validated" if passed else "failed_validation",
                              "evidence": "evidence/paired_runs.json",
                              "before": "expected_failure" if passed else "inspect_evidence",
                              "fixed": "pass" if passed else "inspect_evidence"}
    record["status"] = "validated" if passed else "candidate"
    if passed:
        record["snapshot_results"] = [
            {"revision": record["before_revision"], "result": "bug_present",
             "evidence": "evidence/paired_runs.json", "repetitions": repetitions},
            {"revision": record["fixed_revision"], "result": "behavior_passes",
             "evidence": "evidence/paired_runs.json", "repetitions": repetitions},
        ]
    else:
        # The evidence file was replaced; old passing snapshot entries no
        # longer describe it and must not remain eligible for the reward bank.
        record["snapshot_results"] = []
    write_json(path, record)
    if emit:
        print(json.dumps({"bug_id": record["bug_id"], "verified_fail_to_pass": passed}))
    return passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    if not verify(args.record, args.repo, args.python):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
