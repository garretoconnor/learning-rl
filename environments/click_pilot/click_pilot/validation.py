"""Offline framework integration checks using a deterministic local model stub.

The stub sends known tool calls. It does not measure model capability and does
not call an external inference endpoint. Episode traces contain private gold.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
from importlib.metadata import version
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import threading
import time

import verifiers.v1 as vf
from .config import HostConfig, PilotConfig
from .taskset import ClickPilotConfig


@asynccontextmanager
async def scripted_model(commands, *, allow_compaction=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != '/v1/responses':
                self.send_error(404, 'Only /v1/responses is supported')
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            # Retries of the same conversation receive the same next tool call.
            inputs = body.get('input', [])
            tail = json.dumps(inputs[-1] if isinstance(inputs, list) and inputs else inputs).lower()
            summary = bool(allow_compaction and
                           (not body.get('tools') or body.get('tool_choice') == 'none' or
                            'you are now acting as a compaction engine' in tail or
                            'context checkpoint compaction' in tail))
            if summary:
                body['_test_summary'] = True
            index = sum(m.get('type') == 'function_call_output' for m in body.get('input', []))
            if allow_compaction:
                index = sum(not r.get('_test_summary') for r in requests[:-1])
            issue = index < len(commands) and not summary
            if issue:
                names = [t['name'] for t in body['tools']]
                assert names in (['command'], ['mcp__workspace__command'], ['mcp__workspace']), names
                tool_name = 'command' if names == ['mcp__workspace'] else names[0]
            output = ([{'id': f'fc_{index}', 'type': 'function_call', 'status': 'completed',
                        'call_id': f'call_{index}', 'name': tool_name,
                        'arguments': json.dumps({'command': commands[index]})}]
                      if issue else
                      [{'id': f'msg_{index}', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': 'Submission ready.', 'annotations': []}]}])
            response = {'id': f'resp_{index}', 'object': 'response', 'created_at': int(time.time()),
                        'model': 'scripted-pilot-check', 'status': 'completed', 'output': output,
                        'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
            if issue and names == ['mcp__workspace']:
                response['output'][0]['namespace'] = 'mcp__workspace'
            if summary:
                response['output'][0]['content'][0]['text'] = ('Task: inspect the sandbox with command. '
                    'A large output was read. Continue with the remaining commands. Keep all files unchanged.')
            tokens = max(1, len(json.dumps(body))//4)
            output_tokens = max(1, len(json.dumps(response['output']))//4)
            response['usage'] = {'input_tokens': tokens, 'output_tokens': output_tokens,
                                 'total_tokens': tokens+output_tokens}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()


def write_command(files):
    source = "from pathlib import Path\n"
    for name, text in files.items():
        source += f"Path('submissions/{name}').write_text({text!r})\n"
    return "python3 -I -X utf8 -B -c " + shlex.quote(source)


async def run_case(config, commands, *, variant='minimal', compact_tokens=32000, allow_compaction=False, record_root=None):
    async with scripted_model(commands, allow_compaction=allow_compaction) as (url, requests):
        env_config = vf.resolve_env_config({
            "taskset": ClickPilotConfig(id="click-pilot", task=config),
            "agent": {"harness": {"id": "click-pilot", 'variant': variant, 'compact_tokens': compact_tokens,
                                  **({'record_root': record_root} if record_root is not None else {})}, "runtime": {"type": "subprocess"},
                      "max_turns": len(commands) + (10 if allow_compaction else 2), "retries": {"max_retries": 0}},
        })
        env = vf.load_environment(env_config)
        task = next(iter(env.taskset))
        ctx = vf.ModelContext(model="scripted-pilot-check", client=vf.EvalClientConfig(
            base_url=url, api_key_var="PILOT_UNUSED_LOCAL_KEY"))
        async with env.serving():
            episode = await env.run_episode(task, ctx)
        if not episode.ok or len(episode.traces) != 1 or not episode.traces[0].ok:
            raise AssertionError(episode.model_dump_json())
        trace = episode.traces[0]
        normal_requests = [r for r in requests if not r.get('_test_summary')]
        assert len(normal_requests) == len(commands) + 1, (len(normal_requests), len(commands))
        # Model sees tool results only, never the private reward report.
        last = requests[-1]
        tool_results = [m['output'] for m in last['input'] if m.get('type') == 'function_call_output']
        for content in tool_results:
            assert "controller; no unsandboxed fallback" not in str(content), content
            assert "MCP tool call requires approval" not in str(content), content
        return episode, tool_results


async def validate_reference(config):
    gold = {path.name: path.read_text() for path in (config.host.bundle / "gold").glob("test_*.py")}
    if len(gold) != 11:
        raise ValueError("Reference bank must contain ten tests and compatibility controls")
    episode, _ = await run_case(config, [write_command(gold), "python3 -I -X utf8 -B run_tests.py"])
    trace = episode.traces[0]
    return trace.reward == 1.0 and trace.metrics["confirmed_bugs"] == 10


async def audit(config, output):
    assert version("verifiers") == "0.3.1"
    bundle = config.host.bundle.resolve()
    output = output.resolve()
    if not output.is_relative_to(bundle) or output.exists():
        raise ValueError("Use a new audit directory inside the private bundle")
    output.mkdir(parents=True)
    gold = {p.name: p.read_text() for p in (bundle / "gold").glob("test_*.py")}
    passing = "import io, unittest, click\nclass TestEcho(unittest.TestCase):\n def test_echo(self):\n  out=io.StringIO(); click.echo('hello', file=out); self.assertEqual(out.getvalue(), 'hello\\n')\n"
    cases = {
        "reference_all": (gold, 1.0, "graded"),
        "passing_only": ({"test_echo.py": passing}, 0.0, "graded"),
        "duplicates": ({"test_one.py": gold["test_b07.py"], "test_two.py": gold["test_b07.py"]}, 0.1, "graded"),
        "multiple_bugs": ({"test_one.py": gold["test_b07.py"], "test_two.py": gold["test_b08.py"]}, 0.2, "graded"),
        "invalid_submission": ({"test_bad.py": "import click\ndef test_bad():return click.__file__\n"}, 0.0, "invalid_submission"),
        "unmatched_claim": ({"test_echo.py": passing.replace("'hello\\n')", "'other\\n')")}, 0.0, "needs_review"),
    }
    checks = {}
    run_ids = set()
    for name, (files, expected, status) in cases.items():
        episode, results = await run_case(config, [
            "test ! -d .git && test -z \"$(ls -A submissions)\" && head -n 1 TASK.md",
            write_command(files), "python3 -I -X utf8 -B run_tests.py",
        ])
        trace = episode.traces[0]
        assert trace.reward == expected, (name, trace.reward)
        assert trace.info["grading"]["status"] == status, (name, trace.info)
        assert json.loads(results[0])["exit_code"] == 0
        assert json.loads(results[1])["exit_code"] == 0
        assert trace.state.run_id not in run_ids
        run_ids.add(trace.state.run_id)
        (output / f"{name}.json").write_text(episode.model_dump_json(indent=2))
        checks[name] = {"reward": trace.reward, "status": status, "run_id": trace.state.run_id}
        print(f"{name}: reward={trace.reward} status={status}", flush=True)
    # Exercise the actual MCP path with a live listening model stub as a denied
    # network destination, and private files as denied read targets.
    async with scripted_model([]) as (url, _):
        from urllib.parse import urlparse
        port = urlparse(url).port
        target = ('1.1.1.1', 443) if config.host.backend == 'prime' else ('127.0.0.1', port)
        probe = f"""import errno,json,pathlib,socket
