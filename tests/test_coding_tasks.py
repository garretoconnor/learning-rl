import asyncio
import itertools
import unittest

import verifiers.v1 as vf
from verifiers.v1.graph import MessageNode
from verifiers.v1.trace import AgentInfo, TraceTask
from verifiers.v1.configs.agent import AgentConfig
from verifiers.v1.types import AssistantMessage
from verifiers.v1.utils.loaders import load_taskset

from coding_tasks.cases import CASES
from coding_tasks.grading import grade
from coding_tasks.taskset import CodingTasksConfig, CodingTasksTaskset


class GradingTests(unittest.TestCase):
    def test_reference_suites_kill_all_mutants(self):
        for name, case in CASES.items():
            with self.subTest(name=name):
                result = grade(name, case.gold)
                self.assertEqual(result.score, 1.0)
                self.assertTrue(result.correct_passed)
                self.assertEqual(result.mutants_killed, 3)

    def test_partial_and_weak_submissions(self):
        self.assertEqual(grade("clamp", "assert clamp(9, 0, 5) == 5").score, 2 / 3)
        self.assertEqual(grade("clamp", "assert clamp(0, 0, 5) == 0").score, 0)

    def test_wrong_assertion_cancels_otherwise_good_suite(self):
        result = grade("clamp", CASES["clamp"].gold + "\nassert clamp(2, 0, 5) == 99")
        self.assertTrue(result.valid)
        self.assertFalse(result.correct_passed)
        self.assertEqual(result.score, 0)

    def test_empty_and_invalid_source(self):
        for source in ("", "# no assertions", "assert", "assert True", "assert clamp(0,0,1) != 1"):
            with self.subTest(source=source):
                self.assertFalse(grade("clamp", source).valid)

    def test_code_fence_and_comments(self):
        result = grade("clamp", "```python\n# boundary tests\n" + CASES["clamp"].gold + "\n```")
        self.assertEqual(result.score, 1)

    def test_executable_constructs_are_rejected(self):
        for source in (
            "import os", "while True: pass", "assert __import__('os').system('false') == 0",
            "assert clamp.__call__(0,0,1) == 0", "assert clamp(*(0,0,1)) == 0",
            "assert clamp(x=0,lo=0,hi=1) == 0", "assert clamp(0,0,1) == 0, print('oops')",
            "assert clamp(0,0,1) == 0\nraise Exception()",
            "assert clamp(1+1,0,3) == 2", "assert clamp(0,0,1) == [x for x in []]",
        ):
            with self.subTest(source=source):
                self.assertFalse(grade("clamp", source).valid)

    def test_domains_and_limits(self):
        for name, source in (
            ("clamp", "assert clamp(0,2,1) == 1"),
            ("clamp", "assert clamp(True,0,1) == 1"),
            ("clamp", "assert clamp(10001,0,1) == 1"),
            ("chunks", "assert chunks([1],0) == []"),
            ("insertion_index", "assert insertion_index([2,1],1) == 0"),
            ("unique", "assert unique([[]]) == [[]]"),
            ("unique", "assert unique(" + str(list(range(101))) + ") == []"),
            ("clamp", "assert clamp(0,0,1) == 0\n" * 33),
            ("clamp", "#" * 16001),
        ):
            with self.subTest(name=name, source=source[:80]):
                self.assertFalse(grade(name, source).valid)

    def test_duplicate_tests_do_not_inflate_score(self):
        source = "assert clamp(9,0,5) == 5\n"
        self.assertEqual(grade("clamp", source).score, grade("clamp", source * 20).score)

    def test_correct_implementations_agree_on_small_domains(self):
        # Independent cross-check of correct implementations beyond reference assertions.
        domains = {
            "clamp": [(x, lo, hi) for x in range(-3, 4) for lo in range(-2, 3)
                      for hi in range(lo, 3)],
            "unique": [(list(xs),) for n in range(5) for xs in itertools.product([-1,0,1], repeat=n)],
            "chunks": [(list(range(n)), size) for n in range(9) for size in range(1, 6)],
            "insertion_index": [(list(xs), target) for n in range(5)
                                for xs in itertools.combinations_with_replacement([-1,0,1], n)
                                for target in range(-2, 3)],
        }
        for name, args_list in domains.items():
            first, second = CASES[name].correct
            for args in args_list:
                self.assertEqual(first(*args), second(*args), (name, args))

    def test_verifiers_reward_hook_and_trace_metrics(self):
        for task in CodingTasksTaskset(CodingTasksConfig()).load():
            for source, expected in [(CASES[task.data.case_id].gold, 1), ("", 0)]:
                trace = vf.Trace(
                    task=TraceTask(type=type(task).__name__, data=task.data),
                    agent=AgentInfo(config=AgentConfig()),
                    nodes=[MessageNode(message=AssistantMessage(content=source), sampled=True)],
                )
                asyncio.run(task.score(trace))
                self.assertEqual(trace.reward, expected)
                self.assertEqual(trace.metrics["mutants_total"], 3)
                self.assertIn("grading", trace.info)

    def test_taskset_has_distinct_tasks_and_no_solutions_in_prompts(self):
        tasks = CodingTasksTaskset(CodingTasksConfig()).load()
        self.assertEqual(len(tasks), 4)
        self.assertEqual(len({t.key for t in tasks}), 4)
        for task in tasks:
            self.assertTrue(asyncio.run(task.validate()))
            self.assertNotIn(CASES[task.data.case_id].gold, task.data.prompt)

    def test_installed_plugin_loads_through_verifiers(self):
        taskset = load_taskset(CodingTasksConfig(id="coding-tasks"))
        self.assertEqual(len(list(taskset)), 4)


if __name__ == "__main__":
    unittest.main()
