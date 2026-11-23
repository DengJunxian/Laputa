# Laputa

Follow [the convergence scope](docs/convergence/SCOPE_LOCK.md) and [stage status](docs/convergence/TASK_STATUS.md). Start a subsequent stage only when requested. Use the current Git source, contracts and executable tests to verify historical evidence.

- Use one WorldKernel clock and one EconomicRuntime authority. Freeze observations, check evidence, execute typed intents, and commit receipts before feedback becomes visible. Replay executes the recorded inputs again.
- Preserve legacy run readers and regression fixtures. Version incompatible state changes and keep model cost receipts independent of the economic journal.
- Reuse the audited components before considering up to two external candidates per new function. Preserve license and attribution files. Default to zero external model calls.
- Write or update the scope before each stage. Allow an implementation pass and a targeted QA/fix pass. Record unresolved errors after at most two targeted attempts. Stop at the stage gate. Keep deferred work in the backlog.
- Push, public releases, project license changes and paid model calls require explicit authorization.