checks={{}}
for name,path in [('private_read',{str(bundle/'canary.txt')!r}),('private_listing',{str(bundle)!r}),('source_write','click/__init__.py')]:
 try:
  if name=='private_read':pathlib.Path(path).read_text()
  elif name=='private_listing':list(pathlib.Path(path).iterdir())
  else:pathlib.Path(path).write_text('tampered')
 except OSError as error:checks[name]=error.errno in (errno.EACCES,errno.EPERM,errno.ENOENT,errno.EROFS)
 else:checks[name]=False
try:
 s=socket.socket();s.settimeout(1);s.connect({target!r})
except OSError as error:checks['network']=error.errno in (errno.EACCES,errno.EPERM,errno.ENETUNREACH)
else:checks['network']=False
print(json.dumps(checks))
"""
        episode, results = await run_case(config, ["python3 -I -X utf8 -B -c " + shlex.quote(probe), write_command({"test_echo.py": passing})])
    isolation = json.loads(json.loads(results[0])["stdout"])
    assert all(isolation.values()), isolation
    (output / "isolation.json").write_text(episode.model_dump_json(indent=2))
    checks["isolation"] = isolation
    # Prompt/data must not carry private paths or records.
    data = next(iter(vf.load_taskset(ClickPilotConfig(id="click-pilot", task=config)))).data.model_dump_json()
    plan = json.loads((bundle / "plan.json").read_text())
    for secret in [str(bundle), plan["baseline_revision"], *(b["canonical_record"] for b in plan["bugs"])]:
        assert secret not in data
    checks["public_task_data"] = True
    summary = {"passed": True, "verifiers_version": version("verifiers"), "checks": checks,
               "execution_backend": config.host.backend,
               "scope": "Real Verifiers environment, rollout, MCP Responses loop, state channel, tools, and reward; deterministic local model stub, no capability measurement.",
               "adapter_sha256": hashlib.sha256(b"".join(str(p.relative_to(Path(__file__).parent)).encode() + p.read_bytes()
                    for p in sorted(Path(__file__).parent.rglob('*')) if p.is_file()
                    and (p.suffix in {".py", ".md"} or p.name == 'Dockerfile'))).hexdigest(),
               "plan_sha256": hashlib.sha256((bundle / "plan.json").read_bytes()).hexdigest()}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--backend', choices=['macos', 'prime'], default='macos')
    parser.add_argument('--root', type=Path, default=HostConfig().root)
    parser.add_argument('--bundle', type=Path, default=HostConfig().bundle)
    parser.add_argument('--image')
    args = parser.parse_args()
    asyncio.run(audit(PilotConfig(host=HostConfig(backend=args.backend, root=args.root.resolve(),
        bundle=args.bundle.resolve(), image=args.image)), args.output))


if __name__ == "__main__":
    main()
