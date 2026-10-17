"""Refuse harnesses that expose the controller host to model execution."""
from verifiers.v1.envs.single_agent.env import SingleAgentEnv, SingleAgentEnvConfig
import os
from pathlib import Path
import tempfile


class ClickPilotEnv(SingleAgentEnv):
    def __init__(self, config: SingleAgentEnvConfig):
        agent = config.agent
        if agent.runtime.type != "subprocess" or (agent.harness and agent.harness.id != "click-pilot"):
            raise ValueError("Click pilot requires its MCP-only click-pilot harness and local subprocess controller")
        if agent.harness and agent.harness.id == 'click-pilot':
            agent.harness.prime_required = config.taskset.task.host.backend == 'prime'
            agent.harness.prime_image = config.taskset.task.host.image
            agent.harness.execution_bundle = config.taskset.task.host.bundle
        super().__init__(config)

    async def start(self):
        self._owned_auth_file = None
        host = self.taskset.config.task.host
        if host.backend == 'prime' and host.auth_file is None:
            from prime_sandboxes import APIClient
            # File mode is 0600. This path enters trusted tool config only;
            # it is never mounted in the agent's execution container.
            fd, name = tempfile.mkstemp(prefix='click-pilot-controller-auth-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    stream.write(APIClient().api_key)
                host.auth_file = Path(name)
                self._owned_auth_file = host.auth_file
            except BaseException:
                Path(name).unlink(missing_ok=True)
                raise

    async def stop(self):
        path = getattr(self, '_owned_auth_file', None)
        if path:
            path.unlink(missing_ok=True)
            self.taskset.config.task.host.auth_file = None

    async def run(self, task, agents):
        traces = []
        try:
            await agents.agent.run(task, on_trace=traces.append)
        finally:
            if task.config.host.backend == 'prime':
                from .bridge import host_call
                for trace in traces:
                    if trace.state.run_id:
                        await host_call(task.config.host, 'cleanup', trace.state.run_id)
