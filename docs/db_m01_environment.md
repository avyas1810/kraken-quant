# Database Milestone 1: environment and connectivity

This milestone prepares local storage and verifies the environment. It creates no persistent database schema, asset catalogue, price history, strategy or paper-execution pipeline. `data/metadata.sqlite` is reserved for a later milestone and is never opened or created by this diagnostic. Only `data/` and `data/market_data/` are prepared; SQLite and Parquet probes use uniquely named temporary directories and clean up on success or failure. Existing files are not replaced.

## Repository audit and reuse

Base: `review-base/milestone1-20261029`, fetched at merge commit `37f74f548d96389c15176ddf735d9763fabb341c`. PR #1 was merged, with no other open PRs at the initial audit. The working tree had two modified tracked notebooks, three untracked notebooks and untracked environment snapshots; all are excluded from this change. Preservation hashes cover notebooks, research artifacts and `.env`.

The existing runtime was Python 3.10.11. Existing requirements-ci.txt pins the core test/data/CCXT stack; requirements-research.txt adds statistical libraries, and requirements-notebook.txt declares notebook dependencies. setup.py describes legacy TWSQ installation and has old packaging constraints (`multidict<5`, Python >=3.0); do not install that legacy profile into the database environment. Run from the repository root instead. No strategy or research configuration changes are needed.

Reuse `market_data.public_get` for bounded public REST requests and `milestone1.check_connectivity` / `credentials.readonly_from_env` for dedicated, permission-checked private reads. The existing `streaming.WebSocketMarketData` uses websocket-client; no extra HTTP or WebSocket client is introduced. The existing locked research universe is not a database asset catalogue. Public metadata diagnostics inspect the complete AssetPairs response independently of it. Existing spot/risk/accounting and reconciliation SQLite stores remain separate and untouched.

`db_environment.EnvironmentConfig` only locates storage and the existing `.env`. Explicit `--data-dir` overrides `KRAKEN_DATA_DIR` in that file; otherwise `data/` is relative to `--root`. `--env-file` is resolved relative to `--root`. Loading uses existing python-dotenv without interpolation or global environment mutation. The diagnostic never displays configuration values or private exception text.

## Isolated installation (Windows PowerShell)

Python 3.11.14 is installed under ignored `.shadow-runtime/db-python`, with `.venv-db` as an independent environment. The existing Python installations, research environments and PATH are preserved. For an equivalent fresh checkout with an existing Python interpreter:

```powershell
python -m pip install --target .shadow-runtime/db-tools uv==0.9.7
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Get-Location) '.shadow-runtime/db-python'
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.shadow-runtime/db-cache'
./.shadow-runtime/db-tools/bin/uv.exe python install 3.11.14 --no-bin
./.shadow-runtime/db-tools/bin/uv.exe venv .venv-db --python 3.11.14 --managed-python
./.shadow-runtime/db-tools/bin/uv.exe pip sync --python .venv-db/Scripts/python.exe --require-hashes requirements-db.lock
```

If Python 3.11 is already installed, `py -3.11 -m venv .venv-db` followed by `.venv-db/Scripts/python.exe -m pip install --require-hashes -r requirements-db.lock` is sufficient. On Linux use `python3.11 -m venv .venv-db` and `.venv-db/bin/python -m pip install --require-hashes -r requirements-db.lock`. Do not overwrite an existing environment. The managed Python download comes from uv's python-build-standalone distribution; see [uv Python installation](https://docs.astral.sh/uv/guides/install-python/).

requirements-db.in includes the existing three requirement files plus PyArrow 19.0.1. requirements-db.lock pins transitive versions and accepted source/wheel hashes for Python 3.11 on Windows/Linux. Regenerate deliberately using uv 0.9.7:

```powershell
./.shadow-runtime/db-tools/bin/uv.exe pip compile requirements-db.in --python-version 3.11 --universal --generate-hashes --output-file requirements-db.lock
```

Generated environments, local data, SQLite/DB files and Parquet files are ignored by Git. Broad extension ignores do not untrack existing research artifacts.

