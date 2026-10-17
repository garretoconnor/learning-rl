# learning-rl

This code-only checkout excludes `code_testing_eval/private_grading/`, including
the bug catalog, source snapshots, grading bundles, and run results. Click pilot
execution requires a separately prepared bundle. Dataset-dependent checks skip
when the bundle is absent; the introductory coding tasks need no external data.

Learning reinforcement learning and coding-task environment design.

The [Click bug-discovery catalog](code_testing_eval/README.md) contains the
in-progress repository history audit and private grading records for real bugs.
The [Click pilot](code_testing_eval/PILOT.md) runs one repository task with ten
known bugs through Verifiers, with sandboxed agent tools and private scoring.
Its local evaluation config is `configs/click-pilot-local.toml`. For cloud runs,
the minimal, Codex, and dsh agent loops use dedicated Prime Linux VMs and the
Responses API. Chat Completions is not supported by the Click pilot.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then from
this directory run:

```bash
uv sync --locked
uv run --frozen python -c 'import verifiers; print(verifiers.__version__)'
```

The project pins **Verifiers 0.3.1** from PyPI. `uv.lock` records the full
dependency set; `.python-version` selects Python 3.12. Python 3.12 and 3.13 are
supported by this project. The local `.venv` is ignored by Git.

## Verifiers commands

The published 0.3.1 wheel has different entry points from current upstream
`main`. Use these commands for its **v1** API:

```bash
uv run --frozen init --help
uv run --frozen eval --help
```

The [coding task suite](environments/coding_tasks/README.md) is installed as a
local workspace dependency. It contains four test-generation tasks with
mutation-based rewards. Try it without any model credentials:

```bash
uv run --frozen python -m coding_tasks demo
uv run --frozen python -m coding_tasks prompt clamp
uv run --frozen python -m coding_tasks grade clamp my_tests.py
uv run --frozen python -m unittest discover -s tests -v
```

V1 code imports `verifiers.v1`. Validate the included local evaluation config
without running inference:

```bash
uv run --frozen eval @ configs/coding-tasks-local.toml --dry-run --no-push
```

Dry runs write a resolved configuration under ignored `outputs/`. Actual model
evaluation needs a running endpoint; see the suite README for instructions.

The legacy API is also included: `import verifiers as vf`,
`uv run --frozen vf-init --help`, and `uv run --frozen vf-eval --help`.
In this release, the `vf-` commands use the legacy API; they are not aliases for
v1 `init`/`eval`.

## Credentials and runtimes

Installation, imports, and CLI help need no credentials. Actual evaluations
need a reachable model endpoint and its API key, unless using a local endpoint
that does not require one. V1 evaluation defaults to Prime inference, a Prime
sandbox runtime, and uploading results. Configure these deliberately before
running: Prime services require `PRIME_API_KEY`; another endpoint uses
`client.base_url` and `client.api_key_var`; a local Docker runtime requires
Docker. Use `--no-push` to disable result uploads. Keep credentials out of Git
(the existing `.gitignore` ignores `.env`).

The optional [Prime CLI](https://github.com/PrimeIntellect-ai/prime) is useful
for Hub and platform operations; it is not required for the installed local
commands. The task suite is local. No training run, hosted resources, or model calls
were created during setup.

## Installation verification

Verified on macOS with Python 3.12.3 and uv 0.8.14: package version and imports
of v1 `Task`, `Taskset`, `Env`, legacy `SingleTurnEnv` and `Rubric`, plus help
for `init`, `eval`, `vf-init`, and `vf-eval`. These setup checks need no model calls.

The coding suite passes 12 regression tests, Verifiers' model-free validation
for all four tasks, and a dry run of the included local model configuration.
The Click pilot also passes an offline Verifiers rollout audit and the standard
gold-validation command. Those checks use a scripted localhost model stub.

References: [published package](https://pypi.org/project/verifiers/0.3.1/)
and [official source](https://github.com/PrimeIntellect-ai/verifiers).
