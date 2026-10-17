# Prime migration

All five migration steps passed on 2026-10-17. The Linux scoring audit, the
Verifiers audit through Prime VMs, and the audit with the controller on Prime
passed. These runs used a scripted model; they did not measure model capability.

## Current state

| Step | State |
|---|---|
| Build the Linux runtime image | Complete: private image built; observed Linux runtime pinned |
| Replace the macOS execution backend | Complete: Prime VM execution and isolation audits pass |
| Preserve the grading rules | Complete: same grader; native and Linux scoring audits pass |
| Repeat the audit on Linux | Complete: all ten bugs and controlled scores pass |
| Move the controller to Prime | Complete: full Verifiers audit passed on a dedicated Prime controller VM |

The verified private image is
`prime/cmgmzj3qf001e18jip6eovbnl/click-pilot-runtime:95047fb269b0f433`.
The cloud config in `configs/click-pilot-prime.toml` uses this image.

Saved evidence:

- [Linux audit](private_grading/pilots/click-pilot-linux-01/evidence/linux-audit-01/summary.json)
- [Verifiers cloud audit](private_grading/pilots/click-pilot-linux-01/evidence/cloud-framework-audit-01/summary.json)
- [Controller audit on Prime](private_grading/pilots/prime-migration-01/controller-audit-01/extracted/evidence/hosted-framework-audit/summary.json)
- [Migration status](private_grading/pilots/prime-migration-01/status.json)
- [VM cleanup check](private_grading/pilots/prime-migration-01/cleanup-verification.json)

The reference tests earned reward 1.0 for all ten bugs. Duplicate tests earned
0.1, two distinct bugs earned 0.2, and unmatched claims entered review. All 51
unit tests passed. Temporary VMs were removed; the private image is retained.

The native macOS pilot remains available. Cloud execution has no native or
unrestricted fallback. Offline tests use fake SDK responses and do not certify
Linux, Docker, or Prime isolation.

## Architecture

The trusted Verifiers controller starts an agent VM with a clean Click checkout.
It keeps the bug bank, repaired variants, cloud credential, and private reports
outside that VM. The agent uses the existing `command` tool.

Each command runs in a Docker container inside the agent VM. It runs without
administrator rights, with a read-only root filesystem and checkout, no network,
no added capabilities, and fixed CPU, memory, process, output, and time limits.
Only submissions and temporary storage are writable. The container cannot use
the VM's Docker socket or Prime credential. Prime's deny-all outbound policy
must be applied before the backend accepts the VM.

At submission, the controller collects only bounded, regular `test_*.py` files.
It deletes the agent VM before it starts a separate grading VM. Grading uses
the canonical source and a fresh container and directory for every variant and
repetition. The scorer still requires three stable runs, and duplicate tests
still earn no extra credit. Unknown claims still enter private review.

The Linux runtime uses Python 3.12.3. Its base image is pinned by an amd64 image
digest. Runtime Python dependencies use the existing version pins and package
hashes. The controller dependencies come from `uv.lock`, with hashes. A separate
Linux bundle receives its actual runtime lock only after a real Prime probe.
The original macOS runtime lock is preserved.

The image contains a prebuilt execution filesystem archive. Docker imports it
inside each VM without a network download. The image build context contains
only the Dockerfile, dependency locks, generic worker, and VM supervisor. It
contains no Click fixes, bug records, credentials, Git history, or catalog.

## Reproduce the migration

The migration above is complete. For a new run, use new evidence output paths.
Run from the repository root and check Prime access. The installed SDK reads `PRIME_API_KEY` or Prime's
configuration file. Do not paste a credential into chat.

```bash
uv run --frozen python -m click_pilot.cloud.migrate preflight
```

The current prepared context is under
`code_testing_eval/.cache/prime-migration/runtime-current`. A copy is saved in the private
migration evidence directory. To prepare a new context, use a new directory:

```bash
uv run --frozen python -m click_pilot.cloud.migrate prepare --context NEW_CONTEXT
```

Create the private image build, then inspect its status. The build command records
its ID before upload, so a lost connection does not require a duplicate build.
If upload or start fails, inspect the saved build ID before retrying. Signed
upload URLs and API keys are not saved in the build record.

```bash
uv run --frozen python -m click_pilot.cloud.migrate build
uv run --frozen python -m click_pilot.cloud.migrate status
```

