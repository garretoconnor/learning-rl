# Click pilot for Verifiers

This code-only checkout excludes `code_testing_eval/private_grading/`, including
the bug catalog, source snapshots, grading bundles, and run results. Click pilot
execution requires a separately prepared bundle. Dataset-dependent checks skip
when the bundle is absent; the introductory coding tasks need no external data.

This package implements the Click test-writing pilot in Prime Intellect
Verifiers **0.3.1**, using its `verifiers.v1` API. It keeps the current ten-bug
bank and its differential grading rules.

The [Prime migration guide](../../code_testing_eval/PRIME_MIGRATION.md) describes
the cloud backend and deployment commands. The private image, Linux audit,
Verifiers cloud audit, and full audit with the controller on Prime all passed.
The native macOS path remains available.

From the repository root:

```bash
uv sync --frozen
uv run --frozen eval @ configs/click-pilot-local.toml --dry-run --no-push
uv run --frozen validate click-pilot --runtime.type subprocess --only-gold \
  --max-concurrent 1 --no-rich \
  --output-dir code_testing_eval/private_grading/pilots/click-pilot-01/evidence/cli-validation
```

Gold validation runs a deterministic local model stub through the actual
Verifiers environment, agent loop, MCP server, state channel, command tool,
and reward hook. It needs no external model or API key. It is an integration
test, not a blind discovery trial.

To check other scoring cases and isolation, use a new output directory:

```bash
uv run --frozen python -m click_pilot.validation \
  --output code_testing_eval/private_grading/pilots/click-pilot-01/evidence/new-verifiers-audit
```

To run a model, set its ID and endpoint in
`configs/click-pilot-local.toml`, start the server, then run:

```bash
uv run --frozen eval @ configs/click-pilot-local.toml --no-push
```

The configuration caps the attempt at 40 model turns and disables uploads.
It uses a controller with an MCP-only harness. Responses is the only supported API.
Chat Completions configurations are rejected; there is no fallback. Model-directed
execution goes through the `command` tool on the configured execution backend.
Do not switch to an unrestricted shell harness. The task and environment reject
that configuration.

`ClickPilotTaskset` emits one public task, with no hidden bug data.
`ClickPilotTask.setup` creates a separate workspace for each rollout.
`WorkspaceTools` uses Verifiers' typed state channel to select that workspace;
the model cannot choose a run ID or host path as a tool argument.
The `bug_discovery` reward calls the private grader and records a score from
0 to 1: distinct confirmed bugs divided by ten. Duplicate tests add no credit.
Unmatched behavioral findings go to private review. The final model text is
not the submission.

The private grader runs under the Python executable in the existing runtime
lock. The framework itself runs under uv. Controller settings are under
`env.taskset.task.host` (`root`, `bundle`, and `python`). Defaults point to this
checkout and the current macOS Python installation. Tool placement must stay
local and separate from the harness. Moving runtimes or bundles requires the
same explicit pin checks and revalidation as the native pilot.

The local config requires the native macOS sandbox. The Prime config uses
Prime VMs and restricted Linux containers. Neither backend has an unrestricted
execution fallback.

## Responses harness comparison

The three `configs/click-pilot-{minimal,codex,dsh}-responses.toml` files use
the same historical Click task, Prime execution image, OpenRouter model,
40-call limit, and 30-minute agent limit. Load `OPENROUTER_API_KEY` from the
ignored root `.env` before running one of these configurations. Keep uploads
disabled. Each configuration starts a fresh attempt.

`minimal` replays the Responses conversation without compaction. `codex` uses
Codex CLI 0.160.0 and `dsh` uses DeepSeek Harness 0.2.1-rc.2. Both CLI variants
use native semantic compaction at a configured 32,000-token trigger. Summary
requests count toward the same inference-call limit and cost totals.

These are restricted CLI integrations. Only the shared sandbox `command`
tool is advertised and accepted. Native host shell and file tools are disabled.
The gateway buffers Responses output and re-emits streaming events for CLI
compatibility. This can change latency.

For Prime tasks, **all three agent loops run in dedicated Prime Linux VMs**.
They use one launcher, the same image and VM resources, and the same Responses
and MCP file relay. Each loop runs as UID 65534. Prime's deny-all egress policy
must be applied before provisioning starts. Startup also verifies the actual
child's identity and lack of provider/cloud keys before forwarding any model
request. The bridge listens on 127.0.0.1; no additional network namespace or raw
TCP-connect probe is needed. A controlled HTTP/TLS diagnosis confirmed blocking
for the tested traffic; a successful TCP connect alone did not show a policy failure.

The trusted controller retains credentials and relays messages through the
Prime file API. The harness VMs receive no provider key, cloud key, repository,
or private grading bundle. All model-directed commands and tests still execute
in separate Prime task containers. VM journals support cleanup after interruption.
The Verifiers `subprocess` runtime starts only the trusted provisioner and relay;
the minimal Python loop, Codex CLI, and dsh process execute in their Linux VMs.
The legacy native macOS minimal path remains available for local-only tasks.
Prime tasks require Responses and refuse a local fallback.

Linux assets are pinned in `code_testing_eval/.cache/harnesses/`: the Codex
binary archive, dsh's Linux npm installation/archive, and Node v26.0.0. Minimal
uses pinned Python packages already in the common image. Asset staging time is
recorded separately from the running harness; asset sizes differ.
The `prime-harness-migration-01` evidence directory records their versions,
hashes, package lock, configurations, and smoke checks. The first comparison
report records the earlier mixed runtime layout; its results were not rerun by
this migration. Full traces and harness event files must stay private.

Private evidence and workspaces stay under
`code_testing_eval/private_grading/pilots/click-pilot-01/verifiers_runs/`.
Framework evaluation logs stay under the configured output directory. Keep
these controller files out of agent mounts and public training prompts.
The reference audit traces include the reference test code and must stay private.

See [the full pilot guide](../../code_testing_eval/PILOT.md) for the submission
contract, exact reward rules, native isolation limits, and private bundle checks.
