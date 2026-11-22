"""Synthetic B/E manipulation checks on independently committed C/D worlds."""
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.contracts.economic import WithdrawDepositIntent
from civitas.economy.banking import GUARANTOR, ISSUER
from civitas.economy.fiscal import OPERATIONS, TREASURY
from civitas.experiments.closed_economy import audit_snapshot
from civitas.storage.commit_store import CommitStore, atomic_json


def _audit_treasury_support(state):
    events = [e for e in state['journal'] if e['kind'] == 'bank'
              and e['payload']['action'] in ('support_disburse', 'support_collect')]
    if not events:
        return {}
    disbursed = repaid = guaranteed = 0
    for event in events:
        bank = event['payload']['reference']
        amounts = {(p['entity_id'], p['account_code'], p['side']): p['amount_minor'] for p in event['postings']}
        if event['payload']['action'] == 'support_disburse':
            funding = amounts.get((TREASURY, 'cash', 'credit'), 0)
            expected = [(bank, 'bank_reserve', 'debit'), (bank, 'loan_payable', 'credit'),
                        (ISSUER, 'cash', 'debit'), (ISSUER, 'reserve_payable', 'credit'),
                        (TREASURY, 'loan_asset', 'debit')]
            if any(amounts.get(key, 0) != funding for key in expected):
                raise AssertionError('Treasury advance did not fund all reserve and principal legs')
            disbursed += funding
        else:
            bank_cash = amounts.get((bank, 'bank_reserve', 'credit'), 0)
            guarantee = amounts.get((GUARANTOR, 'cash', 'credit'), 0)
            expected = {(ISSUER, 'cash', 'credit'): bank_cash, (ISSUER, 'reserve_payable', 'debit'): bank_cash,
                        (bank, 'loan_payable', 'debit'): bank_cash + guarantee,
                        (TREASURY, 'loan_asset', 'credit'): bank_cash + guarantee,
                        (GUARANTOR, 'guarantee_expense', 'debit'): guarantee,
                        (bank, 'debt_release_income', 'credit'): guarantee}
            # Bank cash and guarantee may produce separate Treasury cash debits.
            treasury_cash = sum(p['amount_minor'] for p in event['postings']
                                if (p['entity_id'], p['account_code'], p['side']) == (TREASURY, 'cash', 'debit'))
            if treasury_cash != bank_cash + guarantee or any(amounts.get(k, 0) != v for k, v in expected.items()):
                raise AssertionError('Treasury advance collection legs do not reconcile')
            repaid += bank_cash
            guaranteed += guarantee
    treasury = state['balances'][TREASURY]
    outstanding = disbursed - repaid - guaranteed
    allowance = -treasury.get('loan_allowance', 0)
    advances = state['bank_positions'].get('support_advances', {})
    recorded = sum(row['outstanding_minor'] for row in advances.values())
    expected_allowance = sum(row['outstanding_minor'] for row in advances.values() if row['status'] == 'defaulted')
    if (outstanding != treasury.get('loan_asset', 0) or outstanding != recorded or allowance != expected_allowance):
        raise AssertionError('Treasury cash flows, nominal advance and allowance disagree')
    return {'disbursed_minor': disbursed, 'bank_repaid_minor': repaid, 'guarantee_paid_minor': guaranteed,
            'outstanding_minor': outstanding, 'allowance_minor': allowance,
            'treasury_net_cash_outlay_minor': outstanding, 'public_net_cash_outlay_minor': disbursed - repaid}


