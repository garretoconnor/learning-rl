#!/usr/bin/env python3
"""Build, export, explore, and grade the ten-bug Click pilot."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pilot.build import build, dump, seal
from pilot.environment import check_workspace, export, grade, load_bundle
from pilot.isolation import run, runtime
from pilot.verify import verify_pilot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('build')
    create.add_argument('--repo', type=Path, required=True)
    create.add_argument('--bundle', type=Path, required=True)
    new = commands.add_parser('new')
    new.add_argument('--bundle', type=Path, required=True)
    new.add_argument('--workspace', type=Path, required=True)
    shell = commands.add_parser('exec')
    shell.add_argument('--bundle', type=Path, required=True)
    shell.add_argument('--workspace', type=Path, required=True)
    shell.add_argument('command', nargs=argparse.REMAINDER)
    submit = commands.add_parser('grade')
    submit.add_argument('--bundle', type=Path, required=True)
    submit.add_argument('--submission', type=Path)
    submit.add_argument('--workspace', type=Path)
    submit.add_argument('--report', type=Path, required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--bundle', type=Path, required=True)
    verify.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'build':
        plan = build(args.repo.resolve(), ROOT, args.bundle.resolve())
        dump(args.bundle / 'runtime.lock', runtime())
        seal(args.bundle)
        print(json.dumps({'bundle': str(args.bundle), 'bugs': len(plan['bugs'])}))
    elif args.action == 'new':
        print(json.dumps(export(args.bundle.resolve(), args.workspace.resolve())))
    elif args.action == 'exec':
        command = args.command[1:] if args.command and args.command[0] == '--' else args.command
        if not command:
            parser.error('exec requires a command after --')
        check_workspace(load_bundle(args.bundle.resolve()), args.workspace)
        result = run(args.workspace, command, editable=True)
        print(result['stdout'], end='')
        print(result['stderr'], file=sys.stderr, end='')
        raise SystemExit(result['exit_code'] if not result['timed_out'] else 124)
    elif args.action == 'verify':
        result = verify_pilot(args.bundle.resolve(), args.output.resolve())
        print(json.dumps({'passed': result['passed'], 'bugs': result['bugs'], 'summary': str(args.output / 'summary.json')}))
    else:
        if (args.submission is None) == (args.workspace is None):
            parser.error('grade requires exactly one of --submission or --workspace')
        submissions = args.submission or args.workspace / 'submissions'
        result = grade(args.bundle.resolve(), submissions, args.report, workspace=args.workspace)
        print(json.dumps({key: result[key] for key in ('status', 'score', 'error') if key in result} | {'report': str(args.report)}))


if __name__ == '__main__':
    main()
