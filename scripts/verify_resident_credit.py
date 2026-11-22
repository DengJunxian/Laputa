"""Audit the local five-period resident credit comparison from committed observations and postings."""
import argparse
from pathlib import Path

from civitas.application.closed_economy import build_closed_economy
from civitas.application.economic_run import parse_config, verify_implementation
from civitas.contracts.base import content_hash
from civitas.storage.checkpoint import restore
from civitas.storage.commit_store import CommitStore, json_bytes, read_json


def principal_flow(entries, borrower, side):
    return sum(p.amount_minor for entry in entries for p in entry.postings
               if (p.entity_id, p.account_code, p.side) == (borrower, 'loan_payable', side))


def audit(config, rule, archive):
    common = parse_config(config)
    offer = common.banking.credit_offer
    seat = {'household_index': int(offer.actor.split(':')[1]), 'source': 'archived_response'}
    rows = {}
    observed = []
    for side, path in [('rule', rule), ('archive', archive)]:
        expected = common if side == 'rule' else parse_config({**config, 'credit_cognition': seat})
        store = CommitStore(path)
        verify_implementation(store)
        if (store.identity.config_hash != content_hash(expected) or store.markers()[-1].tick != common.ticks
                or store.identity.metadata.get('parent') or store.identity.metadata['external_api_calls'] != 0):
            raise ValueError('Credit arm differs from its registered configuration or endpoint')
        # The two arms share economic participants. All model evidence is in the committed segments.
        world = build_closed_economy(common)
        restore(world, store)
        book = world.scenario.economy
        marker, decision_state = store.load(offer.tick)
        observations = store.observation_rows(marker)
        observed.append(content_hash([o.model_dump(mode='json') for o in observations]))
        facts = [(o.actor_id, f) for o in observations for f in o.market_view if f.value.get('kind') == 'credit_offer']
        if (len(facts) != 1 or facts[0][0] != offer.actor or facts[0][1].owner_id != offer.actor
                or facts[0][1].value['amount_minor'] != offer.amount_minor or facts[0][1].value['bank'] != offer.bank):
            raise ValueError('Credit opportunity was not delivered at the registered terms to exactly its recipient')
        decision, = [d for d in decision_state.decisions if (d.tick, d.actor_id) == (offer.tick, offer.actor)]
        intents = [i for i in decision.economic_intents if i.kind == 'apply_credit']
        receipts = [r for r in decision_state.receipts
                    if r.decision_id == decision.decision_id and r.command_id in {'command:' + i.intent_id for i in intents}]
        applications = [r for r in receipts if r.outcome == 'credit_application_recorded']
        if (len(receipts) != len(intents) or any(i.offer_fact_id != facts[0][1].fact_id for i in intents)
                or any(r.cash_delta_minor or r.executed_quantity for r in applications)):
            raise ValueError('Credit application receipt does not match its decision')
        events = [e for e in book.journal if e.kind == 'bank' and e.payload['reference'] == offer.operation_id]
        funding = [e for e in events if e.payload['action'] == 'fund']
        if len(funding) != len(applications) or any(e.tick != offer.tick + 1 for e in funding):
            raise ValueError('Credit funding is missing or precedes its registered boundary')
        issued = principal_flow(funding, offer.actor, 'credit')
        repaid = principal_flow([e for e in events if e.payload['action'] == 'due'], offer.actor, 'debit')
        loans = book.banking_report()['loans']
        if (issued != sum(e.details.get('disbursed_minor', 0) for e in funding)
                or issued - repaid != loans.get(offer.operation_id, {}).get('principal_minor', 0)):
            raise ValueError('Disbursement, repayment and the outstanding claim do not reconcile')
        calls = store.archived_records()
        if side == 'archive':
            call, = calls
            request = call['request']
            view = request['observation']
            if (request['task'] != 'cd_credit' or view['actor_id'] != offer.actor
                    or view['market_view'] != [facts[0][1].model_dump(mode='json')]
                    or view['account_view'] or view['private_memory_refs'] or view['delivered_messages']
                    or call['attempt_id'] != decision.belief_patch['cd_credit']['attempt_id']):
                raise ValueError('Model attempt does not bind the single authorized offer')
            if call['state'] == 'success' and (call['parsed']['action'] == 'apply') != bool(intents):
                raise ValueError('Archived choice and executed application differ')
        elif calls:
            raise ValueError('Rule arm contains a model attempt')
        rows[side] = {'offered': 1, 'reached': len(facts), 'unreached': 1 - len(facts), 'waited': int(not intents),
            'applications': len(intents), 'application_rejections': len(receipts) - len(applications),
            'funding_refusals': sum(e.details['status'] == 'rejected' for e in funding),
            'application_nominal_minor': sum(r.accepted_quantity for r in applications),
            'application_paid_minor': sum(r.cash_delta_minor for r in applications),
            'funded_minor': issued, 'principal_repaid_minor': repaid, 'model_attempts': len(calls),
            'failed_attempts': sum(c['state'] == 'failed' for c in calls),
            'fallbacks': int(decision.belief_patch.get('cd_credit', {}).get('status') == 'rule_fallback'),
            'config_hash': store.identity.config_hash,
            'periods': [{'tick': m.tick, 'commit': m.commit_hash, 'state_hash': m.state_hash} for m in store.markers()]}
    if observed[0] != observed[1]:
        raise ValueError('The two arms did not decide from identical observations')
    return {'status': 'matched', 'common_config_hash': content_hash(common), 'observation_hash': observed[0],
            'source_hash': store.identity.code_hash, 'arms': rows,
            'archive_minus_rule_funded_minor': rows['archive']['funded_minor'] - rows['rule']['funded_minor']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'rule', 'archive'):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    print(json_bytes(audit(read_json(args.config), args.rule, args.archive)).decode(), end='')