def audit_institutions(snapshot):
    state = snapshot.data['economic_runtime']
    book = state['balances']
    result = {'economy': audit_snapshot(snapshot)}
    fiscal = [entry for entry in state['journal'] if entry['kind'] == 'fiscal']
    if TREASURY in book:
        receipts = sum(entry['details']['tax_collected_minor'] for entry in fiscal)
        bank_disclosures = [e for e in state['journal'] if e['kind'] == 'bank'
                            and 'disclosure_by_recipient_minor' in e['details']]
        bank_paid = 0
        for entry in bank_disclosures:
            details = entry['details']
            allocations = details['disclosure_by_recipient_minor']
            paid = details['disclosure_paid_minor']
            expected = {(TREASURY, 'disclosure_expense', 'debit'): paid, (TREASURY, 'cash', 'credit'): paid,
                        (OPERATIONS, 'cash', 'debit'): paid, (OPERATIONS, 'disclosure_income', 'credit'): paid} if paid else {}
            actual = {(p['entity_id'], p['account_code'], p['side']): p['amount_minor'] for p in entry['postings']}
            if (sum(allocations.values()) != paid or set(allocations) != set(details['reached'])
                    or actual != expected or len(actual) != len(entry['postings'])):
                raise AssertionError('Bank disclosure costs, posting legs and actual recipients do not reconcile')
            bank_paid += paid
        program = sum(entry['details']['disclosure_paid_minor'] + entry['details']['transfers_paid_minor']
                      for entry in fiscal) + bank_paid
        treasury = book[TREASURY]
        support = _audit_treasury_support(state)
        if state['initial_cash'][TREASURY] + receipts - program - support.get('treasury_net_cash_outlay_minor', 0) != treasury['cash']:
            raise AssertionError('Treasury cash and realized costs did not reconcile')
        if treasury.get('tax_receivable', 0) != -sum(b.get('tax_payable', 0) for b in book.values()):
            raise AssertionError('Tax assessment mirror failed')
        result['fiscal'] = {'collected_minor': receipts, 'program_cash_outlay_minor': program,
                            'cash_remaining_minor': treasury['cash'],
                            **({'bank_disclosure_paid_minor': bank_paid} if bank_disclosures else {})}
        if support:
            result['treasury_support'] = support
    if ISSUER in book:
        balances = book.values()
        reserves = sum(b.get('bank_reserve', 0) for b in balances)
        if reserves != -book[ISSUER].get('reserve_payable', 0) or reserves != book[ISSUER]['cash']:
            raise AssertionError('Reserve backing or issuer mirror failed')
        for asset, liability in (('deposit_asset', 'deposit_payable'), ('loan_asset', 'loan_payable'),
                                 ('interest_asset', 'interest_payable')):
            if sum(b.get(asset, 0) + b.get(liability, 0) for b in balances):
                raise AssertionError(f'Unmirrored aggregate {asset}')
        closed = state['bank_positions']
        for lender, accounts in closed['deposits'].items():
            if sum(accounts.values()) != -book[lender].get('deposit_payable', 0):
                raise AssertionError('Unmirrored lender deposits')
        bank_equity = {}
        for bank, row in book.items():
            if not bank.startswith('bank:'):
                continue
            assets = sum(row.get(k, 0) for k in (
                'cash', 'bank_reserve', 'deposit_asset', 'loan_asset', 'interest_asset', 'loan_allowance'))
            liabilities = -sum(row.get(k, 0) for k in ('deposit_payable', 'loan_payable', 'interest_payable'))
            equity = -sum(row.get(k, 0) for k in (
                'opening_equity', 'interest_income', 'credit_recovery', 'payment_income', 'debt_release_income',
                'interest_expense', 'credit_loss', 'payment_expense', 'deposit_loss'))
            if assets != liabilities + equity:
                raise AssertionError('Bank assets, obligations and equity did not reconcile independently')
            bank_equity[bank] = equity
        result['banking'] = {'reserves_minor': reserves, 'bank_equity_minor': bank_equity, 'deposits_minor': sum(
            b.get('deposit_asset', 0) for b in balances), 'gross_principal_minor': sum(
            b.get('loan_asset', 0) for b in balances), 'allowances_minor': sum(
            -b.get('loan_allowance', 0) for b in balances)}
    return result


async def _run(config, root, *, checkpoint_tick=None):
    try:
        await economic_run.run_new(config, root, until=checkpoint_tick)
        if checkpoint_tick is not None:
            await economic_run.resume_run(root)
        world, store, _ = economic_run.open_run(root)
        periods = [{'tick':marker.tick, 'state_hash':marker.state_hash, **audit_institutions(snapshot)}
                   for marker, snapshot in store.states() if marker.tick]
        replay = await economic_run.replay_run(root, destination=root.parent / (root.name + '-replay'))
        if replay['status'] != 'matched':
            raise AssertionError('Independent replay disagrees')
    except Exception as exc:
        return None, {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}', 'path': str(root)}
    return world, {'status': 'completed', 'path': str(root), 'config_hash': store.identity.config_hash,
                   'periods': periods, 'replay': replay}


