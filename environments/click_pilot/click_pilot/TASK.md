Find incorrect behavior in this Click checkout and write regression tests.

This is an unmodified historical snapshot of Click. No artificial bugs were
inserted. A real defect counts even if it was present in an official release.
Judge behavior against the public API and documentation. Familiar or historical
behavior can still be a bug; you do not need a different release to prove it.

Work offline. Inspect the source, documentation, and existing tests. Add new
unittest tests under submissions/test_*.py. Keep the library and existing tests
unchanged. Use ordinary behavioral assertions against Click. Do not inspect
source files, versions, grader state, or runtime internals from your tests.
No implementation changes, network access, skipped tests, or third-party imports.

As soon as you confirm a defect, save a small unittest under submissions/ and
run it before exploring another area. Keep each confirmed finding as a saved
test. Scratch probes and your final message are not submissions. You can add
more tests after saving the first one. A failing regression test is expected
on this checkout when it demonstrates incorrect library behavior.

Run your new tests with: python3 -I -X utf8 -B run_tests.py
Existing upstream tests are available for reference; they are not your submission.
Run them with:
python3 -X utf8 -B -m pytest tests -q -p no:warnings -p no:cacheprovider
This command disables pytest warning capture and cache writes. The historical
warning-output test otherwise fails under pytest's warning capture, and the
read-only checkout prevents cache writes. These are test-runner issues.

Tests should fail because of incorrect Click behavior and pass when that behavior
is corrected. More than one independent bug may earn credit. Repeated tests for
the same bug earn no additional credit. Findings outside the known bank are
queued for review rather than automatically declared incorrect.
