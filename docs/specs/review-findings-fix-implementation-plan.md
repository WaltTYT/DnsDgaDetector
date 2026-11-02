# Review Findings Fix Implementation Plan

> **For agentic workers:** Execute each task test-first. Do not commit, merge, or push. Use only temporary fixtures; do not read, regenerate, or modify the license-restricted project data or root `data/dns_log.csv`.

**Goal:** Close the four confirmed review findings without changing shared CSV schemas or downstream file paths.

**Architecture:** Reject known-benign family labels in public-DGA input, restrict credential-bearing smoke requests to the documented official endpoint, restore the prior D2 output bundle after ordinary replacement failures, and validate every DGA CSV record by deterministic reconstruction. Keep all tests offline and isolated in `tmp_path`.

**Tech Stack:** Python 3.11, pytest, standard-library `pathlib`, `tempfile`, and CSV/JSON handling; existing `requests` client.

PowerShell commands below assume `$python` points to the existing `dns-dga` environment, for example:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Miniconda3\envs\dns-dga\python.exe'
```

---

## Task 1: Restrict the API credential destination

**Files:** `tests/test_llm_smoke.py`, `src/llm_judge.py`.

- [x] Add parameterized smoke tests for an unrelated HTTPS host, a lookalike official hostname, an IP-literal host, a non-443 port, and a non-official path. Each must return `config_error` and make zero HTTP calls.
- [x] Run `& $python -B -m pytest -q -p no:cacheprovider tests/test_llm_smoke.py`; confirm the new cases fail because the current validator accepts non-official HTTPS endpoints.
- [x] Restrict `_sanitize_endpoint` to `https://open.bigmodel.cn/api/paas/v4/chat/completions`, with no user info, query, or fragment, and only the default HTTPS port or explicit 443.
- [x] Re-run `& $python -B -m pytest -q -p no:cacheprovider tests/test_llm_smoke.py`; existing official-endpoint success and new rejection cases must pass.

## Task 2: Protect D2 label integrity and bundle consistency

**Files:** `tests/test_prepare_data.py`, `src/prepare_data.py`.

- [x] Add a loader test with `public_dga__*.csv` containing `family=legit`; assert it fails with source filename and row number rather than creating a positive DGA row.
- [x] Add a `_write_output_bundle` test with three existing sentinel outputs. Inject a one-time failure on the second promotion and assert all three original byte contents are restored and temporary/backup files are removed.
- [x] Run `& $python -B -m pytest -q -p no:cacheprovider tests/test_prepare_data.py`; confirm the new assertions expose the label and partial-replacement behavior.
- [x] Reject `family=legit` (case-insensitively after trimming) while parsing public-DGA files.
- [x] Preserve same-directory staging. Back up existing destinations before promotion; if any promotion fails, restore the complete old set and remove newly created destinations. If rollback itself fails, retain recoverable backups and report their paths. Do not claim crash/power-loss transactionality.
- [x] Re-run `& $python -B -m pytest -q -p no:cacheprovider tests/test_prepare_data.py`; all existing and new tests must pass.

## Task 3: Validate DGA CSV reconstruction

**Files:** `tests/test_gen_dga.py`, `src/gen_dga.py`, `docs/specs/d2-data-preparation-implementation-plan.md`.

- [x] Add tests proving `write_dga_csv` rejects a domain inconsistent with its algorithm/seed/params and leaves an existing target unchanged. Pass the fixture word-root file for `word_digit` records.
- [x] Run `& $python -B -m pytest -q -p no:cacheprovider tests/test_gen_dga.py`; confirm the mismatched-domain case fails because the current writer persists it.
- [x] Extend `write_dga_csv` with an optional `word_roots_path`; rebuild every record using `rebuild_dga_domain`, require exact domain equality, and perform all validation before opening/replacing the target.
- [x] Update the existing D2 API table with the optional path argument and its responsibility.
- [x] Re-run `& $python -B -m pytest -q -p no:cacheprovider tests/test_gen_dga.py`; all tests must pass.

## Final Verification

- [x] Run `& $python -B -m pytest -q -p no:cacheprovider tests/`; expect all tests to pass.
- [x] Run `& $python -B -m pip check`; expect `No broken requirements found.`
- [x] Run `git diff --check` and inspect `git status --short`; ensure only the planned source, tests, and plan files changed, with no generated data or credentials.
- [x] Do not commit, merge, or push without a separate explicit request.