def _bank_outcomes(world, first, F, S):
    entries = [e for e in world.scenario.economy.journal if e.kind == 'bank']
    paid = sum(e.details.get('paid_minor', 0) for e in entries if e.payload['action'] == 'queue')
    losses = sum(sum(e.details.get('losses_minor', {}).values()) for e in entries)
    gaps = sum(e.details['uncovered_due_minor'] for e in entries
               if e.payload['action'] == 'due' and e.details['interbank'])
    notice_entry = next(e for e in entries if e.payload['action'] == 'notice')
    notice = notice_entry.details
    reaches = notice.get('reached', [])
    notices = [e.details for e in entries if e.payload['action'] == 'reaction']
    banking = world.scenario.economy.banking_report()
    losses_denominator = sum(sum(claims.values()) for claims in first['deposit_claims'].values())
    applied_financial = ('transmission' if F else 'fully_covered'
                         if not gaps and world.scenario.config.banking.guarantee_budget_minor >= banking['guarantee_bound_minor']
                         else 'attenuated' if banking['guarantee_spent_minor'] else 'uncovered')
    return {'financial_requested': F, 'social_requested': S, 'financial_realized': applied_financial,
            'root_tx_id': notice.get('source_tx_id'), 'notice_content': notice.get('content'),
            'social_response_mode': 'scripted_boundary_rule',
            'notice_tx_id': notice_entry.tx_id,
            'reaction_receipts': [{'tx_id': e.tx_id, 'actor_id': e.payload['reference'],
                                   'requested_minor': e.details.get('requested_minor', 0),
                                   'status': e.details['status']}
                                  for e in entries if e.payload['action'] == 'reaction'],
            'reaction_payments': [{'tx_id': e.tx_id, 'request_id': e.payload['reference'],
                                  'paid_minor': e.details.get('paid_minor', 0)}
                                 for e in entries if e.payload['action'] == 'queue'
                                 and e.payload['reference'].startswith('notice:')],
            'reached': reaches, 'social_withdrawal_requested_minor': sum(e.get('requested_minor', 0) for e in notices),
            'cash_withdrawn_minor': paid, 'actual_interbank_due_gap_minor': gaps,
            'terminal_deposit_loss_minor': losses, 'pre_shock_initial_deposits_minor': losses_denominator,
            'deposit_loss_rate': losses / losses_denominator if losses_denominator else None,
            'terminal_queued_minor': sum(x['queued_minor'] for x in banking['banks'].values()),
            'terminal_failed_banks': sum(x['resolution'] != 'open' for x in banking['banks'].values()),
            'guarantee_outlay_minor': banking['guarantee_spent_minor'],
            'loan_statuses': {key: value['status'] for key, value in banking['loans'].items()},
            'initial_balance_hash': content_hash(first)}


async def compare_bank_channels(base, destination, *, checkpoint_tick=None, run_world=_run):
    if (base.banking.run_notice is None or base.banking.run_notice.response_mode != 'scripted'
            or not base.banking.guarantee_budget_minor or base.banking.treasury_support is not None):
        raise ValueError('A B channel comparison needs a scripted notice plan and a funded guarantee')
    rows = {}
    for financial in (0, 1):
        for social in (0, 1):
            arm = f'F{financial}S{social}'
            case = base.model_dump()
            case['banking']['guarantee_interbank'] = not financial
            case['banking']['run_notice']['react_to_verified_delay'] = bool(social)
            path = destination / arm
            world, record = await run_world(case, path, checkpoint_tick=checkpoint_tick)
            if world is not None:
                opening = build_closed_economy(world.scenario.config)
                _, snapshot = CommitStore(path).load(1)
                opening.restore_snapshot(snapshot)
                record.update(_bank_outcomes(world, opening.scenario.economy.banking_report(), financial, social))
            rows[arm] = record
    complete = all(row['status'] == 'completed' for row in rows.values())
    gate = False
    if complete:
        gate = (all(rows[f'F0S{s}']['financial_realized'] == 'fully_covered' for s in (0, 1))
                and len({r['initial_balance_hash'] for r in rows.values()}) == 1
                and len({(tuple(r['reached']), r['root_tx_id']) for r in rows.values()}) == 1
                and all(rows[f'F{f}S1']['social_withdrawal_requested_minor'] > rows[f'F{f}S0']['social_withdrawal_requested_minor']
                        for f in (0, 1))
                and all(rows[f'F1S{s}']['actual_interbank_due_gap_minor'] > 0 for s in (0, 1)))
    loss = {key: r.get('terminal_deposit_loss_minor') for key, r in rows.items()}
    return {'design': 'B_finance_by_social', 'seed': base.root_seed, 'rows': rows,
            'assigned_worlds': 4, 'completed_worlds': sum(r['status'] == 'completed' for r in rows.values()),
            'manipulation_passed': gate,
            'deposit_loss_interaction_minor': loss['F1S1'] - loss['F1S0'] - loss['F0S1'] + loss['F0S0'] if gate else None}


