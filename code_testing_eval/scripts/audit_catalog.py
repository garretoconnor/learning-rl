#!/usr/bin/env python3
"""Refresh review state and report what is still unproven in the Click catalog."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def audit(root):
    private = root / "private_grading"
    decisions = json.loads((private / "reviews/decisions.json").read_text())
    inventory_path = private / "commit_inventory.jsonl"
    inventory = [json.loads(line) for line in inventory_path.read_text().splitlines()]
    counts = Counter()
    missing = []
    seen_records = set()

    def count_record(path, review=None, parent=None):
        record = json.loads(path.read_text())
        if record['bug_id'] != path.parent.name:
            raise ValueError('Record ID differs from its directory')
        if record['bug_id'] in seen_records:
            raise ValueError('Record is linked more than once: ' + record['bug_id'])
        seen_records.add(record['bug_id'])
        if parent is not None:
            if record.get('parent_bug_id') != parent['bug_id']:
                raise ValueError('Behavior record has an incorrect parent')
            for field in ('source_commit', 'before_revision', 'fixed_revision'):
                if record[field] != parent[field]:
                    raise ValueError('Behavior record provenance differs: ' + field)
            counts['separate_behavior_records'] += 1
        if review is not None:
            record['review'] = review
            path.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
        counts['records'] += 1
        counts['record_' + record['status']] += 1
        if record['verification']['status'] == 'blocked_by_other_bug':
            counts['records_blocked_by_other_bug'] += 1
        counts['records_without_behavioral_claim'] += not bool(record['behavioral_claim'])
        counts['records_without_extracted_reproducer'] += not bool(record['reproducer']['path'])
        counts['additional_claims_pending'] += len(record.get('additional_claims_pending', []))
        for bug_id in record.get('related_records', []):
            if Path(bug_id).name != bug_id:
                raise ValueError('Invalid related record ID')
            count_record(private / 'bugs' / bug_id / 'record.json', parent=record)

    for item in inventory:
        if item["sha"] in decisions:
            item["review"] = decisions[item["sha"]]
        classification = item["review"]["classification"]
        counts["commits"] += 1
        counts["review_" + classification] += 1
        counts['reviews_incomplete'] += item['review'].get('complete') is False
        if item["record"]:
            path = root / item["record"]
            count_record(path, item['review'])
        elif classification == "bugfix":
            missing.append(item["sha"])
    serialized = "".join(json.dumps(item, sort_keys=True) + "\n" for item in inventory)
    orphan_records = sorted(path.parent.name for path in (private / 'bugs').glob('*/record.json')
                            if path.parent.name not in seen_records)
    inventory_path.write_text(serialized)
    manifest_path = private / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["inventory_sha256"] = hashlib.sha256(serialized.encode()).hexdigest()
    manifest["audit_counts"] = dict(counts)
    manifest["complete_bugfix_audit"] = (
        counts["review_unreviewed"] == 0 and counts["review_deferred"] == 0
        and counts['reviews_incomplete'] == 0
        and not missing and counts["additional_claims_pending"] == 0
    )
    # Separate full history classification from defining and validating individual bug records.
    manifest["catalog_complete"] = False
    manifest["completion_gates"] = {
        "all_commits_reviewed": (counts["review_unreviewed"] == 0 and counts["review_deferred"] == 0
                                 and counts['reviews_incomplete'] == 0),
        "all_confirmed_fix_commits_have_records": not missing,
        "multi_bug_commits_split": counts["additional_claims_pending"] == 0,
        "all_record_directories_linked": not orphan_records,
        "all_release_note_fix_references_resolved": False,
        "all_bug_records_defined": False,
        "duplicate_commits_resolved": False,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    report = {
        "counts": dict(counts), "confirmed_bugfixes_without_records": missing,
        "orphan_records": orphan_records,
        "catalog_complete": False,
        "next_actions": ["Finish incomplete commit reviews and review deferred classifications.",
                         "Review every unreviewed commit, including unmatched commits.",
                         "Define claims and reproducers for each confirmed bug fix.",
                         "Resolve every release-note fix reference and duplicate group.",
                         "Split commits with multiple behavioral bugs into separate records.",
                         "Validate runtimes and build the bug–snapshot map."],
    }
    (root / "STATUS.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    audit(parser.parse_args().root)
