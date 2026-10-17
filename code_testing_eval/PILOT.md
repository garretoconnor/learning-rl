# Click test-writing pilot

This code-only checkout excludes `code_testing_eval/private_grading/`, including
the bug catalog, source snapshots, grading bundles, and run results. Click pilot
execution requires a separately prepared bundle. Dataset-dependent checks skip
when the bundle is absent; the introductory coding tasks need no external data.

The pilot now runs through **Prime Intellect Verifiers 0.3.1**. It is one
repository task with ten existing, validated bugs in one Click snapshot.
Verifiers controls task setup, the agent loop, tool calls, traces, and reward.
The existing private differential grader supplies the reward evidence.
Further catalog collection is on hold.

The Prime VM backend and pinned Linux image are validated. The Linux scoring
audit and both Verifiers cloud audits passed, including the run with the
controller on Prime. The native macOS path remains available. See
[the migration guide](PRIME_MIGRATION.md) for evidence and execution commands.

## Run through Verifiers

Run these commands from this repository in a normal macOS terminal:

```bash
uv sync --frozen

# Check the evaluation config without calling a model.
uv run --frozen eval @ configs/click-pilot-local.toml --dry-run --no-push

# Check the reference submission through a real Verifiers rollout and tools.
# A deterministic localhost model stub sends the known reference tests.
uv run --frozen validate click-pilot --runtime.type subprocess --only-gold \
  --max-concurrent 1 --no-rich \
  --output-dir code_testing_eval/private_grading/pilots/click-pilot-01/evidence/cli-validation

# Run the complete framework integration audit. Use a new private output path.
uv run --frozen python -m click_pilot.validation \
  --output code_testing_eval/private_grading/pilots/click-pilot-01/evidence/new-verifiers-audit
```

For a live evaluation, start a Responses-compatible model server, set its model ID
and endpoint in `configs/click-pilot-local.toml`, then run:

```bash
uv run --frozen eval @ configs/click-pilot-local.toml --no-push
```

The `click-pilot` taskset loads one task. Each rollout exports a fresh workspace.
The model receives the public task prompt and one MCP tool, `command`, which can
read the checkout, write submission files, and run tests inside the native
sandbox. The model's final reply ends the attempt; files determine the score.
The bundled harness uses the MCP-only Responses loop; Chat Completions is no
longer supported. Other execution harnesses are
rejected because they could expose the controller host.

The `bug_discovery` reward hook grades those files privately, then records the
reward, confirmed-bug count, pending-review count, and submission status on the
Verifiers trace. It does not send private bug identities or repaired variants
to the model. Attribution and execution logs remain in
`private_grading/pilots/click-pilot-01/verifiers_runs/<run-id>/grading.json`.
Each run also retains its workspace there for inspection; workspace copies are
ignored by Git. Failed or interrupted runs can leave these private workspaces.
Remove a run directory only after its evidence is no longer needed.

The framework uses the uv environment. Commands and grading use the separate,
existing Python runtime in the private bundle's lock. The host paths are in
controller config, not task wire data. This package runs from this checkout;
it does not embed the private bundle in a distributable public task package.
See [the adapter guide](../environments/click_pilot/README.md) for configuration.

The original accepted framework audit is
`private_grading/pilots/click-pilot-01/evidence/verifiers-accepted/summary.json`.
Migration regression evidence is recorded separately under the same evidence
directory; it does not certify the cloud backend.
It covers full reference credit, zero credit for passing tests, duplicate and
multi-bug credit, rejected submissions, review queueing, fresh workspaces, and
file/network isolation through actual MCP calls. The model stub is scripted;
these checks do not measure model capability. No external inference was used.

## Export a workspace manually

The native commands below are also available for debugging the private grader.
Verifiers performs these steps during normal pilot runs.

Run from the learning-rl repository in a normal macOS terminal. The checked
bundle is `private_grading/pilots/click-pilot-01`. Its runtime lock requires
CPython 3.12.3 on the recorded macOS platform and pins the installed pytest
dependencies used for upstream-test exploration. New submitted tests use only
the standard library and snapshot Click. Worker Python runs with UTF-8 mode on.

```bash
python3 code_testing_eval/scripts/pilot.py new \
  --bundle code_testing_eval/private_grading/pilots/click-pilot-01 \
  --workspace code_testing_eval/.cache/my-click-task
```

Give the agent only the exported workspace and its `TASK.md`. Do not mount or
attach the learning-rl checkout, Git mirror, private bundle, or grading reports
to its working environment. The workspace contains Click source, documentation,
examples, existing upstream tests, the license, runtime version information,
and a command for running new tests. It contains no Git database, changelog,
future fix data, private bug records, private tests, or snapshot commit ID.

