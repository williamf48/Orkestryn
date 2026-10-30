# Contributing

Open an issue with the observed behavior, expected behavior and a minimal reproduction. Remove credentials, private accounts and customer information from attachments.

Use Python 3.10+ and run `python scripts/check_repository.py` and `python scripts/test.py` before proposing a change. Run the relevant UI checks when modifying browser behavior. Changes to `app/` require regenerating the public payload manifest with `python scripts/update_manifest.py` before building.

Keep model recommendations separate from permissions. Do not bypass exact approvals, ownership checks, state versions or independent scoring to make a demonstration pass. Preserve failed model attempts and distinguish scripted, mocked and live results. Paid inference is not needed for the offline suite.

Submit a focused pull request explaining the behavior changed and the validation performed. Do not commit runtime data, generated distributions, credentials or test logs. See [testing](docs/testing.md).
