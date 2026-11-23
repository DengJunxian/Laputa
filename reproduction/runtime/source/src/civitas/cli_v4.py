"""Small standard-library CLI for the implemented v4 research slice."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import redirect_stdout
import json
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(prog='ecosim', description='EcoSim v4 research runner')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='create a fresh closed-economy run')
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--config', type=Path)
    run.add_argument('--ticks', type=int)
    run.add_argument('--households', type=int)
    run.add_argument('--seed', type=int)
    run.add_argument('--concurrency', type=int)
    run.add_argument('--social-mode', choices=['adaptive','static','none'])
    run.add_argument('--until', type=int)
    run.add_argument('--provider', type=Path, help='explicit live provider descriptor JSON')
    run.add_argument('--response-archive', type=Path, help='fixed offline structured requests and raw responses')
    run.add_argument('--project-ledger', type=Path, help='existing cumulative model expense ledger')
    run.add_argument('--run-budget', type=Path, help='explicit live per-run limits JSON')
    run.add_argument('--run-id', help='unique run identity in the project ledger')
    run.add_argument('--no-fault', action='store_true')
    resume = commands.add_parser('resume', help='resume without changing the frozen configuration')
    resume.add_argument('run', type=Path)
    resume.add_argument('--until', type=int)
    branch = commands.add_parser('branch', help='fork a rule run at a committed tick')
    branch.add_argument('run', type=Path)
    branch.add_argument('--tick', type=int, required=True)
    branch.add_argument('--output', type=Path, required=True)
    branch.add_argument('--intervention', type=Path, help='validated JSON for a next-period labor shift')
    workbench = commands.add_parser('workbench', help='serve the local research workbench')
    workbench.add_argument('--workspace', type=Path, required=True)
    workbench_inputs = workbench.add_mutually_exclusive_group()
    workbench_inputs.add_argument('--config', type=Path, help='optional initial configuration for the editor')
    workbench_inputs.add_argument('--supplier-preview', type=Path, help='serve a frozen synthetic supplier pair')
    workbench_inputs.add_argument('--withdrawal-template', type=Path,
                                  help='enable registration and launch of the local withdrawal comparison')
    workbench_inputs.add_argument('--labor-template', type=Path,
                                  help='enable registration and launch of the fixed seat-3 labor comparison')
    workbench_inputs.add_argument('--research-preview', type=Path, metavar='EXAMPLES_DIR',
                                  help='open the joint world, labor and withdrawal in one local workbench')
    workbench_inputs.add_argument('--choice-preview', type=Path, nargs='+', default=(),
                                  help='open an audited withdrawal and/or labor comparison')
    workbench.add_argument('--port', type=int, default=0)
    workbench.add_argument('--joint-pilot', type=Path, help='read-only archive from the registered joint shock pilot')
    replay = commands.add_parser('replay', help='re-execute and compare every committed state')
    replay.add_argument('run', type=Path)
    replay.add_argument('--output', type=Path, required=True)
    interventions = commands.add_parser('audit-intervention', help='recompute a frozen intervention pair from commits')
    interventions.add_argument('--kind', required=True, choices=('income', 'liquidity', 'revaluation', 'fiscal'))
    interventions.add_argument('--control', type=Path, required=True)
    interventions.add_argument('--treatment', type=Path, required=True)
    interventions.add_argument('--tick', type=int)
    interventions.add_argument('--download', type=Path, help='verify the browser comparison export')
    export = commands.add_parser('export', help='export a fixed-commit researcher projection')
    export.add_argument('run', type=Path)
    export.add_argument('--output', type=Path)
    export.add_argument('--tick', type=int)
    institutions = commands.add_parser('compare-institutions', help='verify synthetic B or E institutional arms')
    institutions.add_argument('--config', type=Path, required=True)
    institutions.add_argument('--output', type=Path, required=True)
    institutions.add_argument('--bank-policy-combination', action='store_true',
                              help='freeze fixed-dose and matched-budget joint bank policy designs')
    generate = commands.add_parser('synthetic-population', help='write a runnable 32- or 1000-household config')
    generate.add_argument('--households', type=int, choices=(32,1000), required=True)
    generate.add_argument('--seed', type=int, default=7)
    generate.add_argument('--output', type=Path, required=True)
    responses = commands.add_parser('freeze-offline-responses', help='freeze a completed prefix of synthetic responses')
    responses.add_argument('run', type=Path)
    responses.add_argument('--output', type=Path, required=True)
    fit = commands.add_parser('fit-population', help='fit and diagnose weighted joint household controls')
    fit.add_argument('--spec', type=Path, required=True)
    stream = commands.add_parser('export-tables', help='stream fixed-commit researcher tables')
    stream.add_argument('run', type=Path)
    stream.add_argument('--output', type=Path, required=True)
    stream.add_argument('--tick', type=int)
    stream.add_argument('--format', choices=['parquet','jsonl'], default='parquet')
    stream.add_argument('--batch-rows', type=int, default=256)
    inspect = commands.add_parser('inspect', help='write a read-only report for one disclosure scope')
    inspect.add_argument('run', type=Path)
    inspect.add_argument('--output', type=Path, required=True)
    inspect.add_argument('--ticks', nargs='+', type=int, required=True)
    inspect.add_argument('--audience', choices=['researcher','agent'], required=True)
    inspect.add_argument('--actor')
    inspect.add_argument('--format', choices=['html','json'], default='html')
    observation_index = commands.add_parser('observation-index', help='prepare actor rows from one observe.v1 commit')
    observation_read = commands.add_parser('observation-read', help='read one prepared, commit-bound actor observation')
    for command in (observation_index, observation_read):
        command.add_argument('run', type=Path)
        command.add_argument('--tick', type=int, required=True)
        command.add_argument('--commit', required=True)
        command.add_argument('--index', type=Path, required=True)
    observation_index.add_argument('--actor', action='append', required=True)
    observation_read.add_argument('--actor', action='append', required=True)
    research_index = commands.add_parser('research-index', help='build a fixed-commit researcher index')
    research_page = commands.add_parser('research-page', help='read an indexed researcher account page')
    research_summary = commands.add_parser('research-summary', help='read verified fixed-commit research metrics')
    for command in (research_index, research_page, research_summary):
        command.add_argument('run', type=Path)
        command.add_argument('--tick', type=int, required=True)
        command.add_argument('--commit', required=True)
    research_index.add_argument('--output', type=Path, required=True)
    research_index.add_argument('--state-only', action='store_true',
                                help='verify business state through the selected commit and leave OBSERVE audit separate')
    research_page.add_argument('--index', type=Path, required=True)
    research_summary.add_argument('--index', type=Path, required=True)
    research_page.add_argument('--page', type=int, default=1)
    inspect_pair = commands.add_parser('inspect-compare', help='compare two verified runs in a scoped offline report')
    inspect_pair.add_argument('left', type=Path)
    inspect_pair.add_argument('right', type=Path)
    inspect_pair.add_argument('--output', type=Path, required=True)
    inspect_pair.add_argument('--ticks', nargs='+', type=int, required=True)
    inspect_pair.add_argument('--audience', choices=['researcher','agent'], required=True)
    inspect_pair.add_argument('--actor')
    inspect_pair.add_argument('--baseline-tick', type=int)
    inspect_pair.add_argument('--labels', nargs=2, metavar=('LEFT', 'RIGHT'))
    retain = commands.add_parser('retain-history', help='make an independently verifiable history prefix')
    retain.add_argument('run', type=Path)
    retain.add_argument('--output', type=Path, required=True)
    retain.add_argument('--tick', type=int)
    audit = commands.add_parser('audit-history', help='fully verify business state at each commit')
    audit.add_argument('run', type=Path)
    audit.add_argument('--checkpoint-tail', action='store_true', help='trust the already audited checkpoint prefix')
    reindex = commands.add_parser('reindex', help='update or explicitly rebuild the derived commit index')
    reindex.add_argument('run', type=Path)
    reindex.add_argument('--repair', action='store_true')
    batch = commands.add_parser('batch-closed', help='run independent offline worlds')
    batch.add_argument('--config', type=Path, required=True)
    batch.add_argument('--output', type=Path, required=True)
    batch.add_argument('--seeds', nargs='+', type=int, required=True)
    batch.add_argument('--workers', type=int, default=1)
    batch.add_argument('--resume', action='store_true')
    commands.add_parser('schema', help='print the implemented C/D configuration schema')
    compare = commands.add_parser('compare', help='run a small paired synthetic mechanism comparison')
    compare.add_argument('--output', type=Path, required=True)
    compare.add_argument('--seeds', type=int, nargs='+', default=[7,19,43,71])
    compare.add_argument('--ticks', type=int, default=8)
    compare.add_argument('--households', type=int, default=8)
    pilot = commands.add_parser('labor-pilot', help='run or resume a paired labor pilot')
    pilot.add_argument('--output', type=Path, required=True)
    pilot.add_argument('--workers', type=int, default=1, help='independent world processes')
    pilot_mode = pilot.add_mutually_exclusive_group()
    pilot_mode.add_argument('--spec', type=Path, help='create a fresh pilot from a JSON specification')
    pilot_mode.add_argument('--analyze-only', action='store_true', help='recompute from existing verified commits')
    pilot_mode.add_argument('--compare-fees', metavar='HIGH', type=Path,
                            help='compare --output with a higher-fee pilot from verified commits')
    commands.add_parser('scenario-catalog', help='list registered scenarios, feedback loops and gates')
    suite = commands.add_parser('verify-scenarios', help='run frozen A–E assignments and verify independent replay')
    suite.add_argument('--spec', type=Path)
    suite.add_argument('--output', type=Path, required=True)
    suite.add_argument('--scenarios', nargs='+', choices=list('ABCDE'))
    suite.add_argument('--workers', type=int, default=1)
    suite.add_argument('--resume', action='store_true', help='run only assignments never started under the original plan')
    suite_audit = commands.add_parser('audit-scenarios', help='verify existing scenario receipts and commit chains')
    suite_audit.add_argument('run', type=Path)
    pair = commands.add_parser('herding-pair', help='freeze, package or audit a paired Scenario A preview')
    pair_modes = pair.add_subparsers(dest='pair_mode', required=True)
    freeze = pair_modes.add_parser('freeze', help='register the comparison before running either arm')
    freeze.add_argument('config', type=Path)
    freeze.add_argument('--output', type=Path, required=True)
    pack = pair_modes.add_parser('pack', help='seal the researcher download and the local authoritative histories')
    pack.add_argument('plan', type=Path)
    pack.add_argument('download', type=Path)
    pack.add_argument('--workspace', type=Path, required=True)
    pack.add_argument('--output', type=Path, required=True)
    pair_modes.add_parser('audit', help='recompute from a portable evidence bundle').add_argument('bundle', type=Path)
    supplier = commands.add_parser('supplier-preview', help='freeze or audit a one-slot offline supplier comparison')
    supplier_modes = supplier.add_subparsers(dest='supplier_mode', required=True)
    supplier_freeze = supplier_modes.add_parser('freeze')
    supplier_freeze.add_argument('config', type=Path)
    supplier_freeze.add_argument('--response-archive', type=Path, required=True)
    supplier_freeze.add_argument('--output', type=Path, required=True)
    supplier_audit = supplier_modes.add_parser('audit')
    supplier_audit.add_argument('registration', type=Path)
    supplier_audit.add_argument('export', type=Path)
    supplier_audit.add_argument('--workspace', type=Path, required=True)
    for name, help_text in (
        ('withdrawal-preview', 'run or audit a frozen verified-notice withdrawal pair'),
        ('labor-preview', 'run or audit a frozen competitive vacancy pair'),
    ):
        preview = commands.add_parser(name, help=help_text)
        preview_modes = preview.add_subparsers(dest='choice_mode', required=True)
        preview_run = preview_modes.add_parser('run')
        preview_run.add_argument('config', type=Path)
        preview_run.add_argument('--output', type=Path, required=True)
        preview_run.add_argument('--response-archive', type=Path)
        if name == 'labor-preview':
            preview_run.add_argument('--seat', type=int, required=True, help='frozen household index')
        preview_modes.add_parser('audit').add_argument('run', type=Path)
    args = parser.parse_args(argv)
    from civitas.application import economic_run
    from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
    if args.command == 'run':
        # StrictResearchModel JSON loader rejects duplicate keys and coercion.
        config = ClosedEconomyConfig.model_validate_json(args.config.read_text()) if args.config else ClosedEconomyConfig()
        changes = {key:value for key,value in {
            'ticks':args.ticks,'households':args.households,'root_seed':args.seed,
            'concurrency':args.concurrency,'social_mode':args.social_mode}.items() if value is not None}
        if args.no_fault:
            changes['fault_tick'] = None
        config = config.model_copy(update=changes)
        from civitas.cognition.budget import BudgetLimits
        from civitas.cognition.providers import ProviderConfig
        from civitas.storage.commit_store import read_json
        provider = ProviderConfig.model_validate_json(args.provider.read_text()) if args.provider else None
        limits = BudgetLimits.model_validate_json(args.run_budget.read_text()) if args.run_budget else None
        archive = read_json(args.response_archive) if args.response_archive is not None else None
        result = asyncio.run(economic_run.run_new(config, args.output, until=args.until, run_id=args.run_id,
            provider_config=provider, project_ledger=args.project_ledger, limits=limits, response_archive=archive))
    elif args.command == 'resume':
        result = asyncio.run(economic_run.resume_run(args.run, until=args.until))
    elif args.command == 'branch':
        try:
            from civitas.scenarios.closed_economy_config import LaborShiftIntervention
            intervention = (LaborShiftIntervention.model_validate_json(args.intervention.read_bytes())
                            if args.intervention is not None else None)
            result = economic_run.branch_run(args.run, tick=args.tick, destination=args.output, intervention=intervention)
        except (ValueError, OSError) as exc:
            result = {'status': 'failed', 'error': str(exc)}
    elif args.command == 'workbench':
        from civitas.application.workbench_http import serve
        from civitas.storage.commit_store import read_json
        template = read_json(args.config) if args.config else {
            'households': 8, 'firms': 2, 'ticks': 8, 'root_seed': 7, 'archive_observations': True,
            'fault_tick': None,
        }
        comparison_paths = {'withdrawal': args.withdrawal_template, 'labor': args.labor_template}
        joint_template = None
        if args.research_preview:
            template = read_json(args.research_preview/'closed-economy-joint-world.json')
            if not isinstance(template, dict):
                raise ValueError('The joint-world template must be a world configuration object')
            # The preview schedules the shift from the period-19 parent commit.
            template.pop('labor_income_shock', None)
            joint_path = args.research_preview/'closed-economy-security-feedback.json'
            if joint_path.is_file():
                joint_template = read_json(joint_path)
                reference_path = args.research_preview/'reference-loss-settings.json'
                if reference_path.is_file():
                    joint_template['reference_loss'] = read_json(reference_path)
            comparison_paths = {
                'withdrawal': args.research_preview/'closed-economy-bank-notice.json',
                'labor': args.research_preview/'closed-economy-labor-choice-preview.json',
            }
        comparisons = {}
        for kind, path in comparison_paths.items():
            if path is not None:
                raw = read_json(path)
                if not isinstance(raw, dict):
                    raise ValueError(f'The {kind} template must be a world configuration object')
                comparisons[kind] = raw
        launch_options = ({'research_preview': comparisons} if args.research_preview else
                          {kind+'_template': raw for kind, raw in comparisons.items()})
        serve(args.workspace, template, args.port, supplier_preview=args.supplier_preview,
              choice_previews=args.choice_preview, joint_template=joint_template, joint_pilot=args.joint_pilot,
              **launch_options)
        return 0
    elif args.command == 'replay':
        result = asyncio.run(economic_run.replay_run(args.run, destination=args.output))
    elif args.command == 'audit-intervention':
        from civitas.experiments.intervention_preview import audit_pair
        result = audit_pair(args.kind, args.control, args.treatment, tick=args.tick, download=args.download)
    elif args.command == 'export':
        result = economic_run.export_projection(args.run, destination=args.output, tick=args.tick)
    elif args.command == 'compare-institutions':
        from civitas.experiments.institutions import compare_institutions
        config = ClosedEconomyConfig.model_validate_json(args.config.read_bytes())
        result = asyncio.run(compare_institutions(config, args.output, bank_policy_combination=args.bank_policy_combination))
    elif args.command == 'synthetic-population':
        from civitas.population.synthetic import synthetic_population_config
        from civitas.storage.commit_store import atomic_json
        config = synthetic_population_config(args.households, args.seed)
        atomic_json(args.output, config.model_dump(mode='json'), replace=False)
        result = {'status': 'generated', 'households': config.households, 'firms': config.firms,
                  'root_seed': config.root_seed, 'source_id': config.population_input.source_id}
    elif args.command == 'freeze-offline-responses':
        result = economic_run.freeze_offline_responses(args.run, args.output)
    elif args.command == 'fit-population':
        from civitas.population.fit import HouseholdFit, fit_households
        with redirect_stdout(sys.stderr):
            result = fit_households(HouseholdFit.model_validate_json(args.spec.read_text()))
    elif args.command == 'export-tables':
        from civitas.storage.parquet_sink import export_economic_tables
        result = export_economic_tables(args.run, args.output, tick=args.tick, format=args.format, batch_rows=args.batch_rows)
    elif args.command == 'retain-history':
        from civitas.storage.history import retain_history
        result = retain_history(args.run, args.output, tick=args.tick)
    elif args.command == 'inspect':
        from civitas.application.inspection import write_inspection
        result = write_inspection(args.run, args.output, args.ticks, audience=args.audience,
                                  actor=args.actor, format=args.format)
    elif args.command in ('observation-index', 'observation-read'):
        from civitas.application.observation_cursor import PreparedObservationCursor, build_observation_index
        if args.command == 'observation-index':
            result = build_observation_index(args.run, args.index, tick=args.tick, commit=args.commit, actors=args.actor)
        else:
            cursor = PreparedObservationCursor(args.run, args.index, tick=args.tick, commit=args.commit)
            views = [cursor.read(actor) for actor in args.actor]
            result = views[0] if len(views) == 1 else {'observations': views}
    elif args.command in ('research-index', 'research-page', 'research-summary'):
        from civitas.application.research_cursor import build_research_index, research_page, research_summary
        if args.command == 'research-index':
            result = build_research_index(args.run, args.output, tick=args.tick, commit=args.commit,
                                          state_only=args.state_only)
        elif args.command == 'research-summary':
            result = research_summary(args.run, args.index, tick=args.tick, commit=args.commit)
        else:
            result = research_page(args.run, args.index, tick=args.tick, commit=args.commit, page=args.page)
    elif args.command == 'inspect-compare':
        from civitas.application.comparison_inspection import write_comparison
        result = write_comparison(args.left, args.right, args.output, args.ticks, audience=args.audience,
                                  actor=args.actor, baseline_tick=args.baseline_tick, labels=args.labels)
    elif args.command in ('audit-history', 'reindex'):
        from civitas.storage.commit_store import CommitStore
        store = CommitStore(args.run)
        if args.command == 'reindex':
            result = store.repair_index() if args.repair else store.update_index(verify_prefix=True)
        else:
            result = store.audit_history(checkpoint_tail=args.checkpoint_tail)
    elif args.command == 'batch-closed':
        from civitas.experiments.closed_batch import run_closed_batch
        config = ClosedEconomyConfig.model_validate_json(args.config.read_bytes())
        result = run_closed_batch(config, args.seeds, args.output, workers=args.workers, resume=args.resume)
    elif args.command == 'schema':
        result = ClosedEconomyConfig.model_json_schema()
    elif args.command in ('withdrawal-preview', 'labor-preview'):
        from civitas.experiments.labor_choice_preview import audit_labor_choice, run_labor_choice
        from civitas.experiments.withdrawal_preview import audit_withdrawal_preview, run_withdrawal_preview
        from civitas.storage.commit_store import read_json
        try:
            labor = args.command == 'labor-preview'
            if args.choice_mode == 'run':
                tape = read_json(args.response_archive) if args.response_archive else None
                config = read_json(args.config)
                operation = (run_labor_choice(config, args.seat, args.output, tape) if labor else
                             run_withdrawal_preview(config, args.output, tape))
                result = asyncio.run(operation)
            else:
                result = audit_labor_choice(args.run) if labor else audit_withdrawal_preview(args.run)
        except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
            result = {'status':'failed', 'error':str(exc)}
    elif args.command == 'supplier-preview':
        from civitas.experiments.supplier_preview import audit_supplier_preview, freeze_supplier_preview
        from civitas.storage.commit_store import read_json
        try:
            if args.supplier_mode == 'freeze':
                result = freeze_supplier_preview(read_json(args.config), read_json(args.response_archive), args.output)
            else:
                result = audit_supplier_preview(args.registration, args.export, args.workspace)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = {'status': 'failed', 'error': str(exc)}
    elif args.command == 'herding-pair':
        from civitas.experiments.herding_pair import audit_pair, freeze_pair, pack_pair
        from civitas.storage.commit_store import read_json
        try:
            if args.pair_mode == 'freeze':
                result = freeze_pair(read_json(args.config), args.output)
            elif args.pair_mode == 'pack':
                result = pack_pair(args.plan, args.download, args.workspace, args.output)
            else:
                result = audit_pair(args.bundle)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            result = {'status': 'failed', 'error': str(exc)}
    elif args.command == 'audit-scenarios':
        from civitas.experiments.scenario_archive import audit_suite
        result = audit_suite(args.run)
    elif args.command in ('scenario-catalog', 'verify-scenarios'):
        from civitas.experiments.scenario_catalog import CATALOG, ScenarioSuite, run_suite
        if args.command == 'scenario-catalog':
            result = {'scenarios': CATALOG}
        else:
            if args.spec is None and not args.resume:
                suite.error('--spec is required for a new suite')
            if args.resume:
                from civitas.storage.commit_store import read_json

                frozen = read_json(args.output/'plan.json')['plan']
                scenarios = args.scenarios or list(dict.fromkeys(r['scenario'] for r in frozen['assignments']))
                spec = ScenarioSuite.model_validate(frozen['spec']) if args.spec is None else None
            else:
                scenarios = args.scenarios or list(CATALOG)
                spec = None
            if args.spec is not None:
                spec = ScenarioSuite.model_validate_json(args.spec.read_bytes())
            report = run_suite(spec, args.output, scenarios=scenarios, workers=args.workers, resume=args.resume)
            result = {key: report[key] for key in ('status', 'plan_hash', 'scenario_ids',
                      'assigned_worlds', 'completed_worlds')}
            result['report'] = str(args.output.resolve() / 'report.json')
    elif args.command == 'labor-pilot':
        from civitas.experiments.labor_factorial import LaborPilotSpec, analyze_pilot, compare_partner_fees, run_pilot
        if args.compare_fees:
            result = compare_partner_fees(args.output, args.compare_fees)
        elif args.analyze_only:
            result = analyze_pilot(args.output)
        else:
            spec = LaborPilotSpec.model_validate_json(args.spec.read_text()) if args.spec else None
            result = asyncio.run(run_pilot(args.output, spec, workers=args.workers))
    else:
        from civitas.experiments.closed_economy import compare_conditions
        result = asyncio.run(compare_conditions(args.output, seeds=args.seeds, ticks=args.ticks, households=args.households))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get('status') not in ('different','failed','projection_failed','infeasible_support','residual_exceeds_tolerance') else 1


if __name__ == '__main__':
    raise SystemExit(main())
