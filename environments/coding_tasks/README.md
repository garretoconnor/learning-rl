# Coding tasks

A Verifiers **0.3.1 v1** suite for learning test-generation task design.
The model writes assertions from a function specification. The grader checks
those assertions against two correct implementations and three deliberately
faulty implementations per task.

| Task ID | Function | Skills exercised |
| --- | --- | --- |
| `clamp` | `clamp(x, lo, hi)` | Lower/upper bounds and interior values |
| `unique` | `unique(xs)` | Duplicates, order, zero, empty input |
| `chunks` | `chunks(xs, size)` | Remainders, exact multiples, stride, empty input |
| `insertion_index` | `insertion_index(xs, target)` | Duplicates, insertion at end, empty input |

## Run locally without a model

From the repository root:

```bash
uv sync --locked
uv run --frozen python -m coding_tasks list
uv run --frozen python -m coding_tasks prompt clamp
uv run --frozen python -m coding_tasks demo
uv run --frozen python -m unittest discover -s tests -v
```

To grade your own tests, save a UTF-8 file such as `my_tests.py`:

```python
assert clamp(-2, 0, 5) == 0
assert clamp(9, 0, 5) == 5
assert clamp(3, 0, 5) == 3
```

Then run:

```bash
uv run --frozen python -m coding_tasks grade clamp my_tests.py
```

This returns score, validity, correct-implementation success, killed bug names,
assertion count, and an error message when relevant. The demo uses hand-written
reference submissions; its scores are grader checks, not model performance.

## Submission contract and reward

Submit 1–32 statements of the form `assert FUNCTION(literal_args) == literal_expected`.
A single complete Python code fence and comments are accepted. Imports, helper
functions, variables, loops, keyword arguments, comprehensions, attribute
access, and arbitrary calls are rejected. The submitted function is provided
by the grader: the model must not implement it.

Arguments must satisfy the function's prompt. Integers are bounded to
[-10000, 10000], lists/tuples to 100 items, strings to 1000 characters, source
to 16000 characters, and the AST to 3000 nodes. Inputs here use integers and
integer lists; expected values may also use strings, None, booleans, and tuples.
Assertions use ordinary Python equality semantics.

Invalid, empty, or incorrect tests earn **0**. If every assertion passes both
correct implementations, reward is `detected mutants / 3`. A mutant is detected
when any assertion fails or the trusted implementation raises an exception.
Repeating a test does not increase the reward. Verifiers records grading
metrics and details in each trace.

The grader parses Python syntax and interprets literals and assertions itself;
it never executes submission source. This is deliberately a small assertion
language, not a general pytest/unittest runner. Prompts contain specifications,
not reference tests or mutant code. The source catalog is visible in this
learning repository; this four-task suite is not a held-out benchmark.

## Model-free Verifiers validation

```bash
uv run --frozen validate coding-tasks --runtime.type subprocess --only-gold --no-rich
```

This checks each task's reference submission through its validation hook.
It needs no model API key or hosted sandbox.

## Evaluate a model later

The checked-in `configs/coding-tasks-local.toml` uses a local OpenAI-compatible
endpoint at `http://127.0.0.1:8000/v1`, the `null` harness, and a subprocess
runtime. The null harness asks the model for text and does not execute its
answer as a shell command. Uploads are disabled. First validate the config:

```bash
uv run --frozen eval @ configs/coding-tasks-local.toml --dry-run --no-push
```

For a real evaluation, start a compatible local model server, replace
`local-model` with its served model ID, and set `LOCAL_MODEL_API_KEY` to the
server's key (or a nonempty placeholder if it accepts unauthenticated requests).
Then run:

```bash
uv run --frozen eval @ configs/coding-tasks-local.toml --no-serve --no-push
```

Actual evaluation may need network access to prepare the null harness's Python
script dependencies. It produces traces and logs under ignored `outputs/`.
No model server is installed here. No model calls or training were run during
suite creation.

## Extend the suite

Add a `Case` in `coding_tasks/cases.py`: specification, two independent correct
implementations, named faulty implementations, argument-domain validator, and
a reference test set that detects every mutant. Run the regression tests and
the model-free validation command. Trusted functions must terminate for every
accepted input. `taskset.py` handles prompts and Verifiers reward integration;
`grading.py` defines the assertion language and scoring rule.
