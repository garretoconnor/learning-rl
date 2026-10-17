#!/usr/bin/env python3
"""Define a separate behavioral record from an already reviewed fix commit."""
import argparse
import json
import re
import shutil
from pathlib import Path

PROVENANCE = (
    'repository', 'source_commit', 'source_pr', 'title', 'author_date',
    'upstream_reported_description', 'before_revision', 'fixed_revision',
    'parents', 'parent_selection', 'pr_or_issue_numbers', 'discovery_reasons',
    'production_files', 'test_files', 'production_patch_id',
    'upstream_test_functions', 'fix_patch', 'regression_patch', 'full_patch',
)


def split(parent_dir, key, claim, reason):
    if not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', key):
        raise ValueError('Behavior key must contain lowercase words separated by hyphens')
    parent_dir = parent_dir.resolve()
    parent_path = parent_dir / 'record.json'
    parent = json.loads(parent_path.read_text())
    if parent['review']['classification'] != 'bugfix' or parent.get('parent_bug_id'):
        raise ValueError('Split requires a reviewed primary bugfix record')
    bug_id = parent['bug_id'] + '--' + key
    folder = parent_dir.parent / bug_id
    if folder.exists():
        raise ValueError('Behavior record already exists; edit it without resetting its evidence')
    record = json.loads((Path(__file__).resolve().parents[1] / 'templates/record.example.json').read_text())
    record.update({field: parent[field] for field in PROVENANCE if field in parent})
    record.update(bug_id=bug_id, parent_bug_id=parent['bug_id'], behavior_key=key,
                  behavioral_claim=claim, review={**parent['review'], 'reason': reason},
                  contract_evidence=[{'kind': 'reviewed_commit_diff', 'path': 'full.patch', 'note': reason}])
    folder.mkdir()
    for filename in ('fix.patch', 'regression.patch', 'full.patch'):
        shutil.copyfile(parent_dir / filename, folder / filename)
    (folder / 'record.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    parent.setdefault('related_records', []).append(bug_id)
    parent_path.write_text(json.dumps(parent, indent=2, sort_keys=True) + '\n')
    print(folder)
    return folder


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('parent', type=Path)
    parser.add_argument('--key', required=True)
    parser.add_argument('--claim', required=True)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    split(args.parent, args.key, args.claim, args.reason)
