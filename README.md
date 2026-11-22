# Laputa

Laputa is the v4 EcoSim social and economic simulation workbench. Build a synthetic economy, run its households and firms through one checked ledger, fork an intervention from a committed period, and recompute the outcome from the resulting evidence. The preview includes banks and government, rule and recorded decision policies, frozen experiments, and a loopback browser workbench.

## Install and run

Use Python 3.12. The base install uses PyPI dependencies. From the repository root:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install .
mkdir -p runs

ecosim run --config examples/closed-economy-credit-offer.json --output runs/credit --until 2
ecosim resume runs/credit
ecosim replay runs/credit --output runs/credit-replay
ecosim export runs/credit --tick 3 --output runs/credit-tick3.json
```

The credit example has five periods. A registered household applies for 12 minor units at period 2, the bank funds it at period 3, and two installments repay the principal. Replay independently re-executes the saved inputs and reports matching committed states. Use fresh run directories and rerun with the same dependency versions for comparisons. The public snapshot has its own source identity.

Launch the local workbench and open the address printed by the process. Its initial interface is in simplified Chinese. The default workspace offers four registered intervention templates. The research preview workspace opens a joint 32-household world and the labor and withdrawal comparisons. Use the researcher link printed by the process. Separate observer links grant read-only access to the selected actor.

```sh
ecosim workbench --workspace runs/workbench
# In a separate terminal or after stopping the first server:
ecosim workbench --workspace runs/preview --research-preview examples
```

The examples and tests use simulated data. For a repeatable rule versus archived policy comparison with no provider credentials, the preview can generate and freeze its own synthetic responses. The following runs one documented template and independently audits the evidence:

```sh
ecosim labor-preview run examples/closed-economy-labor-choice-preview.json --seat 3 --output runs/labor
ecosim labor-preview audit runs/labor
```

Use `ecosim --help` to find control, inspection, and export commands. Install `.[research,population]` for statistical analysis and population fitting, `.[data]` for Parquet, or `.[llm]` for the optional live provider adapter. The documented quick starts are zero-call. The optional adapter requires an explicit provider descriptor and project and run budgets.

## Experiments and data

The examples register herding, network and strategy comparisons. The experiment driver freezes a new local plan and audits it separately. A full pilot takes longer than the quick start. Freeze and run from the same installed build with the analysis dependencies present.

```sh
python -m pip install '.[research,population]'
python scripts/goal05_research.py freeze --protocol examples/goal05-research-protocol.json --output runs/plan.json
python scripts/goal05_research.py run --plan runs/plan.json --output runs/pilot
python scripts/goal05_research.py audit runs/pilot
```

The observed network cooperation data are available directly from Zeyu Zhu's v1 replication deposit, DOI `10.5281/zenodo.19544006`, CC BY 4.0. Download `individual_data.csv` and check its SHA-256 `b37fa71434dd46249a0e7f43f9b9e798b1d3cafcffea3c80bc6997b99b6dd0a5` against the frozen calibration specification. The command refits on the registered training sessions and evaluates separate holdout sessions. [Zenodo record](https://zenodo.org/records/19544006) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

```sh
python scripts/rp06_calibrate.py --spec examples/rp06-calibration.json --raw /path/to/individual_data.csv --output runs/holdout.json
```

Run the source regression with `python -m pip install '.[research,population]' pytest` followed by `python -m pytest -q tests`. Parquet checks require the `data` extra.

## Scope and attribution

This is a source release of the reproducible research preview. The local acceptance used two archived purchase seats in a four-period, thousand-household example. The formal double-root, 100-period performance and replay gate and live model billing reconciliation remain pending. Bank and fiscal controls are registered templates. The securities experiment has its own prefunded liquidity. The original D1 confirmation retained natural stops and fell below its registered eligible-root minimum. Its inferential result remains unavailable.

Component terms are recorded in [Third-party notices](THIRD_PARTY_NOTICES.md). A project-wide open-source license has not been assigned.
