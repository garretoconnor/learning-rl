"""Mutation-testing taskset for Verifiers 0.3.1's v1 API."""
import verifiers.v1 as vf
from coding_tasks.cases import CASES
from coding_tasks.grading import grade


class CodingTasksData(vf.TaskData):
    case_id: str


class CodingTasksTask(vf.Task[CodingTasksData]):
    @vf.reward(weight=1.0)
    async def mutation_score(self, trace: vf.Trace) -> float:
        result = grade(self.data.case_id, trace.last_reply)
        trace.record_metrics({
            "valid_submission": float(result.valid),
            "correct_passed": float(result.correct_passed),
            "mutants_killed": float(result.mutants_killed),
            "mutants_total": float(result.mutants_total),
            "assertions": float(result.assertions),
        })
        trace.info["grading"] = result.to_dict()
        return result.score

    async def validate(self, runtime=None) -> bool:
        return grade(self.data.case_id, CASES[self.data.case_id].gold).score == 1.0


class CodingTasksConfig(vf.TasksetConfig):
    pass


class CodingTasksTaskset(vf.Taskset[CodingTasksTask, CodingTasksConfig]):
    def load(self) -> list[CodingTasksTask]:
        instructions = (
            "Write tests for the function below. Return only Python assertions, "
            "optionally inside one ```python code fence. Each statement must be "
            "assert FUNCTION(literal positional arguments) == literal expected value. "
            "Do not import or define the function. No variables, loops, helpers, "
            "keyword arguments, or other operators. Use 1–32 assertions. "
            "Allowed literals: integers in [-10000, 10000], strings up to 1000 characters, "
            "None, booleans, lists and tuples of up to 100 items. "
            "Tests must pass all correct implementations. Reward is the fraction of "
            "faulty implementations detected; any incorrect assertion gives zero.\n\n"
        )
        return [CodingTasksTask(CodingTasksData(
            idx=i, name=key, case_id=key, description=case.spec,
            prompt=instructions + case.spec,
        )) for i, (key, case) in enumerate(CASES.items())]
