# D1 Foundation Implementation Plan

> **For agentic workers:** Keep all five data artifacts synchronized with `docs/plans/data-contracts.md` v1. The project maintainer manages contract changes; no per-member approval gate applies. Do not commit or merge unless separately requested.

**Goal:** Establish the local Python environment, provide a credential-safe GLM JSON smoke command, and align setup documentation with observed results.

**Architecture:** Use Miniconda under `%LOCALAPPDATA%\Miniconda3` and create `dns-dga` from the checked-in environment definitions. Add only a `--smoke` path to `src/llm_judge.py`; it reads config and the existing key helper, makes at most one HTTP request, validates the model and JSON response, and writes redacted evidence. Formal LLM judging remains out of scope.

**Tech Stack:** Windows PowerShell, Miniconda, Python 3.11.17, requests, pytest, official Zhipu GLM HTTP API.

## Execution Status (2026-11-01)

- Miniconda is installed at `%LOCALAPPDATA%\Miniconda3`; `dns-dga` uses Python 3.11.17.
- Installed versions match the seven direct dependency pins: `dnspython==2.9.0`, `requests==2.34.2`, `tldextract==5.4.0`, `numpy==2.4.6`, `pandas==3.0.6`, `matplotlib==3.11.2`, `pytest==9.1.1`. `pip check` reports no broken requirements.
- Smoke CLI and tests are implemented. Full suite: 13 passed.
- A real GLM-4-Flash smoke request succeeded: HTTP 200, valid JSON response, 2514 ms. The credential is configured only in the current Windows user's environment; the redacted result is in `report/evidence/d1_glm_smoke.json`.
- `docs/plans/data-contracts.md` defines v1 for all five data artifacts; the project maintainer owns future version updates.

---

### Task 1: Install and verify the local Conda environment

**Files:** Local install at `%LOCALAPPDATA%\Miniconda3`; read `environment.yml` and `requirements.txt` without changing dependency pins.

- [x] Confirm the user-local target was absent and the drive had sufficient space. Do not overwrite an existing installation.
- [x] Download the official Miniconda installer and verify its published SHA-256 before running it.
- [x] Install for the current user without modifying PATH or registering a global Python.
- [x] Create `dns-dga` from `environment.yml`. If terms-of-service prompts recur on another machine, stop and obtain that user's decision; do not bypass the prompt.
- [x] Verify Python version, all seven exact direct dependency pins, and `python -m pip check`.

Installer verification record: `Miniconda3-py314_26.9.4-0-Windows-x86_64.exe`, SHA-256 `4e5646825a87ac9ea86969fdaf7181e89b66b42e3f9107e4e6a61f65d76ae518`.

Verification commands:

```powershell
$installRoot = Join-Path $env:LOCALAPPDATA 'Miniconda3'
$conda = Join-Path $installRoot 'Scripts\conda.exe'
$env:CONDA_PKGS_DIRS = Join-Path $installRoot '.cache\pkgs'
$env:PIP_CACHE_DIR = Join-Path $installRoot '.cache\pip'
New-Item -ItemType Directory -Force $env:CONDA_PKGS_DIRS, $env:PIP_CACHE_DIR | Out-Null
& $conda env create --file environment.yml

$python = Join-Path $installRoot 'envs\dns-dga\python.exe'
& $python --version
& $python -m pip check
& $python -c "import importlib.metadata as m; names=['dnspython','requests','tldextract','numpy','pandas','matplotlib','pytest']; print('\n'.join(f'{n}=={m.version(n)}' for n in names))"
```

The environment already exists on this machine; do not rerun the create command here.

### Task 2: Add and test the smoke CLI

**Files:** `src/llm_judge.py`, `tests/test_llm_smoke.py`.

- [x] Write tests first and verify the initial failure was due to the missing smoke API/CLI, not test setup.
- [x] Implement `run_smoke_test(config_path=None, evidence_path=None)` and `main(argv=None)` with only the `--smoke` command.
- [x] Cover successful Bearer-authenticated JSON requests, redacted evidence, missing-key no-network behavior, malformed JSON, unexpected model IDs, timeout, HTTP error categories, and CLI output/exit status.
- [x] Verify focused and complete test runs. No live network is used by the automated tests.

Required behavior:

- Read model, endpoint, timeout, temperature, and `max_tokens` from `config.json`; obtain credentials only through `get_api_key()`.
- Make at most one HTTPS POST with redirects disabled. Do not add retries, cache behavior, formal verdicts, or response-body logging.
- Require the response model to match case-insensitively and the message content to parse as JSON with `status == "ok"`.
- Write only the allowlisted metadata to `report/evidence/d1_glm_smoke.json`; never save or print a credential, raw response, or exception text.

Verification commands:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Miniconda3\envs\dns-dga\python.exe'
& $python -m pytest tests/test_llm_smoke.py -q
& $python -m pytest tests/ -v
```

Observed: 13 smoke tests collected and passed; exit code 0.

### Task 3: Align setup documentation and non-secret config

**Files:** `README.md`, `config.json`, `docs/plans/d1-foundation-design.md`, this implementation plan.

- [x] Set the documented official endpoint to `https://open.bigmodel.cn/api/paas/v4/chat/completions` and add `max_tokens: 32`; do not put credentials in config.
- [x] Replace stale local environment paths and claims with the observed user-local install, creation and verification commands.
- [x] Finalize the five-artifact v1 contract, including the JSONL field contract; no per-member approval is required.
- [x] Check config JSON, evidence fields, whitespace, and changed documentation for consistency and secrets.

### Task 4: Make the real API smoke request when credentials are available

**Output:** `report/evidence/d1_glm_smoke.json`.

- [x] Check credential availability without printing or reading its value; verify `api.txt` is ignored by Git.
- [x] After the account owner configures the credential locally, make one real request with `python -m src.llm_judge --smoke`; HTTP 200 and JSON validation succeeded.
- [x] Verify the evidence uses the allowlisted fields and contains no key or raw response.

Current result: `success: true`, HTTP 200, model `GLM-4-Flash`, valid JSON response, latency 2514 ms. The evidence contains only allowlisted metadata and no credential or raw response.

## D1 Boundary

The environment, smoke, contract, and documentation checks described above are complete. No DNS dataset ingestion, detection pipeline, DGA evaluation, FPR/FNR report, commit, or merge is included in this D1 implementation plan.
