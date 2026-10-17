"""One repository task; ten private behaviors contribute to its reward."""
import uuid
from pathlib import Path
import verifiers.v1 as vf
from .bridge import host_call
from .config import PilotConfig, PilotState
from .tools import WorkspaceTools


class PilotData(vf.TaskData):
    pass


class ClickPilotTask(vf.Task[PilotData, PilotState, PilotConfig]):
    async def setup(self, trace, runtime):
        agent = trace.agent
        validating = (agent is not None and agent.name == "validate" and not agent.trainable
                      and agent.config.harness is None)
        if (agent is None or agent.config.runtime.type != "subprocess"
                or (not validating and (agent.config.harness is None or agent.config.harness.id != "click-pilot"))):
            raise ValueError("Use the MCP-only click-pilot harness with the local subprocess controller")
        trace.state.run_id = uuid.uuid4().hex
        if validating:
            self._validation_run_id = trace.state.run_id
        await host_call(self.config.host, "new", trace.state.run_id)

    @classmethod
    def toolsets(cls, config):
        return [WorkspaceTools(config.host)]

    async def finalize(self, trace, runtime):
        if not (await host_call(self.config.host, "check", trace.state.run_id))["exists"]:
            raise RuntimeError("Click workspace disappeared before scoring")

    @vf.reward(weight=1.0)
    async def bug_discovery(self, trace):
        result = await host_call(self.config.host, "grade", trace.state.run_id)
        trace.record_metrics({"confirmed_bugs": float(result["matched"]),
                              "pending_review": float(result["needs_review"]),
                              "valid_submission": float(result["status"] in {"graded", "needs_review"})})
        # Attribution and full execution logs stay in the private host report.
        trace.info["grading"] = {"status": result["status"]}
        return result["score"]

    async def validate(self, runtime=None):
        # CLI gold validation exercises this same lifecycle and reward hook.
        from .validation import validate_reference
        try:
            return await validate_reference(self.config)
        finally:
            if self.config.host.backend == 'prime' and getattr(self, '_validation_run_id', None):
                await host_call(self.config.host, 'cleanup', self._validation_run_id)


class ClickPilotConfig(vf.TasksetConfig):
    task: PilotConfig = PilotConfig()


class ClickPilotTaskset(vf.Taskset[ClickPilotTask, ClickPilotConfig]):
    def load(self):
        prompt = Path(__file__).with_name("TASK.md").read_text()
        yield ClickPilotTask(PilotData(idx=0, name="click-regression-tests", description="Find Click bugs and write regression tests.",
                                      prompt=prompt + "\nUse the command tool to inspect files, write tests, and run them. Your final reply ends the task; files in submissions/ are graded.",
                                      timeout=vf.TaskTimeout(setup=900 if self.config.task.host.backend == 'prime' else 120,
                                                             agent=self.config.task.agent_timeout_seconds, finalize=30,
                                                             scoring=1800 if self.config.task.host.backend == 'prime' else 600)), self.config.task)
