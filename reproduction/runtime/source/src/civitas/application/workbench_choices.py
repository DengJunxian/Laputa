"""Read-only workbench projection of audited CLI choice comparisons."""
from civitas.application.inspection import inspect_run
from civitas.contracts.base import content_hash
from civitas.experiments import labor_choice_preview, withdrawal_preview
from civitas.storage.commit_store import CommitStore, read_json

DESIGNS = {
    withdrawal_preview.DESIGN: ('withdrawal', '提款对照', 'notice_tick', 'withdrawal_cognition',
                                withdrawal_preview.audit_withdrawal_preview),
    labor_choice_preview.DESIGN: ('labor', '岗位对照', 'offer_tick', 'labor_cognition',
                                  labor_choice_preview.audit_labor_choice),
}


class ChoiceComparisons:
    def __init__(self, paths):
        self.pairs, self.arms = {}, {}
        for path in paths:
            root = path.resolve(strict=True)
            plan = read_json(root/'plan.json')['plan']
            if plan['design'] not in DESIGNS:
                raise ValueError('Choose a frozen withdrawal or labor comparison')
            kind, label, tick_field, settings, audit = DESIGNS[plan['design']]
            if kind in self.pairs:
                raise ValueError('Choose one archive per comparison type')
            if any((root/side).is_symlink() for side in ('rule','archive')):
                raise ValueError('Choice comparison run paths must be local directories')
            report = audit(root)
            seat = plan['arms']['archive'][settings]['household_index']
            keys, ticks = {}, set()
            for side, result in report['arms'].items():
                cursors = {row['tick']: row for row in result['commits']}
                ticks.update(cursors)
                key = content_hash([kind, report['plan_hash'], side, cursors[max(cursors)]])[:32]
                keys[side] = key
                self.arms[key] = {'path':root/side, 'actor':f'household:{seat:04d}', 'cursors':cursors,
                                  'label':label+' · '+('规则' if side=='rule' else '固定合成归档'),
                                  'terminal_tick':plan['final_tick']}
            self.pairs[kind] = {'descriptor':{'kind':kind,'label':label,'default_tick':plan[tick_field],
                                              'final_tick':plan['final_tick'],'periods':sorted(ticks)},
                                'report':report,'keys':keys}

    def pair(self, kind):
        if kind not in self.pairs:
            raise FileNotFoundError('Unknown frozen comparison')
        return self.pairs[kind]

    def arm(self, key):
        if key not in self.arms:
            raise FileNotFoundError('Unknown frozen comparison arm')
        return self.arms[key]

    def status(self, scope):
        if scope is None:
            return {'role':'researcher','choice_previews':[p['descriptor'] for p in self.pairs.values()],
                    'choice_archive':True}
        key, actor = scope
        arm = self.arm(key)
        head = max(arm['cursors'])
        return {'role':'observer','actor':actor,'choice_archive':True,'jobs':[{
            'id':key,'label':arm['label'],'status':'frozen','tick':head,'terminal_tick':arm['terminal_tick'],
            'commit_hash':arm['cursors'][head]['source_commit_hash']}]}

    def view(self, kind, tick):
        pair = self.pair(kind)
        if tick not in pair['descriptor']['periods']:
            raise ValueError('Select a committed comparison period')
        return {'kind':kind,'tick':tick,'report':pair['report']}

    def grant_scope(self, kind, side):
        pair = self.pair(kind)
        if side not in pair['keys']:
            raise ValueError('Choose the rule or archive arm')
        key = pair['keys'][side]
        return {'job':key,'actor':self.arm(key)['actor']}

    def inspect(self, key, tick, actor):
        arm = self.arm(key)
        if actor != arm['actor']:
            raise PermissionError('Only the registered actor observation can be opened')
        cursor = arm['cursors'].get(tick)
        if cursor is None:
            return {'status':'uncommitted','tick':tick}
        if tick == 0:
            return {'status':'unrecorded_observe','tick':tick}
        data = inspect_run(arm['path'], [tick], audience='agent', actor=actor)
        if data['views'][0]['cursor'] != cursor:
            raise ValueError('Observation cursor differs from the frozen comparison')
        return {'status':'available', **data}
