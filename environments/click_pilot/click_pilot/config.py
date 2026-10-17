"""Controller configuration. These paths never enter task wire data."""
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
import verifiers.v1 as vf

ROOT = Path(__file__).resolve().parents[3]


class HostConfig(vf.ToolsetConfig):
    backend: Literal['macos', 'prime'] = 'macos'
    image: str | None = None
    region: str | None = None
    auth_file: Path | None = None
    root: Path = ROOT
    bundle: Path = ROOT / "code_testing_eval/private_grading/pilots/click-pilot-01"
    python: Path = Path("/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12")

    @model_validator(mode='after')
    def anchor_controller_paths(self):
        # MCP subprocesses have their own working directory. Send absolute
        # controller paths so setup and tools always use the same sealed bundle.
        self.root = self.root.resolve()
        if not self.bundle.is_absolute():
            self.bundle = self.root / self.bundle
        self.bundle = self.bundle.resolve()
        return self


class PilotConfig(vf.TaskConfig):
    host: HostConfig = HostConfig()
    agent_timeout_seconds: int = Field(default=900, ge=60, le=1800)


class PilotState(vf.State):
    run_id: str = ""
