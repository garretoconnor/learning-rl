#!/usr/bin/env python3
"""Compare fresh serial/batch verification on copies of a pinned real-record sample."""
import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import sys
import tempfile
import time

from batch_runner import ROOT, prepare, run_batch
from catalog_click import dump
from verify_record import verify

SAMPLE = [
    'click-33cd8696cb88bcdaa6d16d506bf468f275770ace',
    'click-33cd8696cb88bcdaa6d16d506bf468f275770ace--command-undefined-name',
    'click-33cd8696cb88bcdaa6d16d506bf468f275770ace--completion-callback-signature',
    'click-8ac004faaf2bc40ceab3688968fa2ab63bbb5ec1',
    'click-6131e96559e3a29aeb91129ac2a155af435a9433',
    'click-430c50a3b6dca4e23936389513d2099b0af865cf',
    'click-014ce7cc07ece8c5be1d7406377ee824184c66ec',
    'click-014ce7cc07ece8c5be1d7406377ee824184c66ec--pending-option-subcommands',
    'click-014ce7cc07ece8c5be1d7406377ee824184c66ec--group-argument-callback',
    'click-014ce7cc07ece8c5be1d7406377ee824184c66ec--original-callback-arguments',
    'click-014ce7cc07ece8c5be1d7406377ee824184c66ec--callback-keyword-documentation',
    'click-4e099e5a906fc2268ab868ff655d332ef72644c4',
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def comparable(evidence, folder):
    """Ignore paths, elapsed time, and order within unittest's set-difference display."""
    def text(value):
        value = re.sub(r'Ran (\d+) tests? in [0-9.]+s', r'Ran \1 test(s) in <TIME>',
                       value.replace(str(folder), '<RECORD>'))
        # Python hash randomization changes set item display order. Keep every
        # item and every other diagnostic byte; never suppress changed failures.
        return re.sub(r'(Items in the (?:first|second) set but not the (?:first|second):\n)((?:\x27[^\n]*\x27\n)+)',
                      lambda match: match[1] + ''.join(sorted(match[2].splitlines(keepends=True))), value)

    def normalized(run):
        return {key: text(value)
                if isinstance(value, str) else value for key, value in run.items()}
    return {'before': [normalized(r) for r in evidence['before']],
            'fixed': [normalized(r) for r in evidence['fixed']],
            'verified_fail_to_pass': evidence['verified_fail_to_pass'],
            'repetitions': evidence['repetitions'], 'runtime': evidence['runtime']}


def measure_packet(packet_path):
    packet = json.loads(packet_path.read_text())
    patches = {sha: (packet_path.parent / item['path']).read_text()
               for sha, item in packet['patches'].items()}
    texts = {
        'ungrouped': ''.join(patches[row['patch_sha256']] for row in packet['commits']),
        'grouped': ''.join(patches.values()),
        'ungrouped_review': '\n'.join(json.dumps(row, sort_keys=True) + '\n' + patches[row['patch_sha256']]
                                      for row in packet['commits']),
        'grouped_review': json.dumps(packet['commits'], sort_keys=True) + '\n' +
                          '\n'.join(sha + '\n' + patch for sha, patch in patches.items()),
    }
    metrics = {'scope': 'Complete patches; review variants also include all commit metadata and patch lookup IDs',
               'actual_model_tokens': None,
               **{key + '_bytes': len(value.encode()) for key, value in texts.items()}}
    try:
        # Vocabulary data is a local cache, separate from trusted test runtimes.
        os.environ.setdefault('TIKTOKEN_CACHE_DIR', str(ROOT / '.cache/tokenizers'))
        import tiktoken
        encoding = tiktoken.get_encoding('cl100k_base')
        metrics.update(tokenizer='cl100k_base', tiktoken_version=importlib.metadata.version('tiktoken'),
                       **{key + '_tokens': len(encoding.encode(value, disallowed_special=()))
                          for key, value in texts.items()})
    except (ImportError, OSError, ValueError) as exc:
        metrics['tokenizer_unavailable'] = type(exc).__name__
    return metrics


def benchmark(repo, root, python, output, trials=3, jobs=4):
    if trials < 1 or output.exists():
        raise ValueError('Use positive trials and a new output directory')
    from catalog_click import git
    git(repo, 'rev-parse', '--git-dir')
    output.mkdir(parents=True)
    python = python.absolute()
    source = root / 'private_grading/bugs'
    inputs = []
    original_hashes = {}
    for name in SAMPLE:
        folder = source / name
        record = json.loads((folder / 'record.json').read_text())
        assert record['status'] == 'validated', name
        inputs.append({'bug_id': name, 'before_revision': record['before_revision'],
                       'fixed_revision': record['fixed_revision'],
                       'record_sha256': digest(folder / 'record.json'),
                       'reproducer_sha256': digest(folder / record['reproducer']['path'])})
        for file in folder.rglob('*'):
            if file.is_file():
                original_hashes[file] = digest(file)
    dump(output / 'inputs.json', inputs)
    rounds = []
    with tempfile.TemporaryDirectory(prefix='click-benchmark-') as temp:
        temp = Path(temp)
        for trial in range(trials):
            # Alternate ordering to reduce host/file-cache order bias. Every run
            # starts with new batch input caches; no test results are reused.
            order = ['serial', 'batch'] if trial % 2 == 0 else ['batch', 'serial']
            outcomes = {}
            for mode in order:
                folders = []
                for name in SAMPLE:
                    folder = temp / name
                    if folder.exists():
                        shutil.rmtree(folder)
                    shutil.copytree(source / name, folder)
                    # This fixed sample was reviewed: each test uses only its
                    # snapshot, pure definitions, and local process state.
                    record_path = folder / 'record.json'
                    copied = json.loads(record_path.read_text())
                    copied['reproducer']['parallel_safe'] = True
                    dump(record_path, copied)
                    folders.append(folder)
                started = time.perf_counter()
                stdout = io.StringIO()
                if mode == 'serial':
                    with contextlib.redirect_stdout(stdout):
                        statuses = [verify(folder, repo, python) for folder in folders]
                    summary = {'seconds': time.perf_counter() - started,
                               'setup_counts': {'archive_exports': 2 * len(folders), 'runtime_probes': len(folders)},
                               'test_processes': 6 * len(folders), 'all_validated': all(statuses)}
                else:
                    report = run_batch(folders, repo, python, jobs)
                    dump(output / f'trial-{trial + 1}-batch.json', report)
                    summary = {'seconds': report['seconds'], 'setup_counts': report['setup_counts'],
                               'test_processes': report['test_processes'],
                               'all_validated': all(r['status'] == 'validated' for r in report['records'])}
                    stdout.write(json.dumps({'report': f'trial-{trial + 1}-batch.json',
                                             'validated': sum(r['status'] == 'validated' for r in report['records'])}) + '\n')
                summary['console_bytes'] = len(stdout.getvalue().encode())
                assert summary['all_validated'], f'{mode} failed validation; inspect logs'
                (output / f'trial-{trial + 1}-{mode}-console.txt').write_text(stdout.getvalue())
                outcomes[mode] = {}
                for folder in folders:
                    evidence = json.loads((folder / 'evidence/paired_runs.json').read_text())
                    dump(output / f'trial-{trial + 1}' / mode / folder.name / 'paired_runs.json', evidence)
                    outcomes[mode][folder.name] = comparable(evidence, folder)
                if mode == 'batch':
                    for item in report['records']:
                        name = Path(item['record']).name
                        item['source_record'] = str(source / name)
                        item['record'] = name
                        item['evidence'] = str(output / f'trial-{trial + 1}/batch' / name / 'paired_runs.json')
                    dump(output / f'trial-{trial + 1}-batch.json', report)
                rounds.append({'trial': trial + 1, 'mode': mode, **summary})
            if outcomes['serial'] != outcomes['batch']:
                raise AssertionError('Serial/batch evidence differs; inspect saved paired logs')
            print(json.dumps({'trial': trial + 1, 'evidence_matches': True}), flush=True)
    assert all(digest(path) == sha for path, sha in original_hashes.items()), 'Source catalog changed'
    commits = list(dict.fromkeys(item['fixed_revision'] for item in inputs))
    packet = prepare(repo, root, output / 'review-packet', len(commits), commits)
    # Equivalent patch inputs only. This measures exact duplicate removal, not
    # model reasoning tokens, billing, or full-history review throughput.
    token_metrics = measure_packet(output / 'review-packet/packet.json')
    serial = statistics.median(r['seconds'] for r in rounds if r['mode'] == 'serial')
    batch = statistics.median(r['seconds'] for r in rounds if r['mode'] == 'batch')
    result = {'schema_version': 1, 'sample_records': len(SAMPLE), 'trials': trials, 'jobs': jobs,
              'runs': rounds, 'median_serial_seconds': serial, 'median_batch_seconds': batch,
              'wall_time_reduction_percent': 100 * (1 - batch / serial), 'speedup': serial / batch,
              'evidence_matches_all_trials': True, 'source_catalog_unchanged': True,
              'test_result_reuse': False, 'patch_input_metrics': token_metrics,
              'parallel_safety_review': 'All 12 sample reproducers inspected; snapshot-local files and process-local state only. Permission set on benchmark copies, not source catalog records.',
              'limits': ['Small fixed sample of known validated records; not an end-to-end classification benchmark.',
                         'Serial baseline uses the same verifier, including its unified runtime probe.',
                         'OS filesystem caches are not flushed; alternating order reduces but does not remove host noise.',
                         'Token counts use an explicitly named local encoding; actual model usage is not measured.',
                         'Evidence comparison normalizes paths, unittest elapsed time, and set-difference item ordering only.']}
    dump(output / 'results.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--measure-packet', type=Path, help='Print local token/byte counts for an existing packet')
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    if args.measure_packet:
        print(json.dumps(measure_packet(args.measure_packet), indent=2))
    else:
        if args.repo is None or args.output is None:
            parser.error('--repo and --output are required for execution benchmarks')
        result = benchmark(args.repo, args.root, args.python, args.output, args.trials, args.jobs)
        print(json.dumps({key: result[key] for key in ('median_serial_seconds', 'median_batch_seconds',
                                                       'wall_time_reduction_percent', 'speedup')}))
