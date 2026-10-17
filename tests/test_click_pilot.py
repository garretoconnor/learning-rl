"""Framework wiring and controller boundary checks (no native process required)."""
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import verifiers.v1 as vf
from click_pilot.config import HostConfig, PilotConfig, PilotState
from click_pilot.host import main as host_main
from click_pilot.taskset import ClickPilotConfig, ClickPilotTaskset
from click_pilot.tools import WorkspaceTools


class FrameworkTests(unittest.TestCase):
    def test_task_time_budget_changes_without_changing_prompt(self):
        tasks = [next(iter(ClickPilotTaskset(ClickPilotConfig(
            task=PilotConfig(agent_timeout_seconds=seconds))))) for seconds in (900, 1800)]
        self.assertEqual(tasks[0].data.prompt, tasks[1].data.prompt)
        self.assertEqual([task.data.timeout.agent for task in tasks], [900, 1800])

    def test_loader_resolves_task_environment_and_default_safe_harness(self):
        config = vf.resolve_env_config({"taskset": {"id": "click-pilot"},
                                        "agent": {"runtime": {"type": "subprocess"}}})
        env = vf.load_environment(config)
        self.assertEqual(type(env).__name__, "ClickPilotEnv")
        self.assertEqual(type(env._harnesses["agent"]).__name__, "ClickPilotHarness")
        task, = list(env.taskset)
        self.assertEqual([fn.__name__ for fn in task.hooks("reward")], ["bug_discovery"])
        self.assertEqual([type(t).__name__ for t in task.toolsets(task.config)], ["WorkspaceTools"])

    @unittest.skipUnless((HostConfig().bundle / 'plan.json').is_file(), 'Requires local grading data excluded from the code-only checkout')
    def test_private_data_is_absent_from_task_wire(self):
        task, = list(ClickPilotTaskset(ClickPilotConfig()))
        data = task.data.model_dump_json()
        bundle = task.config.host.bundle
        plan = json.loads((bundle / "plan.json").read_text())
        for secret in [str(bundle), plan["baseline_revision"], *(b["canonical_record"] for b in plan["bugs"])]:
            self.assertNotIn(secret, data)
        self.assertNotIn("run_id", data)
        self.assertNotIn("gold", data)

    def test_packaged_prompt_matches_exported_workspace_prompt(self):
        config = HostConfig()
        tree = ast.parse((config.root / "code_testing_eval/pilot/environment.py").read_text())
        original = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "PROMPT" for t in n.targets))
        import click_pilot.taskset
        self.assertEqual(Path(click_pilot.taskset.__file__).with_name("TASK.md").read_text(), original)

    def test_unsafe_harness_and_tool_placement_rejected(self):
        with self.assertRaisesRegex(ValueError, "MCP-only"):
            vf.load_environment(vf.resolve_env_config({"taskset": {"id": "click-pilot"},
                "agent": {"harness": {"id": "bash"}, "runtime": {"type": "subprocess"}}}))
        for overrides in ({"colocated": True}, {"url": "http://localhost:1234"}):
            with self.assertRaisesRegex(ValueError, "trusted local controller"):
                WorkspaceTools(HostConfig(**overrides))

    def test_invalid_rollout_identity_cannot_select_host_path(self):
        for run_id in ("", "../other", "/tmp/other", "a" * 31, "a" * 33):
            with self.assertRaisesRegex(ValueError, "rollout identity"):
                host_main({"run_id": run_id})


class AsyncFrameworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_agent_bypass_is_rejected_before_workspace_creation(self):
        task, = list(ClickPilotTaskset(ClickPilotConfig()))
        trace = vf.Trace(task=vf.TraceTask(type=type(task).__name__, data=task.data), state=PilotState(),
                         agent=vf.AgentInfo(config=vf.AgentConfig(runtime=vf.SubprocessConfig())))
        with patch("click_pilot.taskset.host_call", new_callable=AsyncMock) as host:
            with self.assertRaisesRegex(ValueError, "MCP-only"):
                await task.setup(trace, None)
            host.assert_not_called()

    async def test_reward_dispatch_records_score_without_private_attribution(self):
        task, = list(ClickPilotTaskset(ClickPilotConfig()))
        trace = vf.Trace(task=vf.TraceTask(type=type(task).__name__, data=task.data), state=PilotState(run_id="a" * 32),
                         agent=vf.AgentInfo(config=vf.AgentConfig(runtime=vf.SubprocessConfig())))
        with patch("click_pilot.taskset.host_call", new_callable=AsyncMock,
                   return_value={"status": "needs_review", "score": 0.2, "matched": 2, "needs_review": 1}):
            await task.score(trace)
        self.assertEqual(trace.reward, 0.2)
        self.assertEqual(trace.metrics["confirmed_bugs"], 2)
        self.assertEqual(trace.metrics["pending_review"], 1)
        self.assertEqual(trace.info["grading"], {"status": "needs_review"})

    async def test_tool_errors_do_not_expose_controller_diagnostics(self):
        tools = WorkspaceTools(HostConfig())
        tools._inert_state.run_id = "a" * 32
        with patch("click_pilot.tools.host_call", new_callable=AsyncMock,
                   side_effect=RuntimeError("private report and path")), self.assertLogs("click_pilot.tools", level="ERROR"):
            result = await tools.command("ls")
        self.assertNotIn("private report", json.dumps(result))
        self.assertIn("no unsandboxed fallback", result["error"])


if __name__ == "__main__":
    unittest.main()