def _notice_outcomes(world, store, _record):
    spec = world.scenario.config.banking.run_notice
    source = next(op for op in world.scenario.config.banking.operations if op.operation_id == spec.source_request)
    _, opening = store.load(source.tick - 1)
    book = opening.data['economic_runtime']
    entries = world.scenario.economy.journal
    snapshot = world.snapshot
    exposures = [{'tick': marker.tick, 'actor_id': obs.actor_id, 'observation_id': obs.observation_id,
                  'fact_id': fact.fact_id, **fact.value}
                 for marker in store.markers() for obs in store.observation_rows(marker)
                 for fact in obs.market_view if fact.fact_id.startswith('bank-notice:')
                 and fact.visible_at.tick == marker.tick]
    receipts = {r.command_id: r for r in snapshot.receipts if r.unit == 'cash_minor'}
    requests = [{'decision_id': d.decision_id, 'observation_id': d.observation_id,
                 'evidence_used': intent.notice_fact_id in d.evidence_ids,
                 'notice_fact_id': intent.notice_fact_id, 'requested_minor': intent.amount_minor,
                 'receipt': receipts['command:' + intent.intent_id].model_dump()}
                for d in snapshot.decisions for intent in d.economic_intents if isinstance(intent, WithdrawDepositIntent)]
    payments = [{'tick': e.tick, 'tx_id': e.tx_id, 'request_id': e.payload['reference'],
                 **{key: e.details[key] for key in ('status', 'outstanding_minor')},
                 'paid_minor': e.details.get('paid_minor', 0)}
                for e in entries if e.kind == 'bank' and e.payload['action'] == 'queue'
                and e.payload['reference'].startswith('notice:')]
    funded = next((e for e in entries if e.kind == 'bank' and 'disclosure_by_recipient_minor' in e.details), None)
    return {'exposures': exposures, 'requests': requests, 'queue_service': payments,
            'initial_bank_state_hash': content_hash([book['balances'], book['bank_positions']]),
            'initial_deposits_minor': sum(sum(rows.values()) for rows in book['bank_positions']['deposits'].values()),
            'requested_minor': sum(r['requested_minor'] for r in requests),
            'queued_minor': sum(r['receipt']['accepted_quantity'] for r in requests),
            'paid_minor': sum(r['paid_minor'] for r in payments),
            'terminal_deposit_loss_minor': sum(sum(e.details.get('losses_minor', {}).values()) for e in entries),
            **({'disclosure': {'tx_id': funded.tx_id, **funded.details,
                                'unspent_authority_minor': funded.details['budget_cap_minor']
                                - funded.details['disclosure_paid_minor']},
                'treasury_cash_remaining_minor': world.scenario.economy.account_view(TREASURY).cash_minor}
               if funded is not None else {})}


def _freeze_arms(destination, design, configs, **protocol):
    plan = {'design': design, 'implementation': economic_run.current_implementation(), 'arms': configs}
    plan.update(protocol)
    plan_hash = content_hash(plan)
    atomic_json(destination / 'plan.json', {'plan_hash': plan_hash, 'plan': plan})
    return plan_hash


async def _run_assigned_arms(destination, configs, *, checkpoint_tick, outcomes):
    rows = {}
    for arm, config in configs.items():
        path = destination / arm
        world, record = await _run(config, path, checkpoint_tick=checkpoint_tick)
        if world is not None:
            try:
                record.update(outcomes(world, CommitStore(path), record))
            except (OSError, ValueError, KeyError, AssertionError) as exc:
                record.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        rows[arm] = record
    return rows


async def _run_frozen_arms(destination, design, configs, *, checkpoint_tick, outcomes):
    plan_hash = _freeze_arms(destination, design, configs)
    rows = await _run_assigned_arms(destination, configs, checkpoint_tick=checkpoint_tick, outcomes=outcomes)
    return plan_hash, rows


def _requests_cite_exposures(row):
    exposed = {(e['observation_id'], e['fact_id']) for e in row['exposures']}
    return all((r['observation_id'], r['notice_fact_id']) in exposed and r['evidence_used'] for r in row['requests'])


async def compare_bank_notice_timing(base, destination):
    notice = base.banking.run_notice
    if (not base.archive_observations or not notice.disclose or not notice.react_to_verified_delay
            or notice.tick + 1 >= base.ticks or 'withdraw_deposit' in base.disabled_actions):
        raise ValueError('Timing comparison requires archived observations and a service period after both disclosures')
    design = 'B_notice_actor_timing.v1'
    configs = {}
    for arm, tick in (('early', notice.tick), ('delayed', notice.tick + 1), ('withheld', notice.tick)):
        config = base.model_dump()
        config['banking']['run_notice'].update(tick=tick)
        if arm == 'withheld':
            config['banking']['run_notice']['disclose'] = False
        configs[arm] = economic_run.parse_config(config).model_dump(mode='json')
    plan_hash, rows = await _run_frozen_arms(destination, design, configs, checkpoint_tick=notice.tick - 1,
                                            outcomes=_notice_outcomes)
    complete = sum(row['status'] == 'completed' for row in rows.values())
    gate = False
    if complete == len(configs):
        early, delayed, withheld = (rows[arm] for arm in ('early', 'delayed', 'withheld'))
        actual = [[(e['actor_id'], e['source_tx_id'], e['kind'], e['bank'], e['withdrawal_limit_minor'])
                   for e in row['exposures']] for row in (early, delayed)]
        gate = (len({r['initial_bank_state_hash'] for r in rows.values()}) == 1
                and actual[0] == actual[1]
                and sorted(e[0] for e in actual[0]) == sorted(notice.withdrawal_by_recipient)
                and all(e[2] == 'verified_withdrawal_delay' for e in actual[0])
                and all(e['tick'] == notice.tick for e in early['exposures'])
                and all(e['tick'] == notice.tick + 1 for e in delayed['exposures'])
                and not withheld['exposures'] and not withheld['requests']
                and all(_requests_cite_exposures(row) for row in (early, delayed)))
    return {'design': design, 'plan_hash': plan_hash, 'seed': base.root_seed, 'rows': rows,
            'assigned_worlds': len(configs), 'completed_worlds': complete, 'manipulation_passed': gate,
            'early_minus_delayed_requested_minor': early['requested_minor'] - delayed['requested_minor'] if gate else None,
            'early_minus_delayed_paid_minor': early['paid_minor'] - delayed['paid_minor'] if gate else None}


