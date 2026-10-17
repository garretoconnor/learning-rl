"""Only model-directed execution surface; every command enters the native sandbox."""
import asyncio
import logging
import verifiers.v1 as vf
from .bridge import host_call
from .config import HostConfig, PilotState


class WorkspaceTools(vf.Toolset[HostConfig, PilotState]):
    TOOL_PREFIX = None

    def __init__(self, config):
        if config.colocated or config.url or config.runtime.type != "subprocess":
            raise ValueError("Click tools must run in their own trusted local controller")
        super().__init__(config)
        self._lock = asyncio.Lock()

    @vf.tool
    async def command(self, command: str) -> dict:
        """Run a shell command in the offline Click workspace (15 second limit).

        Read source and docs, write submissions/test_*.py, and run tests with
        python3 -I -X utf8 -B run_tests.py. Only submissions/ and scratch/ are writable.
        Each call starts in the workspace root; shell state does not persist.
        """
        async with self._lock:
            try:
                return await host_call(self.config, "exec", self.state.run_id, command=command)
            except Exception:
                logging.getLogger(__name__).exception("Pilot workspace command failed")
                return {"error": "Workspace command failed in the controller; no unsandboxed fallback was used."}


if __name__ == "__main__":
    WorkspaceTools.run()
