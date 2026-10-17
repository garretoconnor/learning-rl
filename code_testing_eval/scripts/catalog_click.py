#!/usr/bin/env python3
"""Collect Click history without mistaking automated discovery for an exhaustive audit."""
import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

KEYWORDS = re.compile(
    r"\b(fix(?:e[ds])?|bug(?:fix)?|regression|incorrect|wrong|crash|broken|handle|avoid|"
    r"prevent|restore|respect|ensure|preserve|error|failure|fail|leak|race)\b", re.I
)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def production(path):
    # Click started as a single module before moving to a package and then src/.
    return path == "click.py" or path.startswith(("src/click/", "click/"))


def test_file(path):
    return path.startswith(("tests/", "test/")) or path in ("conftest.py", "tox.ini", "pytest.ini")


def collect(repo, output, pinned=None):
    private = output / "private_grading"
    private.mkdir(parents=True, exist_ok=True)
    refs = pinned["refs"] if pinned else dict(line.split(" ", 1) for line in git(
        repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes/origin", "refs/tags"
    ).decode().splitlines())
    head = pinned["default_head"] if pinned else git(repo, "rev-parse", "HEAD").decode().strip()
    tips = sorted(set(refs.values()))
    metadata = git(repo, "log", "--topo-order", "--format=%H%x00%P%x00%aI%x00%s%x00%b%x1e", *tips)
    commits = []
    for entry in metadata.decode(errors="replace").split("\x1e"):
        if not entry.strip():
            continue
        sha, parents, date, title, body = entry.strip("\n").split("\0", 4)
        commits.append({"sha": sha, "parents": parents.split(), "author_date": date,
                        "title": title, "body": body.strip()})
    # Current change log supplies another discovery signal, including fixes without "fix" in titles.
    changelog_path = "CHANGES.md"
    try:
        changelog = git(repo, "show", f"{head}:{changelog_path}").decode()
    except subprocess.CalledProcessError:
        changelog_path = "CHANGES.rst"
        changelog = git(repo, "show", f"{head}:{changelog_path}").decode()
    (private / "upstream_changes.txt").write_text(changelog)
    release_refs = set()
    release_notes = []
    for paragraph in re.split(r"\n(?=-\s|\*\s)", changelog):
        if KEYWORDS.search(paragraph):
            references = sorted(set(re.findall(r"(?:\{(?:pr|issue)\}|:(?:pr|issue):)`(\d+)`", paragraph)), key=int)
            release_refs.update(references)
            release_notes.append({"text": paragraph.strip(), "references": references,
                                  "review_status": "unreviewed"})
    reviews_path = private / "reviews" / "decisions.json"
    reviews = json.loads(reviews_path.read_text()) if reviews_path.exists() else {}
    inventory = []
    counts = Counter()
    patch_groups = {}
    for i, commit in enumerate(commits):
        sha = commit["sha"]
        parents = commit["parents"]
        before = parents[0] if parents else None
        diff_args = ["diff", before, sha] if before else ["show", "--format=", sha]
        changed = git(repo, *diff_args, "--name-only").decode().splitlines()
        message = commit["title"] + "\n" + commit["body"]
        prs = sorted(set(re.findall(r"(?:#|pull/)(\d+)\b", message)), key=int)
        reasons = []
        if KEYWORDS.search(message):
            reasons.append("message_keyword")
        if set(prs) & release_refs:
            reasons.append("release_note_reference")
        review = reviews.get(sha)
        if review and review["classification"] == "bugfix":
            reasons.append("manual_review")
        code_paths = [p for p in changed if production(p)]
        test_paths = [p for p in changed if test_file(p)]
        if code_paths and test_paths:
            reasons.append("production_and_tests_changed")
        candidate = bool(reasons)
        item = {**commit, "changed_files": changed, "production_files": code_paths,
                "test_files": test_paths, "pr_or_issue_numbers": prs,
                "discovery_reasons": reasons,
                "review": review or {"classification": "unreviewed"},
                "record": None}
        counts["commits"] += 1
        counts["review_" + item["review"]["classification"]] += 1
        if candidate:
            record_id = "click-" + sha
            location = private / "bugs" / record_id
            location.mkdir(parents=True, exist_ok=True)
            # Preserve curated fields. Generated provenance and patch artifacts remain reproducible.
            record_path = location / "record.json"
            record = json.loads(record_path.read_text()) if record_path.exists() else {
                "schema_version": 1, "bug_id": record_id, "status": "candidate",
                "behavioral_claim": None, "contract_evidence": [],
                "reproducer": {"path": None, "test_command": None, "status": "not_extracted"},
                "runtime": {"python": None, "dependency_lock": None, "status": "not_verified"},
                "verification": {"status": "not_run", "before": None, "fixed": None},
                "snapshot_results": [], "duplicate_of": None,
            }
            record.update({"repository": "https://github.com/pallets/click",
                           "upstream_reported_description": message,
                           "source_commit": f"https://github.com/pallets/click/commit/{sha}",
                           "title": commit["title"], "author_date": commit["author_date"],
                           "before_revision": before, "fixed_revision": sha, "parents": parents,
                           "pr_or_issue_numbers": prs, "discovery_reasons": reasons,
                           "production_files": code_paths, "test_files": test_paths,
                           "review": item["review"],
                           "fix_patch": "fix.patch", "regression_patch": "regression.patch",
                           "full_patch": "full.patch",
                           "parent_selection": "first_parent" if len(parents) > 1 else "only_parent"})
            if code_paths:
                patch = git(repo, *diff_args, "--binary", "--", *code_paths)
                (location / "fix.patch").write_bytes(patch)
                patch_id = subprocess.check_output(["git", "patch-id", "--stable"], input=patch).decode().split()
                if patch_id:
                    record["production_patch_id"] = patch_id[0]
                    patch_groups.setdefault(patch_id[0], []).append(record_id)
            else:
                (location / "fix.patch").write_bytes(b"")
            regression = git(repo, *diff_args, "--binary", "--", *test_paths) if test_paths else b""
            (location / "regression.patch").write_bytes(regression)
            record["upstream_test_functions"] = sorted(set(re.findall(
                r"^\+\s*(?:async )?def (test_\w+)\(", regression.decode(errors="replace"), re.M
            )))
            (location / "full.patch").write_bytes(git(repo, *diff_args, "--binary"))
            dump(record_path, record)
            item["record"] = str(record_path.relative_to(output))
            counts["candidates"] += 1
            counts["candidates_with_production_changes"] += bool(code_paths)
            counts["candidates_with_test_changes"] += bool(test_paths)
        inventory.append(item)
        if (i + 1) % 250 == 0:
            print(f"Collected {i + 1}/{len(commits)} commits", flush=True)
    inventory_text = "".join(json.dumps(item, sort_keys=True) + "\n" for item in inventory)
    (private / "commit_inventory.jsonl").write_text(inventory_text)
    for note in release_notes:
        note["matching_commits"] = [item["sha"] for item in inventory
                                    if set(note["references"]) & set(item["pr_or_issue_numbers"])]
    dump(private / "release_note_candidates.json", release_notes)
    dump(private / "patch_groups.json", {k: v for k, v in patch_groups.items() if len(v) > 1})
    dump(private / "manifest.json", {
        "schema_version": 1, "repository": "https://github.com/pallets/click",
        "default_head": head,
        "refs": refs, "changelog_path": changelog_path,
        "inventory_sha256": hashlib.sha256(inventory_text.encode()).hexdigest(),
        "counts": dict(counts), "complete_bugfix_audit": counts["review_unreviewed"] == 0,
        "limits": ["Discovery signals are not proof that a commit fixes a bug.",
                   "All unreviewed commits need examination, including commits without discovery signals.",
                   "Matching production patch IDs identify possible duplicates, not identical bug semantics.",
                   "Commit records may contain multiple bugs; merge parent selection needs review.",
                   "Only fetched public branches and tags are included; unreachable/deleted commits are excluded."],
    })
    print(json.dumps(dict(counts), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--from-manifest", type=Path, help="Use recorded refs instead of current upstream refs")
    args = parser.parse_args()
    pinned = json.loads(args.from_manifest.read_text()) if args.from_manifest else None
    collect(args.repo, args.output, pinned)


if __name__ == "__main__":
    main()