Wait for a successful image build. Then pin the observed Linux runtime in a
new private bundle and run both audits. These commands use the image recorded
by the build command. The audits use known tests and a scripted local model;
they do not make external inference calls.

```bash
uv run --frozen python -m click_pilot.cloud.migrate initialize

uv run --frozen python -m click_pilot.cloud.migrate audit \
  --output code_testing_eval/private_grading/pilots/click-pilot-linux-01/evidence/linux-audit

uv run --frozen python -m click_pilot.cloud.migrate framework-audit \
  --output code_testing_eval/private_grading/pilots/click-pilot-linux-01/evidence/cloud-framework-audit
```

Each audit needs a new output path. A failed run is retained for inspection;
it is not marked as passed. The live audits check reference scores, duplicate
and multi-bug scores, invalid submissions, review queueing, hidden-data exclusion,
file/network restrictions, and timeout cleanup.

After both audits pass, run the same framework audit with the controller on a
separate Prime VM:

```bash
uv run --frozen python -m click_pilot.cloud.migrate controller \
  --output code_testing_eval/private_grading/pilots/prime-migration-01/controller-audit
```

This uses a dedicated Prime controller VM. It retains Verifiers 0.3.1 and does
not require publishing an environment to the Hub. It is not a deployment through
the managed `prime eval run --hosted` service. The controller payload contains
the private grading bundle and required code only. Its Prime credential is
injected into the trusted controller and is not copied into agent or grading VMs.
Private JSON evidence and logs are downloaded before the controller is deleted.

Agent, grading, and controller VMs have finite lifetimes. Resource identities
are saved before setup. Normal completion, failure, and cancellation attempt
cleanup; API failures can leave resources for explicit recovery. Check the private
VM journals and Prime's sandbox list after a controller or network failure.

```bash
uv run --frozen python -m click_pilot.cloud.migrate cleanup
```

That command removes journaled agent and grading VMs for the selected Linux
bundle. Audit and controller journals also record their VM IDs. Check those
separately if the process was terminated before its cleanup completed.

## Live model evaluations

The verified image is set in `configs/click-pilot-prime.toml`. Its dry run passed.
Before a model evaluation, set the intended model and inference endpoint. No
external model evaluation or training was run as part of this migration.

```bash
uv run --frozen eval @ configs/click-pilot-prime.toml --dry-run --no-push
uv run --frozen eval @ configs/click-pilot-prime.toml --no-push
```

The Verifiers harness runtime remains `subprocess`: it is the trusted chat
controller. The cloud backend places all model-directed commands and submitted
test execution inside Prime VMs. Switching the harness runtime to Prime does
not replace that backend configuration.

The controller-to-tool credential file has mode `0600` and is removed when the
environment stops. It is outside all agent mounts and excluded from deployment
evidence. API-key environment variables are not passed into submitted-code
containers.

## OpenRouter first agent run

`configs/click-pilot-openrouter.toml` selects
`deepseek/deepseek-v4.1-flash-1017` through OpenRouter. Its config dry run passed.
It uses one attempt, a 40-turn limit, and at most 4,096 completion tokens per
request. These limits do not enforce a dollar spending cap.

Put `OPENROUTER_API_KEY` in the ignored root `.env` file. The evaluation CLI does
not load this file itself. Load it into the controller process with:

```bash
.venv/bin/python -m dotenv -f .env run -- .venv/bin/eval @ configs/click-pilot-openrouter.toml --no-push
```

Before starting, check authenticated access, available credit, current model
pricing, and tool support. Use an OpenRouter key spending limit to enforce the
available inference budget. Prime VM charges are separate. Keep the model blind
to the private reference tests, repairs, and audit traces. Save and review the
first attempt before starting more attempts.

## Revised prompt and time-limit comparison

The revised historical-bug instructions were tested with separate 15-minute
and 30-minute attempts. The 30-minute condition finished at the 40-turn cap
and earned 0.3 for three distinct bugs. The 15-minute condition timed out;
separate diagnostic grading of a saved checkpoint also caught three bugs.
See [the full comparison](private_grading/pilots/agent-comparison-01/README.md)
for service errors, costs, attribution, and limits on the conclusion.

Use `configs/click-pilot-openrouter-15m.toml` or
`configs/click-pilot-openrouter-30m.toml` to inspect the frozen conditions.
Each completed output directory must be retained; use a new run name and
output directory for another independent attempt. Task time is configured with
`env.taskset.task.agent_timeout_seconds` (60 through 1800 seconds).
