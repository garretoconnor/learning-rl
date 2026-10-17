#!/usr/bin/env python3
"""Prepare complete review packets and execute trusted catalog reproducers in batches."""
import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from catalog_click import dump, git
from verify_record import run_snapshot, runtime_info, verify

ROOT = Path(__file__).resolve().parents[1]


class SharedInputs:
    """Single-flight immutable inputs, scoped to one invocation. Never cache test results."""
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}
        self.counts = {'archive_exports': 0, 'archive_reuses': 0,
                       'runtime_probes': 0, 'runtime_reuses': 0}

    def get(self, key, create, category):
        with self.lock:
            owner = key not in self.entries
            if owner:
                self.entries[key] = Future()
                self.counts[category] += 1
            else:
                self.counts['archive_reuses' if category == 'archive_exports' else 'runtime_reuses'] += 1
            future = self.entries[key]
        if owner:
            try:
                future.set_result(create())
            except Exception as exc:
                future.set_exception(exc)
        return future.result()

    def runtime(self, python, dependencies):
        if not isinstance(dependencies, list) or any(not isinstance(n, str) for n in dependencies):
            raise ValueError('Dependencies must be distribution names')
        return self.get(('runtime', str(python), tuple(dependencies)),
                        lambda: runtime_info(python, dependencies), 'runtime_probes')

    def snapshot(self, repo, revision, reproducer, python, repetitions, source_mode):
        # Only full, pinned commit IDs enter the archive cache.
        archive = self.get(('archive', str(repo), revision),
                           lambda: git(repo, 'archive', revision), 'archive_exports')
        # Each call extracts a fresh directory. Reproducers can modify it without
        # affecting another record. Three repeats use the legacy verifier's semantics.
        return run_snapshot(repo, revision, reproducer, python, repetitions, source_mode, archive=archive)


def prepare(repo, root, output, limit, commits=None):
    if limit < 1:
        raise ValueError('Limit must be positive')
    if output.exists():
        raise ValueError('Use a new output directory; existing review packets are immutable')
    private = root / 'private_grading'
    decisions = json.loads((private / 'reviews/decisions.json').read_text())
    items = [json.loads(line) for line in (private / 'commit_inventory.jsonl').read_text().splitlines()]
    if commits is not None:
        index = {item['sha']: item for item in items}
        selected = [index[sha] for sha in dict.fromkeys(commits)]
    else:
        selected = [item for item in reversed(items)
                    if decisions.get(item['sha'], item.get('review', {})).get('classification', 'unreviewed') == 'unreviewed'][:limit]
    rows, groups = [], {}
    total_bytes = 0
    for item in selected:
        sha = item['sha']
        parent = item['parents'][0] if item['parents'] else None
        patch = git(repo, 'diff', parent, sha, '--binary') if parent else git(repo, 'show', '--format=', sha, '--binary')
        digest = hashlib.sha256(patch).hexdigest()
        total_bytes += len(patch)
        if digest not in groups:
            target = output / 'patches' / (digest + '.patch')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(patch)
            groups[digest] = {'path': str(target.relative_to(output)), 'bytes': len(patch), 'commits': []}
        groups[digest]['commits'].append(sha)
        rows.append({key: item[key] for key in ('sha', 'parents', 'title', 'body', 'changed_files', 'record')})
        rows[-1]['patch_sha256'] = digest
    result = {'schema_version': 1, 'commits': rows, 'patches': groups,
              'metrics': {'commits': len(rows), 'unique_patches': len(groups),
                          'ungrouped_patch_bytes': total_bytes,
                          'grouped_patch_bytes': sum(g['bytes'] for g in groups.values())},
              'instruction': 'Review every commit. Identical patches are shared reading inputs, not proof of identical bug semantics. No decisions have been changed.'}
    dump(output / 'packet.json', result)
    return result