The task asks the agent to find incorrect behavior and add ordinary unittest
cases under `submissions/test_*.py`. Existing tests are reference material;
they are not submitted and are not promised to pass under modern pytest.
The pilot submission contract disallows runtime/source introspection, dynamic
execution, skips, and imports beyond the documented standard-library allowlist
in `pilot/submission.py`. A file should define TestCase classes. The standard
`if __name__ == '__main__': unittest.main()` footer is accepted.

## Explore and test through the agent interface

```bash
python3 code_testing_eval/scripts/pilot.py exec \
  --bundle code_testing_eval/private_grading/pilots/click-pilot-01 \
  --workspace code_testing_eval/.cache/my-click-task -- \
  /bin/ls click

python3 code_testing_eval/scripts/pilot.py exec \
  --bundle code_testing_eval/private_grading/pilots/click-pilot-01 \
  --workspace code_testing_eval/.cache/my-click-task -- \
  python3 -I -X utf8 -B run_tests.py
```

The controller may use the same `exec` interface to create or edit test files.
Source and existing tests are read-only inside the native sandbox. Only
`submissions/` and `scratch/` are writable. Networking, private file reads, and
private directory listings are denied. Commands have a wall timeout and CPU
and output limits. Grading uses fresh directories and a cleared environment
for every repetition. No host credentials are passed to the worker.

The interface uses macOS `sandbox-exec`. A restricted outer sandbox may prevent
it from starting; it then fails instead of executing without isolation. The
verified local path launches this interface from the host. Linux/container
support has not been implemented or certified.

## Submit tests

```bash
python3 code_testing_eval/scripts/pilot.py grade \
  --bundle code_testing_eval/private_grading/pilots/click-pilot-01 \
  --workspace code_testing_eval/.cache/my-click-task \
  --report code_testing_eval/private_grading/pilots/click-pilot-01/evidence/my-submission.json
```

Reports must stay in the private bundle. The terminal returns only the status,
score, and report path. The private report retains attribution and full logs.
Passing `--workspace` verifies that source, existing tests, and task assets
are unchanged. Alternatively, `--submission DIRECTORY` accepts only flat new
test files and builds every execution from the canonical source archive.

The grader runs each submission three times on the original snapshot, three
times on an all-repaired control, and three times on each diagnostic variant.
A diagnostic variant restores exactly one original bug into the repaired tree.
Credit requires the same test to fail for executed Click behavior on the
original tree, pass the repaired control, and fail with that bug restored.
Per-test outcomes and error categories must be stable across repetitions.

Reward is **distinct confirmed bugs detected / 10**. Duplicate tests do not
increase it. A test can detect several bugs. Passing-only submissions score
zero. Syntax, collection, dependency, setup, unrelated failures, skipped tests,
and source edits receive no credit. Behavioral failures without confirmed bank
attribution go to `review_queue/` and return `needs_review`; this does not declare
the claim either legitimate or wrong. Confirmed credit from other tests is
retained while the unmatched claim awaits review.

Differential results are evidence, not a proof that arbitrary submitted Python
encodes a legitimate specification. The AST rules enforce a pilot contract;
the native sandbox supplies the file/network boundary. Private reports and
reviewable tests remain necessary for reward audits. Do not claim adversarial
grader robustness or model capability from the smoke tests.

## Verify or rebuild

```bash
python3 -m unittest discover -s code_testing_eval/tests -v

python3 code_testing_eval/scripts/pilot.py verify \
  --bundle code_testing_eval/private_grading/pilots/click-pilot-01 \
  --output code_testing_eval/private_grading/pilots/click-pilot-01/evidence/new-audit
```

Use a new output directory. The audit checks all ten bug attributions,
compatibility controls, source/secret exclusion, actual private-read and
network denial, writable test files, source tampering, duplicate and multi-bug
reward, collection/setup failures, unmatched-claim queueing, and timeout cleanup.

The public source archive is included in the private bundle, so task export
does not need Git or network access. To create a new bundle from the source
mirror, use `pilot.py build --repo code_testing_eval/.cache/click.git --bundle
NEW_PRIVATE_BUNDLE`, then run the complete audit. Runtime or repair changes
require a new verified bundle; source, plan, runtime lock, and reference tests
are content-hashed in `bundle_manifest.json`.

The current private implementation and evidence are described in
[the bundle report](private_grading/pilots/click-pilot-01/README.md). The agent
interface smoke test used a new test written by the current assistant after
source inspection. It is an integration check with prior bug knowledge, not
a blind model evaluation. No external model call or training run was made.
