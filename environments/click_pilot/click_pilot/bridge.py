"""Async controller-to-pinned-runtime bridge, never an agent tool itself."""
import asyncio
import json
import os
from pathlib import Path
import signal

from .config import HostConfig


async def host_call(config: HostConfig, action: str, run_id: str, **kwargs):
    if config.backend == 'prime':
        from .cloud.host import main
        operation = asyncio.create_task(asyncio.to_thread(main, config, action, run_id, **kwargs))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # A canceled thread keeps running. Wait for it so cleanup cannot
            # race an in-flight create whose sandbox id is not recorded yet.
            try:
                await operation
            finally:
                if action == 'new':
                    await asyncio.to_thread(main, config, 'cleanup', run_id)
            raise
    process = await asyncio.create_subprocess_exec(
        str(config.python), "-I", str(Path(__file__).with_name("host.py")),
        json.dumps({"root": str(config.root.resolve()), "bundle": str(config.bundle.resolve()),
                    "action": action, "run_id": run_id, **kwargs}),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}, start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 600 if action == "grade" else 30)
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode:
        # Host diagnostics belong to controller logs, not model-visible tool output.
        raise RuntimeError(f"Pilot host {action} failed: {stderr.decode(errors='replace')[-2000:]}")
    return json.loads(stdout)