async def compare_funded_bank_disclosure(base, destination):
    notice = base.banking.run_notice
    funding = notice.disclosure_funding
    cost = funding.cost_per_recipient_minor
    intended = sorted(notice.withdrawal_by_recipient)
    if (not base.archive_observations or not notice.disclose or not notice.react_to_verified_delay
            or len(intended) < 2 or funding.budget_cap_minor < cost * len(intended)
            or funding.authorized_through_tick < notice.tick or 'withdraw_deposit' in base.disabled_actions):
        raise ValueError('Coverage comparison needs an active full-budget arm, multiple recipients and archived observations')
    design = 'B_funded_notice_coverage.v1'
    configs = {}
    for arm, changes in (
            ('zero_budget', {'budget_cap_minor': 0}), ('partial_budget', {'budget_cap_minor': cost}),
            ('full_budget', {}), ('expired', {'authorized_through_tick': notice.tick - 1})):
        config = base.model_dump()
        config['banking']['run_notice']['disclosure_funding'].update(changes)
        configs[arm] = economic_run.parse_config(config).model_dump(mode='json')
    plan_hash, rows = await _run_frozen_arms(destination, design, configs, checkpoint_tick=notice.tick - 1,
                                            outcomes=_notice_outcomes)
    completed = sum(r['status'] == 'completed' for r in rows.values())
    gate = False
    if completed == len(configs):
        expected = {'zero_budget': ('unfunded', []), 'partial_budget': ('disclosed', intended[:1]),
                    'full_budget': ('disclosed', intended), 'expired': ('authorization_expired', [])}
        root = rows['full_budget']['disclosure'].get('source_tx_id')
        gate = bool(root) and len({r['initial_bank_state_hash'] for r in rows.values()}) == 1
        for arm, (status, reached) in expected.items():
            row = rows[arm]
            disclosure = row['disclosure']
            gate &= (disclosure['status'] == status and disclosure['intended'] == intended
                     and disclosure['disclosure_by_recipient_minor'] == dict.fromkeys(reached, cost)
                     and disclosure.get('source_tx_id') == (None if arm == 'expired' else root)
                     and sorted(e['actor_id'] for e in row['exposures']) == reached
                     and all(e['tick'] == notice.tick and e['kind'] == 'verified_withdrawal_delay'
                             and e['source_tx_id'] == root for e in row['exposures'])
                     and _requests_cite_exposures(row))
    return {'design': design, 'plan_hash': plan_hash, 'seed': base.root_seed, 'rows': rows,
            'assigned_worlds': len(configs), 'completed_worlds': completed, 'manipulation_passed': gate}


def _support_outcomes(world, store, record, terms, *, withdrawal_bank=None):
    economy = world.scenario.economy
    entries = [e for e in economy.journal if e.kind == 'bank']
    withdrawal_bank = withdrawal_bank or terms.bank_id
    withdrawals = {key: row for key, row in economy.banking_report()['withdrawals'].items()
                   if row['terms']['bank'] == withdrawal_bank}
    payments = [e for e in entries if e.payload['action'] == 'queue' and e.payload['reference'] in withdrawals]
    _, before = store.load(terms.disbursement_tick - 1)
    prefix = before.model_dump(mode='json')
    opening = prefix['data']['economic_runtime']
    # Each arm embeds a different frozen policy identity in the participant envelope.
    opening.pop('institution_config_hash')
    zero = dict.fromkeys(('disbursed_minor', 'bank_repaid_minor', 'guarantee_paid_minor', 'outstanding_minor',
                          'allowance_minor', 'treasury_net_cash_outlay_minor', 'public_net_cash_outlay_minor'), 0)
    maturity = record['periods'][terms.maturity_tick - 1]
    final = record['periods'][-1]
    return {'bank_id': terms.bank_id,
            **({'withdrawal_bank_id': withdrawal_bank} if withdrawal_bank != terms.bank_id else {}),
            'pre_support_state_hash': content_hash([opening['balances'], opening['bank_positions']]),
            'pre_policy_state_hash': content_hash(prefix),
            'withdrawals_paid_by_support_tick_minor': sum(e.details.get('paid_minor', 0) for e in payments
                                                         if e.tick <= terms.disbursement_tick),
            'withdrawals_paid_by_end_minor': sum(e.details.get('paid_minor', 0) for e in payments),
            'withdrawal_remaining_minor': sum(row['remaining_minor'] for row in withdrawals.values()),
            'terminal_deposit_loss_minor': sum(sum(e.details.get('losses_minor', {}).values())
                                               for e in entries if e.details.get('bank') == terms.bank_id),
            'support_events': [{'tick': e.tick, 'tx_id': e.tx_id, **e.details} for e in entries
                               if e.payload['action'] in ('support_disburse', 'support_collect')],
            'at_maturity': maturity.get('treasury_support', zero),
            'treasury_support': final.get('treasury_support', zero),
            'cash_remaining_minor': final['fiscal']['cash_remaining_minor']}