def run_batch(records, repo, python, jobs=4):
    if jobs < 1:
        raise ValueError('Jobs must be positive')
    repo, python = repo.resolve(), python.absolute()
    paths = [Path(p).resolve() for p in records]
    if len(set(paths)) != len(paths):
        raise ValueError('A record must not be scheduled twice')
    shared = SharedInputs()
    started = time.perf_counter()

    def execute(folder):
        begin = time.perf_counter()
        path = folder / 'record.json'
        original = None
        try:
            original = json.loads(path.read_text())
            if original.get('review', {}).get('classification') != 'bugfix':
                return {'record': str(folder), 'status': 'skipped', 'reason': 'Requires a reviewed bugfix record'}
            if not original.get('behavioral_claim') or not original.get('reproducer', {}).get('path'):
                return {'record': str(folder), 'status': 'skipped', 'reason': 'Requires claim and trusted extracted reproducer'}
            # Resolve symbolic revisions once, before execution, and keep original
            # record references unchanged. Hash-keyed archive inputs cannot drift.
            pinned = {rev: git(repo, 'rev-parse', rev + '^{commit}').decode().strip()
                      for rev in (original['before_revision'], original['fixed_revision'])}

            def snapshot(r, revision, reproducer, py, repetitions, source_mode):
                return shared.snapshot(r, pinned[revision], reproducer, py, repetitions, source_mode)

            passed = verify(folder, repo, python, repetitions=3,
                            runtime_loader=shared.runtime, snapshot_runner=snapshot, emit=False)
            return {'record': str(folder), 'bug_id': original['bug_id'],
                    'status': 'validated' if passed else 'inspect_evidence',
                    'seconds': time.perf_counter() - begin,
                    'pinned_revisions': pinned, 'test_processes': 6,
                    'evidence': str(folder / 'evidence/paired_runs.json')}
        except Exception as exc:
            # A setup failure must not leave stale success eligible for grading.
            if original is not None:
                original['status'] = 'candidate'
                original['snapshot_results'] = []
                original['verification'] = {'status': 'blocked_setup', 'error_type': type(exc).__name__,
                                            'error': str(exc)}
                dump(path, original)
            return {'record': str(folder), 'status': 'blocked_setup',
                    'seconds': time.perf_counter() - begin,
                    'error_type': type(exc).__name__, 'error': str(exc)}

    # A private working directory does not isolate hard-coded external files,
    # ports, or tools. Only explicitly reviewed reproducers can overlap.
    parallel, serial = [], []
    for path in paths:
        try:
            safe = json.loads((path / 'record.json').read_text()).get('reproducer', {}).get('parallel_safe') is True
        except (OSError, ValueError):
            safe = False
        (parallel if safe else serial).append(path)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        parallel_results = list(pool.map(execute, parallel))
    serial_results = [execute(path) for path in serial]
    by_path = {r['record']: r for r in parallel_results + serial_results}
    results = [by_path[str(path)] for path in paths]
    return {'schema_version': 1, 'seconds': time.perf_counter() - started,
            'jobs': jobs, 'records': results, 'setup_counts': shared.counts,
            'test_processes': sum(r.get('test_processes', 0) for r in results),
            'test_result_reuse': False, 'repetitions_per_snapshot': 3,
            'parallel_safe_records': len(parallel), 'serial_records': len(serial)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    packet = commands.add_parser('prepare', help='Prepare complete patches for the next unreviewed commits')
    packet.add_argument('--repo', type=Path, required=True)
    packet.add_argument('--root', type=Path, default=ROOT)
    packet.add_argument('--output', type=Path, required=True)
    packet.add_argument('--limit', type=int, default=25)
    packet.add_argument('--commits', nargs='+', help='Explicit full commit IDs, including reviewed commits')
    run = commands.add_parser('run', help='Run trusted extracted records; full evidence stays in each record')
    run.add_argument('records', nargs='+', type=Path)
    run.add_argument('--repo', type=Path, required=True)
    run.add_argument('--python', type=Path, default=Path(sys.executable))
    run.add_argument('--jobs', type=int, default=4)
    run.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        result = prepare(args.repo, args.root, args.output, args.limit, args.commits)
        print(json.dumps({'packet': str(args.output / 'packet.json'), **result['metrics']}))
    else:
        result = run_batch(args.records, args.repo, args.python, args.jobs)
        dump(args.report, result)
        counts = {s: sum(r['status'] == s for r in result['records'])
                  for s in ('validated', 'inspect_evidence', 'blocked_setup', 'skipped')}
        print(json.dumps({'report': str(args.report), 'seconds': result['seconds'], **counts}))
        if counts['inspect_evidence'] or counts['blocked_setup'] or counts['skipped']:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
