#!/usr/bin/env python3
"""Apply an evidence-backed review and define a record, including previously missed fixes."""
import argparse
import hashlib
import json
import re
from pathlib import Path

from catalog_click import dump, git, production, test_file


def define(repo, root, commit, claim, kind, reason, eligibility):
    sha = git(repo, "rev-parse", commit + "^{commit}").decode().strip()
    private = root / "private_grading"
    inventory_path = private / "commit_inventory.jsonl"
    inventory = [json.loads(line) for line in inventory_path.read_text().splitlines()]
    item = next(i for i in inventory if i["sha"] == sha)
    parent = item["parents"][0] if item["parents"] else None
    location = private / "bugs" / ("click-" + sha)
    location.mkdir(parents=True, exist_ok=True)
    path = location / "record.json"
    template = json.loads((root / "templates/record.example.json").read_text())
    record = json.loads(path.read_text()) if path.exists() else template
    diff = ["diff", parent, sha] if parent else ["show", "--format=", sha]
    changed = git(repo, *diff, "--name-only").decode().splitlines()
    code = [p for p in changed if production(p)]
    tests = [p for p in changed if test_file(p)]
    for filename, paths in [("fix.patch", code), ("regression.patch", tests)]:
        (location / filename).write_bytes(git(repo, *diff, "--binary", "--", *paths) if paths else b"")
    (location / "full.patch").write_bytes(git(repo, *diff, "--binary"))
    review = {"classification": "bugfix", "kind": kind,
              "reason": reason, "evaluation_eligibility": eligibility}
    record.update({"bug_id": "click-" + sha, "repository": "https://github.com/pallets/click",
                   "source_commit": f"https://github.com/pallets/click/commit/{sha}",
                   "title": item["title"], "author_date": item["author_date"],
                   "upstream_reported_description": item["title"] + "\n" + item["body"],
                   "before_revision": parent, "fixed_revision": sha, "parents": item["parents"],
                   "production_files": code, "test_files": tests, "behavioral_claim": claim,
                   "review": review, "fix_patch": "fix.patch", "regression_patch": "regression.patch",
                   "full_patch": "full.patch", "pr_or_issue_numbers": item["pr_or_issue_numbers"],
                   "discovery_reasons": sorted(set(item["discovery_reasons"] + ["manual_review"]))})
    if not record["contract_evidence"]:
        record["contract_evidence"] = [{"kind": "reviewed_commit_diff", "path": "full.patch", "note": reason}]
    dump(path, record)
    decisions_path = private / "reviews/decisions.json"
    decisions = json.loads(decisions_path.read_text())
    decisions[sha] = review
    dump(decisions_path, decisions)
    item.update(review=review, record=str(path.relative_to(root)),
                production_files=code, test_files=tests, discovery_reasons=record["discovery_reasons"])
    serialized = "".join(json.dumps(i, sort_keys=True) + "\n" for i in inventory)
    inventory_path.write_text(serialized)
    print(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--claim", required=True)
    parser.add_argument("--kind", choices=["runtime", "documentation", "tests", "build", "mixed"], required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--eligibility", default="not_assessed")
    args = parser.parse_args()
    define(args.repo, args.root, args.commit, args.claim, args.kind, args.reason, args.eligibility)