async def compare_treasury_support(base, destination):
    terms = base.banking.treasury_support
    design = 'B_treasury_advance.v1'
    configs = {}
    arms = ('control', 'unguaranteed', 'guaranteed') if terms.guarantee_limit_minor else ('control', 'unguaranteed')
    for arm in arms:
        config = base.model_dump(mode='json')
        if arm == 'control':
            config['banking'].pop('treasury_support')
        elif arm == 'unguaranteed':
            config['banking']['treasury_support']['guarantee_limit_minor'] = 0
        configs[arm] = economic_run.parse_config(config).model_dump(mode='json')
    plan_hash, rows = await _run_frozen_arms(destination, design, configs, checkpoint_tick=terms.disbursement_tick - 1,
                                            outcomes=lambda world, store, record: _support_outcomes(world, store, record, terms))
    completed = sum(r['status'] == 'completed' for r in rows.values())
    gate = (completed == len(arms) and len({r['pre_support_state_hash'] for r in rows.values()}) == 1
            and all(rows[arm]['treasury_support']['disbursed_minor'] == (0 if arm == 'control' else terms.amount_minor)
                    for arm in arms))
    early = {arm: r['withdrawals_paid_by_support_tick_minor'] for arm, r in rows.items()} if gate else {}
    return {'design': design, 'plan_hash': plan_hash, 'seed': base.root_seed, 'rows': rows,
            'assigned_worlds': len(arms), 'completed_worlds': completed, 'manipulation_passed': gate,
            'early_withdrawal_difference_minor': {arm: early[arm] - early['control'] for arm in arms[1:]} if gate else None}


_BANK_POLICY_OUTCOMES = ('deposit_loss_rate', 'terminal_deposit_loss_minor', 'requested_minor', 'withdrawals_paid_by_end_minor',
                         'withdrawal_remaining_minor', 'public_net_cash_outlay_minor')


def _bank_policy_terms(base, *, fixed_dose_only=False):
    notice = base.banking.run_notice
    support = base.banking.treasury_support
    if notice is None or support is None or notice.disclosure_funding is None:
        raise ValueError('Bank policy combination needs funded disclosure and authorized support')
    funding = notice.disclosure_funding
    source = next(op for op in base.banking.operations if op.operation_id == notice.source_request)
    unit_cost = funding.cost_per_recipient_minor
    intended = dict.fromkeys(sorted(notice.withdrawal_by_recipient), unit_cost)
    disclosure_cost = sum(intended.values())
    budget = support.amount_minor
    mixed_principal = budget - disclosure_cost
    if (not base.archive_observations or not notice.disclose or not notice.react_to_verified_delay
            or 'withdraw_deposit' in base.disabled_actions
            or notice.tick != support.disbursement_tick or funding.authorized_through_tick < notice.tick
            or funding.budget_cap_minor < disclosure_cost):
        raise ValueError('Bank policy combination needs active policies on the same date and archived observations')
    if not fixed_dose_only and (source.bank != support.bank_id or mixed_principal <= 0
                               or support.guarantee_limit_minor > mixed_principal):
        raise ValueError('Matched budget needs policies at the same bank and funding after the split')

    return notice, support, source, intended, mixed_principal


def _bank_policy_arm(base, disclosure, principal):
    config = base.model_dump(mode='json')
    config['banking']['run_notice']['disclose'] = bool(disclosure)
    if principal:
        config['banking']['treasury_support']['amount_minor'] = principal
    else:
        config['banking'].pop('treasury_support')
    return economic_run.parse_config(config).model_dump(mode='json')


def _bank_policy_outcomes(world, store, record, support, source):
    row = {**_support_outcomes(world, store, record, support, withdrawal_bank=source.bank),
           **_notice_outcomes(world, store, record)}
    fee = record['periods'][-1]['fiscal'].get('bank_disclosure_paid_minor', 0)
    loan = row['treasury_support']
    banks = world.scenario.economy.banking_report()['banks']
    unresolved = sum(b['deposit_liabilities_minor'] for b in banks.values() if b['resolution'] == 'receivership')
    initial = row['initial_deposits_minor']
    row.update(actual_disclosure_paid_minor=fee,
               actual_gross_outlay_minor=fee + loan['disbursed_minor'],
               treasury_net_cash_outlay_minor=fee + loan['treasury_net_cash_outlay_minor'],
               public_net_cash_outlay_minor=fee + loan['public_net_cash_outlay_minor'],
               unresolved_receivership_deposits_minor=unresolved,
               deposit_loss_rate=row['terminal_deposit_loss_minor'] / initial if initial and not unresolved else None)
    return row


