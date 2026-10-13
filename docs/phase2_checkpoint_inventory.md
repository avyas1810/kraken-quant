# Checkpoint inventory, 2026-10-14

Before Phase 2 implementation, HEAD was
`def04c45dd64746dca1be8b0c69cbea332489d91`, branch
`feature/kraken-research-live`, remote `origin`:
`https://github.com/avyas1810/kraken-quant.git`.

The checkpoint captures the existing daily research foundation and the interrupted
shadow-operations implementation/tests as found. Those operational changes are
work in progress; their inclusion is preservation, not production approval.

Repository inventory found no `PriceData_PHASE*.ipynb` notebooks, `phase1_results`,
`phase2_results`, `research_data_cache`, nine-asset four-hour frozen accounting
engine, Phase 1 net-return export or input-hash manifest. Neither tracked files nor
the working-tree recursive notebook/phase-name inventory contained those artifacts.
The existing configuration instead imports five USD assets and daily locked dates
from `examples/research_config.py`; existing research execution forbids test access.
This configuration will not be silently rewritten to stand in for a different study.

The user-provided +0.37 Sharpe, +12.01% return, -30.04% drawdown and initial Phase 2
statistics are unverified external claims in this checkout. They are not targets,
data inputs or generated findings. Missing historical artifacts block historical
replication and the claimed frozen-engine invariance check. Synthetic tests can
verify formulas and software contracts only.

Excluded from staging: `.env`, `.shadow-runtime`, datasets, notebooks with unknown
embedded data, bytecode, pytest caches, build/dist artifacts and API credentials.
The Phase 2 branch must be created only after the checkpoint push is confirmed.

## Supplemental discovery

A later filename-only search under the user folder found the notebooks, original
`phase1_results`, `phase2_results` and `research_data_cache` outside this repository,
under `C:/Users/avyas`. A separate clean repository named
`systematic-crypto-regime-research` also exists there and is left untouched.
The initial repository-only absence is therefore not a final data blocker.
Exact selected Phase 1 source cells, initial Phase 2 code and small provenance
tables are exported to `research/phase1_frozen` and secured in a supplemental
checkpoint. Notebook outputs and raw datasets remain excluded. The original
notebooks, caches, results and other repository are read-only throughout.
