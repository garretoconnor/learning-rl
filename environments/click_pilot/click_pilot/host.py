"""Trusted stdlib-only controller, launched with the bundle's pinned Python."""
import json
from pathlib import Path
import re
import sys


def main(request):
    run_id = request["run_id"]
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Invalid rollout identity")
    root = Path(request["root"]).resolve()
    bundle = Path(request["bundle"]).resolve()
    sys.path.insert(0, str(root / "code_testing_eval"))
    from pilot.environment import check_workspace, export, grade, load_bundle
    from pilot.isolation import run
    run_dir = bundle / "verifiers_runs" / run_id
    workspace = run_dir / "workspace"
    action = request["action"]
    if action == "new":
        run_dir.mkdir(parents=True, exist_ok=False)
        export(bundle, workspace)
        return {"ready": True}
    if action == "exec":
        command = request["command"]
        if not isinstance(command, str) or len(command.encode()) > 32768:
            raise ValueError("Command must be at most 32 KiB")
        check_workspace(load_bundle(bundle), workspace)
        result = run(workspace, ["/bin/bash", "--noprofile", "--norc", "-c", command], editable=True)
        for name in ("stdout", "stderr"):
            result[name] = result[name].replace(str(workspace), "<WORKSPACE>")
        return result
    if action == "check":
        # Record invalid source edits as an invalid submission during scoring.
        return {"exists": workspace.is_dir()}
    if action == "grade":
        result = grade(bundle, workspace / "submissions", run_dir / "grading.json", workspace=workspace)
        return {"status": result["status"], "score": result["score"],
                "matched": len(result.get("matched_bugs", [])),
                "needs_review": len(result.get("needs_review", []))}
    raise ValueError("Unknown host action")


if __name__ == "__main__":
    print(json.dumps(main(json.loads(sys.argv[1]))))