def _bank_policy_issues(row, fee, principal, notice, source, intended):
    verified_root = f'econ:bank:{source.tick}:queue:{source.operation_id}'
    issues = []
    if row['status'] != 'completed':
        issues.append('execution_failed')
    else:
        if row['treasury_support']['disbursed_minor'] != principal:
            issues.append('support_not_fully_disbursed')
        if row['actual_disclosure_paid_minor'] != fee:
            issues.append('disclosure_payment_mismatch')
        expected_recipients = list(intended) if fee else []
        actual_recipients = sorted(e['actor_id'] for e in row['exposures'])
        if (actual_recipients != expected_recipients
                or row.get('disclosure', {}).get('disclosure_by_recipient_minor', {}) != (intended if fee else {})
                or any(e['tick'] != notice.tick or e['source_tx_id'] != verified_root
                       or e['bank'] != source.bank or e['kind'] != 'verified_withdrawal_delay'
                       or e['withdrawal_limit_minor'] != notice.withdrawal_by_recipient[e['actor_id']]
                       for e in row['exposures'])
                or not _requests_cite_exposures(row)):
            issues.append('verified_disclosure_coverage_mismatch')
    return issues


async def compare_bank_policy_combination(base, destination):
    notice, support, source, intended, mixed_principal = _bank_policy_terms(base)
    disclosure_cost = sum(intended.values())
    budget = support.amount_minor

    # Each comparison is sealed before executing any arm.
    designs = {
        'fixed_component_dose': (
            {'D0S0': (0, 0), 'D0S1': (0, budget), 'D1S0': (disclosure_cost, 0), 'D1S1': (disclosure_cost, budget)},
            {'D0S0': 1, 'D0S1': -1, 'D1S0': -1, 'D1S1': 1}, 'interaction'),
        'matched_gross_budget': (
            {'support_only': (0, budget), 'mixed': (disclosure_cost, mixed_principal)},
            {'support_only': -1, 'mixed': 1}, 'difference'),
    }
    assignments, plan_hashes = {}, {}
    for group, (doses, weights, _) in designs.items():
        configs = {}
        expected = {}
        for arm, (fee, principal) in doses.items():
            configs[arm] = _bank_policy_arm(base, fee, principal)
            expected[arm] = {'disclosure_paid_minor': fee, 'support_disbursed_minor': principal}
        directory = destination / group
        directory.mkdir()
        plan_hashes[group] = _freeze_arms(directory, f'E_bank_policy.{group}.v1', configs,
            expected_doses_minor=expected, contrast_weights=weights, observation_unit='world',
            policy_tick=notice.tick, terminal_tick=base.ticks, primary_outcome=_BANK_POLICY_OUTCOMES[0],
            loss_denominator='aggregate deposits immediately before the source withdrawal period',
            loss_missing_if=['zero pre-shock deposits', 'deposit claims in unresolved receivership'],
            outcomes=list(_BANK_POLICY_OUTCOMES),
            **({'gross_budget_target_minor': budget} if group == 'matched_gross_budget' else {}))
        assignments[group] = configs
    design = 'E_bank_policy_combination.v1'
    plan = {'design': design, 'comparison_plan_hashes': plan_hashes, 'root_seed': base.root_seed}
    plan_hash = content_hash(plan)
    atomic_json(destination / 'plan.json', {'plan_hash': plan_hash, 'plan': plan})

    reports = {}
    for group, (doses, weights, label) in designs.items():
        rows = await _run_assigned_arms(destination / group, assignments[group], checkpoint_tick=notice.tick - 1,
                                        outcomes=lambda world, store, record: _bank_policy_outcomes(
                                            world, store, record, support, source))
        completed = sum(r['status'] == 'completed' for r in rows.values())
        for arm, (fee, principal) in doses.items():
            rows[arm]['manipulation_issues'] = _bank_policy_issues(
                rows[arm], fee, principal, notice, source, intended)
        matched_start = (completed == len(doses)
                         and len({r['pre_policy_state_hash'] for r in rows.values()}) == 1)
        valid = matched_start and all(not r['manipulation_issues'] for r in rows.values())
        reports[group] = {'plan_hash': plan_hashes[group], 'valid': valid, 'assigned_worlds': len(doses),
                          'completed_worlds': completed, 'matched_pre_policy_state': matched_start, 'rows': rows,
                          label: {key: sum(weights[a] * rows[a][key] for a in weights)
                                  if all(rows[a][key] is not None for a in weights) else None
                                  for key in _BANK_POLICY_OUTCOMES} if valid else None}
        atomic_json(destination / group / 'comparison.json', reports[group])
    return {'design': design, 'plan_hash': plan_hash, 'seed': base.root_seed, **reports,
            'assigned_worlds': sum(r['assigned_worlds'] for r in reports.values()),
            'completed_worlds': sum(r['completed_worlds'] for r in reports.values()),
            'manipulation_passed': all(r['valid'] for r in reports.values())}


