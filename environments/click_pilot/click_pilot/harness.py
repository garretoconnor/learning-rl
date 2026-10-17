"""MCP-only harnesses; Prime tasks run every agent loop in a separate VM."""
import json
import sys
from pathlib import Path
from typing import Literal
from pydantic import Field
from verifiers.v1.harness import Harness
from verifiers.v1.configs.harness import HarnessConfig


class ClickPilotHarnessConfig(HarnessConfig):
    variant: Literal['minimal', 'codex', 'dsh'] = 'minimal'
    protocol: Literal['responses'] = 'responses'
    compact_tokens: int = Field(default=32000, ge=1024)
    record_root: Path = Path(__file__).resolve().parents[3] / 'code_testing_eval/private_grading/pilots/prime-harness-migration-01/events'
    codex_linux_archive: Path = Path(__file__).resolve().parents[3] / 'code_testing_eval/.cache/harnesses/codex-linux/codex.gz'
    dsh_linux_archive: Path = Path(__file__).resolve().parents[3] / 'code_testing_eval/.cache/harnesses/dsh-linux/dsh.tar.gz'
    node_linux_archive: Path = Path(__file__).resolve().parents[3] / 'code_testing_eval/.cache/harnesses/node-linux/node-v26.0.0-linux-x64.tar.xz'
    prime_required: bool = False
    prime_image: str | None = None
    execution_bundle: Path | None = None


class ClickPilotHarness(Harness[ClickPilotHarnessConfig]):
    APPENDS_SYSTEM_PROMPT = True
    SUPPORTS_MCP = True
    EXECUTES_CODE = False
    NEEDS_CONTAINER = False

    async def setup(self, runtime):
        # The uv workspace pins and installs all Responses-loop dependencies.
        pass

    async def launch(self, ctx, trace, runtime, endpoint, secret, mcp_urls, data):
        record_dir = self.config.record_root / trace.id
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / 'identity.json').write_text(json.dumps({
            'trace_id': trace.id, 'run_id': trace.state.run_id,
            'variant': self.config.variant, 'protocol': self.config.protocol,
            'harness_runtime': 'prime-vm' if self.config.prime_required else 'local',
        }, indent=2) + '\n')
        system, prompt = self.resolve_text_prompt(data)
        if self.config.prime_required or self.config.variant != 'minimal':
            if not self.config.prime_image or not self.config.execution_bundle:
                raise ValueError('A pinned Prime image and execution bundle are required; no local fallback')
            payload = {
                'variant': self.config.variant, 'endpoint': endpoint, 'secret': secret,
                'model': ctx.model, 'prompt': prompt, 'system': system, 'mcp_urls': mcp_urls,
                'compact_tokens': self.config.compact_tokens, 'tool_timeout': self.config.tool_timeout,
                'codex_linux_archive': str(self.config.codex_linux_archive),
                'dsh_linux_archive': str(self.config.dsh_linux_archive),
                'node_linux_archive': str(self.config.node_linux_archive),
                'image': self.config.prime_image, 'trace_id': trace.id,
                'run_dir': str(self.config.execution_bundle/'verifiers_runs'/trace.state.run_id),
                'record_dir': str(record_dir),
            }
            path = f'.vf-prime-harness-{trace.id}.json'
            await runtime.write(path, json.dumps(payload).encode())
            return await runtime.run_program([sys.executable, '-m', 'click_pilot.cli_harness', path], {})
        payload = {'endpoint': endpoint, 'secret': secret, 'model': ctx.model,
                   'system': system, 'prompt': prompt, 'mcp_urls': mcp_urls,
                   'tool_timeout': self.config.tool_timeout}
        path = f'.vf-responses-{trace.id}.json'
        await runtime.write(path, json.dumps(payload).encode())
        return await runtime.run_program([sys.executable, '-m', 'click_pilot.responses_program', path], {})
