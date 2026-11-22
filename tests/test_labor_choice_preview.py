from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from civitas.application import economic_run
from civitas.contracts.economic import LaborApplyIntent
from civitas.experiments.labor_choice_preview import audit_labor_choice, run_labor_choice
from civitas.storage.commit_store import CommitStore, atomic_json, read_json

FIXTURE = Path(__file__).resolve().parents[1]/'examples/closed-economy-labor-choice-preview.json'
SEAT = 'household:0003'


class LaborChoicePreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_wait_changes_only_application_and_wage_contract(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)/'choice'
            result = await run_labor_choice(read_json(FIXTURE), 3, root)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['denominators']['paired_opportunities'], 1)
            rule, archive = (result['arms'][side] for side in ('rule','archive'))
            self.assertEqual((rule['hired'],archive['hired']), (1,0))
            self.assertEqual((rule['paid_contract_wages_minor'],archive['paid_contract_wages_minor']), (24,0))
            self.assertEqual((rule['worked_periods'],archive['worked_periods']), (2,0))
            self.assertEqual((rule['total_seat_wages_after_offer_minor'],archive['total_seat_wages_after_offer_minor']), (24,48))
            self.assertEqual((rule['filled_vacancy'],archive['filled_vacancy']), (1,1))
            self.assertEqual(archive['normal_waits'],1)
            states = [CommitStore(root/side).load(4)[1] for side in ('rule','archive')]
            decisions = [[d for d in state.decisions if d.tick==4 and d.actor_id!=SEAT] for state in states]
            self.assertEqual(*decisions)
            non_labor = [[i for d in state.decisions if d.tick==4 and d.actor_id==SEAT
                          for i in d.economic_intents if not isinstance(i,LaborApplyIntent)] for state in states]
            self.assertEqual(*non_labor)
            row, = CommitStore(root/'archive').archived_records()
            obs = row['request']['observation']
            self.assertEqual([f['owner_id'] for f in obs['market_view']],[SEAT])
            self.assertEqual(obs['market_view'][0]['value']['kind'],'labor_vacancy')
            self.assertFalse(any(obs[k] for k in ('account_view','private_memory_refs','delivered_messages')))
            for term in ('household:','firm:','Bearer ','"request"','"observation"','"response"'):
                self.assertNotIn(term,json.dumps(result))
            replay = await economic_run.replay_run(root/'archive', destination=root/'replay')
            self.assertEqual((replay['status'],replay['compared_ticks']),('matched',8))
            self.assertEqual(audit_labor_choice(root),result)
            result['arms']['rule']['paid_contract_wages_minor'] = 0
            atomic_json(root/'shareable-comparison.json',result)
            with self.assertRaisesRegex(ValueError,'independent audit'):
                audit_labor_choice(root)

    async def test_unreached_ineligible_fallback_and_failure_remain_distinct(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            config = read_json(FIXTURE)
            await run_labor_choice(config, 3, root/'original')
            base = read_json(root/'original'/'response-archive.json')
            for variant in ('ineligible','unreached','apply','refusal','invalid'):
                with self.subTest(variant=variant):
                    raw, tape = deepcopy(config), deepcopy(base)
                    record = tape['records'][0]['response']
                    body = json.loads(record['text'])
                    if variant == 'ineligible':
                        raw['labor_transfer']['additional_candidate_indices'] = [5]
                    elif variant == 'unreached':
                        raw['labor_transfer']['min_previous_sales_units'] = 99
                    elif variant == 'apply':
                        record['text'] = json.dumps({**body,'action':'apply'})
                    elif variant == 'refusal':
                        record.update(ok=False,text='',error_code='refusal')
                    else:
                        record['text'] = json.dumps({**body,'vacancy_id':'unrelated-vacancy'})
                    result = await run_labor_choice(raw, 3, root/variant, tape)
                    self.assertEqual(audit_labor_choice(root/variant),result)
                    counts, archive = result['denominators'], result['arms']['archive']
                    if variant == 'ineligible':
                        self.assertEqual((counts['ineligible_seats'],counts['eligible_unreached_seats'],archive['model_attempts']), (2,0,0))
                    elif variant == 'unreached':
                        self.assertEqual((counts['ineligible_seats'],counts['eligible_unreached_seats'],archive['model_attempts']), (0,2,0))
                    elif variant in ('apply','refusal'):
                        self.assertEqual((archive['attempted_applications'],archive['paid_contract_wages_minor']), (1,24))
                        self.assertEqual((archive['rule_fallbacks'],archive['failed_model_attempts']),
                                         (int(variant=='refusal'),int(variant=='refusal')))
                    else:
                        self.assertEqual((archive['failed_execution_attempts'],counts['unobserved_at_offer']), (1,1))
                        self.assertEqual(archive['commits'][-1]['tick'],3)
                        self.assertEqual(archive['model_attempts'],1)
                        self.assertEqual(archive['normal_waits'],0)

    async def test_rejected_application_does_not_create_contract_work_or_wage(self):
        with TemporaryDirectory() as folder:
            result = await run_labor_choice(read_json(FIXTURE), 1, Path(folder)/'pair')
            rule = result['arms']['rule']
            self.assertEqual((rule['eligible'],rule['reached'],rule['attempted_applications']), (1,1,1))
            self.assertEqual(rule['rejection_codes'], ['labor_vacancy_filled'])
            self.assertEqual((rule['hired'],rule['worked_periods'],rule['paid_contract_wages_minor']), (0,0,0))