## One diagnostic command

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.db_environment
```

It reports individual PASS/FAIL/SKIP results for Python >=3.11; NumPy, pandas, PyArrow, CCXT, websocket-client, pytest and python-dotenv imports/versions; directory writes; SQLite commit/reopen; Parquet nullable-type round-trip; public Kraken AssetPairs parsing with total pair count and AUD spot-pair presence; and configured read-only account authentication/balances. The standard-library urllib client and sqlite3 are reused. The [Kraken AssetPairs endpoint](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs) is public and requires no account key.

Missing dedicated read-only credentials produce SKIP, an explicit environment limitation. One missing credential or failed configured authentication produces FAIL. Generic/trading credentials are never substituted. Private checks use the existing safe read-only adapter; unsafe permissions fail closed. There is no order submission path. Public network failures are mandatory FAIL; HTTP/exchange exception details are suppressed. Exit status is 1 if any check fails, otherwise 0. `--json` emits structured results. `--offline` explicitly skips both network checks for automated use; an offline pass cannot satisfy connectivity acceptance.

## Tests and CI

```powershell
.venv-db/Scripts/python.exe -m pytest -q tests/test_db_environment.py
.venv-db/Scripts/python.exe -m pytest -q tests
$env:KRAKEN_PUBLIC_SMOKE = '1'
.venv-db/Scripts/python.exe -m pytest -q tests/test_db_environment.py::test_real_kraken_public_connectivity
Remove-Item Env:KRAKEN_PUBLIC_SMOKE
```

The real public test is opt-in and skipped by default. Automated tests use mocked APIs and temporary files, cover missing credentials, error redaction, import failures, configuration precedence, data preservation and mandatory-failure exit codes. CI adds a separate Python 3.11 job installing the hash lock, running the full offline suite and the offline diagnostic. Existing Python 3.10 research CI remains as a compatibility check; PyArrow-specific tests skip there if its older profile lacks PyArrow.

## Acceptance and limits

Required: isolated Python >=3.11; reproducibly installed/importable dependencies; temporary SQLite/Parquet round-trips; writable directories; successful actual public request with pair count and identified AUD availability; read-only success or accurately disclosed missing credentials; tests and CI passing; preserved notebooks/configuration/artifacts; no secrets/generated stores in the commit; dedicated branch and unmerged PR. Storage probes do not certify future data ingestion, database migrations, streaming uptime, backups or strategy performance. Stop at this milestone; schema development requires review approval.

Local verification on the isolated environment succeeded with these outputs:

```text
PASS python: {"minimum": "3.11", "version": "3.11.14"}
PASS dependency:numpy: {"version": "1.24.3"}
PASS dependency:pandas: {"version": "2.0.1"}
PASS dependency:pyarrow: {"version": "19.0.1"}
PASS dependency:ccxt: {"version": "4.5.64"}
PASS dependency:websocket-client: {"version": "1.8.0"}
PASS dependency:pytest: {"version": "9.1.1"}
PASS dependency:python-dotenv: {"version": "1.2.3"}
PASS directories: {"directories_writable": 2}
PASS sqlite: {"sqlite_version": "3.50.4", "temporary_round_trip": true}
PASS parquet: {"engine": "pyarrow", "temporary_round_trip": true}
PASS kraken_public: {"aud_spot_pair_count": 14, "aud_spot_pairs_present": true, "total_pairs": 1464}
PASS kraken_readonly: {"authenticated": true, "balance_read": true, "trading_required": false}
Diagnostic exit code: 0
```

Pair counts are a diagnostic observation and can change. No metadata response or account balance values are persisted. Dependency compatibility verification (`uv pip check`) passed for all 82 installed packages. Local tests: `python -m pytest -q tests/test_db_environment.py` produced 25 passed, 1 skipped; `python -m pytest -q tests` produced 437 passed, 1 skipped. The explicit public smoke test produced 1 passed. The skip is the opt-in network test during offline runs. Module compilation and staged diff checks passed. Final CI results are recorded in the PR and delivery response.
