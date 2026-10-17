# Batch review and verification

The runner prepares complete review inputs and executes trusted extracted
reproducers. It does not classify commits, infer test legitimacy, or execute
agent submissions. The catalog's full-history scope stays the same.

## Prepare a review packet

Keep the public Git objects in the ignored workspace cache. A temporary clone
can be cleaned up by the host. The mirror can contain newer refs; collection
and validation still use the catalog's pinned commit IDs.

```bash
git clone --mirror https://github.com/pallets/click.git code_testing_eval/.cache/click.git
python3 code_testing_eval/scripts/batch_runner.py prepare \
  --repo code_testing_eval/.cache/click.git \
  --output code_testing_eval/private_grading/reviews/batches/next-batch \
  --limit 25
```

Use a new output directory for each packet. `packet.json` retains every selected
commit, its parent list, message, changed paths, catalog record reference, and
complete patch reference. Byte-identical full patches share one file. No patch
is truncated. Grouping saves repeated reading; it does not establish semantic
equivalence or select a merge parent for the reviewer. Review every listed
commit and record decisions with the existing catalog tools.

## Execute reviewed records

```bash
python3 code_testing_eval/scripts/batch_runner.py run \
  code_testing_eval/private_grading/bugs/click-6131e96559e3a29aeb91129ac2a155af435a9433 \
  code_testing_eval/private_grading/bugs/click-430c50a3b6dca4e23936389513d2099b0af865cf \
  --repo code_testing_eval/.cache/click.git \
  --python .venv/bin/python --jobs 4 \
  --report code_testing_eval/.cache/batch-report.json
python3 code_testing_eval/scripts/audit_catalog.py
```

This command updates each selected record's evidence and validation status.
It requires an existing reviewed bugfix, claim, extracted reproducer, and
specific failure marker. A missing setup input clears stale validation and
snapshot eligibility. Other records continue. Skipped or failed work produces
a nonzero CLI exit status and appears in the report. Inspect the saved paired
logs before accepting a new bug claim; markers alone do not prove legitimacy.

Records run concurrently only when `reproducer.parallel_safe` is explicitly
`true`. Set that field after checking that the test does not share external
files, ports, mutable tool state, or other resources with another test. Unmarked
records run serially, after concurrent work finishes. The benchmark sets this
field on reviewed temporary copies only.

Within one batch, runtime metadata is read once per interpreter/dependency
combination. Git archives are read once per repository/full commit ID. Each
record/snapshot gets a fresh extracted directory, with the original verifier's
three repetitions. All six test processes still run for every record, including
duplicates. No test result or mutable extracted directory is shared. Setup
caches expire at the end of the invocation; package versions and source are
read again on the next invocation. Do not modify the runtime or repository
while a batch is running.

The terminal receives a compact result and a report path. Full paired logs and
runtime locks stay with each record. Dependency installation, historical runtime
provisioning, and upstream test extraction remain separate review steps.

## Repeat the benchmark

```bash
python3 code_testing_eval/scripts/benchmark_batch.py \
  --repo code_testing_eval/.cache/click.git --python .venv/bin/python \
  --output code_testing_eval/private_grading/benchmarks/new-run \
  --trials 3 --jobs 4
python3 code_testing_eval/scripts/benchmark_batch.py --measure-packet \
  code_testing_eval/private_grading/reviews/batches/2026-10-16-next-25/packet.json
```

The fixed 12-record sample covers runtime behavior, documentation, isolated
example definitions, an import failure, sibling records, and duplicate merge
provenance. Each trial runs both serial and batch verification on temporary
copies, with alternating execution order. It compares all six saved logs per
record, runtime metadata, and verdicts. Only temporary paths, unittest timings,
and unordered set-difference item display are normalized. Source catalog files
are hashed before and checked after the benchmark. Copies of full evidence,
sample hashes, individual trial timings, and medians are saved in the output.

This measures verification throughput and review-input size, not end-to-end
model classification accuracy or billing. Local token counts use the named
`cl100k_base` encoding when tiktoken and its public vocabulary are available.
They are not actual model usage. Token metrics include both raw-patch and
complete-review variants; the latter retains commit metadata and lookup IDs.
The benchmark is small, host-dependent, and includes warm OS filesystem caches.
The serial baseline already includes the verifier's combined runtime probe.

See `private_grading/benchmarks/batch-2026-10-16/REPORT.md` for the measured result.
