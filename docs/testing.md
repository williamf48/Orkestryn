# Testing

The offline suite uses Python's standard library, local workers/HTTP, fixtures and mocked provider transports. It makes no paid requests. Test classes and their fixtures are retained under app/ because several integration tests launch those modules directly.

```sh
python scripts/check_repository.py
python scripts/test.py
python app/project.py doctor
python app/operations_ui_checks.py
python app/safety_ui_checks.py --output /tmp/orkestryn-safety-ui.json
python run.py --demo --check --directory /tmp/orkestryn-clean-check
```

Node.js is needed only for UI checks. On Windows use `py -3` and new output directories beneath `$env:TEMP`. The isolated suite runner launches each registered test class in its own process to avoid cross-module global-state leakage; it writes logs outside the repository by default and fails on errors, source changes or skipped tests.

Individual classes can be run from app/, for example `python -m unittest safety_reliability_tests`. Optional Inspect integration uses `python -m pip install -r app/requirements-research.txt` and `python app/safety_inspect_check.py --output /tmp/inspect-check` on Python 3.11+.

Regenerate the app payload manifest after changing application source with `python scripts/update_manifest.py`. This hashes local public files, not evidence or private data. Then rerun relevant checks. Build the source ZIP and portable application with `python scripts/build_release.py --output dist`.

Linux CI, Windows smoke checks, native executable tests, live inference and public hosting are distinct observations. Report the exact platform and tested source; do not relabel an old result as acceptance of a new build.