async def compare_fiscal_policies(base, destination, *, checkpoint_tick=None, run_world=_run):
    policy = base.fiscal
    recipient_periods = len(policy.transfer_recipients) * (base.ticks - policy.effective_tick + 1)
    disclosure = len(policy.audience) * policy.disclosure_cost_minor
    if not recipient_periods or disclosure <= 0 or disclosure % recipient_periods or (
            policy.transfer_minor <= disclosure // recipient_periods):
        raise ValueError('E paired design needs positive valid doses with an integer budget-matched split')
    rows = {}
    for name, expose, transfer in [('D0T0', False, 0), ('D1T0', True, 0),
                                   ('D0T1', False, policy.transfer_minor), ('D1T1', True, policy.transfer_minor),
                                   ('matched_mix', True, policy.transfer_minor - disclosure // recipient_periods)]:
        case = base.model_dump()
        case['fiscal'].update(audience=policy.audience if expose else [], transfer_minor=transfer)
        world, record = await run_world(case, destination / name, checkpoint_tick=checkpoint_tick)
        if world is not None:
            metrics = world.scenario.metrics(world._context(world.snapshot))
            population = world.snapshot.data['closed_economy']['population']
            shortfall = sum(max(0, population[a]['target_units'] - sum(r.executed_quantity for r in world.snapshot.receipts
                            if r.tick == tick and r.actor_id == a and r.outcome == 'consumed'))
                            for tick in range(policy.effective_tick, base.ticks + 1) for a in population)
            record.update(fiscal=metrics['fiscal'], necessary_consumption_gap_units=shortfall,
                          realized_work_events=len(world.snapshot.data['closed_economy']['labor_events']),
                          actual_program_cost_minor=record['periods'][-1]['fiscal']['program_cash_outlay_minor'])
        rows[name] = record
    costs = {key: row.get('actual_program_cost_minor') for key, row in rows.items()}
    expected = recipient_periods * policy.transfer_minor
    fixed_dose = all(costs[k] == value for k, value in {
        'D0T0': 0, 'D1T0': disclosure, 'D0T1': expected, 'D1T1': expected + disclosure}.items())
    budget_match = costs['D0T1'] == costs['matched_mix'] == expected
    gaps = {key: row.get('necessary_consumption_gap_units') for key, row in rows.items()}
    return {'design': 'E_fiscal', 'seed': base.root_seed, 'rows': rows, 'assigned_worlds': 5,
            'completed_worlds': sum(r['status'] == 'completed' for r in rows.values()),
            'fixed_dose': {'valid': fixed_dose, 'arms': ['D0T0', 'D1T0', 'D0T1', 'D1T1'],
                           'gap_interaction_units': gaps['D1T1'] - gaps['D1T0'] - gaps['D0T1'] + gaps['D0T0']
                           if fixed_dose else None},
            'same_gross_budget': {'valid': budget_match, 'arms': ['D0T1', 'matched_mix'], 'planned_minor': expected,
                                  'gap_difference_units': gaps['matched_mix'] - gaps['D0T1'] if budget_match else None}}


async def compare_institutions(config, destination, *, bank_policy_combination=False):
    base = economic_run.parse_config(config)
    notice = base.banking.run_notice if base.banking is not None else None
    funded = notice is not None and notice.disclosure_funding is not None
    support = base.banking is not None and base.banking.treasury_support is not None
    if bank_policy_combination and not (funded and support):
        raise ValueError('Bank policy combination requires funded disclosure and Treasury support')
    if (not funded and not support and bool(base.banking) == bool(base.fiscal)
            or base.stopping_hazard_bps or base.cognition_mode != 'rule'):
        raise ValueError('A fixed-horizon comparison needs rule decisions and one supported institutional design')
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    comparison = compare_bank_channels if base.banking is not None else compare_fiscal_policies
    if bank_policy_combination:
        comparison = compare_bank_policy_combination
    elif support:
        comparison = compare_treasury_support
    elif funded:
        comparison = compare_funded_bank_disclosure
    elif notice is not None and notice.response_mode == 'actor':
        comparison = compare_bank_notice_timing
    report = await comparison(base, destination)
    report['status'] = ('completed' if report['assigned_worlds'] == report['completed_worlds'] else 'failed')
    report.update(code_hash=economic_run.current_implementation()['source_hash'], data_origin='synthetic')
    atomic_json(destination / 'comparison.json', report)
    return report
