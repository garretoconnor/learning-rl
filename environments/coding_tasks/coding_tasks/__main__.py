"""Offline prompt inspection and grading, with no model or runtime setup."""
import argparse
import json
from pathlib import Path

from coding_tasks.cases import CASES
from coding_tasks.grading import grade
from coding_tasks.taskset import CodingTasksConfig, CodingTasksTaskset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Print task IDs and specifications")
    prompt = commands.add_parser("prompt", help="Print the exact model prompt")
    prompt.add_argument("task", choices=CASES)
    score = commands.add_parser("grade", help="Grade assertions in a UTF-8 file")
    score.add_argument("task", choices=CASES)
    score.add_argument("file", type=Path)
    commands.add_parser("demo", help="Compare reference, weak, and invalid tests offline")
    args = parser.parse_args()
    if args.command == "list":
        for key, case in CASES.items():
            print(f"{key}: {case.spec}")
    elif args.command == "prompt":
        tasks = CodingTasksTaskset(CodingTasksConfig()).load()
        print(next(t.data.prompt for t in tasks if t.data.case_id == args.task))
    elif args.command == "grade":
        print(json.dumps(grade(args.task, args.file.read_text()).to_dict(), indent=2))
    else:
        for key, case in CASES.items():
            print(json.dumps({"task": key, "submission": "reference", **grade(key, case.gold).to_dict()}))
        for label, source in [
            ("weak", "assert clamp(0, 0, 5) == 0"),
            ("partial", "assert clamp(9, 0, 5) == 5"),
            ("incorrect", "assert clamp(3, 0, 5) == 99"),
            ("invalid", "import os"),
        ]:
            print(json.dumps({"task": "clamp", "submission": label, **grade("clamp", source).to_dict()}))


if __name__ == "__main__":
    main()
