"""Shared cash, explicit two-sided postings and exact resource conservation."""
from types import MappingProxyType

from civitas.contracts.base import content_hash, require_unique
from civitas.contracts.portfolio import (AccountState, AssetSpec, FeeSchedule, LedgerTransaction, PortfolioAccountView,
    PortfolioLedgerState, PortfolioTrade, Posting, Reservation, ReserveCommand)
from .fixedpoint import checked, decimal_minor, fee, notional
from .reservations import ReservationStore


class LedgerReject(ValueError):
    """A legal command that cannot consume the requested resources."""


class PortfolioLedger:
    participant_id='portfolio_ledger'
    state_schema_version=3

    def __init__(self, assets, accounts, *, fees=None, cash_scale=1_000_000, purchase_lock_schedule=None):
        assets=[AssetSpec.model_validate(a) for a in assets]
        accounts=[AccountState.model_validate(a) for a in accounts]
        require_unique([a.asset_id for a in assets],'asset')
        require_unique([a.account_id for a in accounts],'account')
        currencies={a.quote_currency for a in assets}
        if not assets or len(currencies)!=1 or not accounts:
            raise ValueError('One quote currency and nonempty assets/accounts required')
        self.currency=next(iter(currencies))
        if self.currency in {a.asset_id for a in assets} or 'fees' in {a.account_id for a in accounts}:
            raise ValueError('Reserved resource/account identity')
        decimal_minor('0',cash_scale)
        self.cash_scale=cash_scale
        self.assets={a.asset_id:a for a in assets}
        # Paper schedules are frozen in the run config and reconstructed before state import.
        self.purchase_lock_schedule=None if purchase_lock_schedule is None else tuple(purchase_lock_schedule)
        if self.purchase_lock_schedule is not None:
            if (len(assets)!=1 or not self.purchase_lock_schedule or self.purchase_lock_schedule[0][0]!=1
                    or any(type(t) is not int or t<1 or type(flag) is not bool for t,flag in self.purchase_lock_schedule)
                    or sorted({t for t,_ in self.purchase_lock_schedule})!=[t for t,_ in self.purchase_lock_schedule]):
                raise ValueError('Purchase locks require an ordered single-asset schedule starting at tick 1')
        resources={self.currency,*self.assets}
        for a in accounts:
            if (set(a.balances)!=resources or not set(a.locked)<=self.assets.keys()
                    or not set(a.cost_minor)<=self.assets.keys() or any(a.locked.values())):
                raise ValueError('Initial accounts require every resource and no acquired-today locks')
        accounts.append(AccountState(account_id='fees',balances={r:0 for r in sorted(resources)}))
        self._initial={a.account_id:a for a in accounts}
        self._accounts=dict(self._initial)
        self._totals={r:checked(sum(a.balances[r] for a in accounts)) for r in sorted(resources)}
        self.fees=FeeSchedule.model_validate(fees or FeeSchedule())
        self.day=0
        self.reservations=ReservationStore()
        self._journal: dict[str,LedgerTransaction]={}
        self._trade_ids=set()
        self.assert_invariants()

    @property
    def accounts(self):
        return MappingProxyType(self._accounts)

    @property
    def journal(self):
        return tuple(self._journal.values())

    def locks_purchase(self, asset_id, tick):
        if self.purchase_lock_schedule is None:
            return self.assets[asset_id].settlement_rule=='T+1'
        return next(flag for start,flag in reversed(self.purchase_lock_schedule) if start<=tick)

    def available(self, account_id: str, resource_id: str) -> int:
        if account_id not in self._accounts:
            raise LedgerReject('unknown_account')
        if resource_id not in self._totals:
            raise LedgerReject('unknown_resource')
        a=self._accounts[account_id]
        return a.balances[resource_id]-a.locked.get(resource_id,0)-self.reservations.total(account_id,resource_id)

    def account_view(self, account_id: str, *, version=0) -> PortfolioAccountView:
        self.available(account_id,self.currency)
        return PortfolioAccountView(**self._accounts[account_id].model_dump(),cash_scale=self.cash_scale,version=version,
            reserved={r:self.reservations.total(account_id,r) for r in self._totals},
            available={r:self.available(account_id,r) for r in self._totals})

    def _required(self, command, quantity, fees=None):
        if command.side=='sell':
            return quantity
        value=notional(command.limit_price_ticks,self.assets[command.asset_id].price_tick_minor,quantity)
        return checked(value+fee(value,(fees or self.fees).commission))

    def reserve(self, command: ReserveCommand) -> Reservation:
        c=ReserveCommand.model_validate(command)
        existing=self.reservations.get(c.reservation_id)
        if existing:
            if existing.command!=c:
                raise ValueError('reservation_id payload conflict')
            return existing
        if c.account_id not in self._accounts or c.account_id=='fees':
            raise LedgerReject('unknown_account')
        if c.asset_id not in self.assets:
            raise LedgerReject('unknown_asset')
        if c.quantity_units==0:
            raise LedgerReject('zero_quantity')
        resource=self.currency if c.side=='buy' else c.asset_id
        amount=self._required(c,c.quantity_units)
        if amount>self.available(c.account_id,resource):
            raise LedgerReject('insufficient_cash' if c.side=='buy' else 'insufficient_sellable_inventory')
        reservation=Reservation(reservation_id=c.reservation_id,command=c,resource_id=resource,
                                amount=amount,remaining_units=c.quantity_units)
        self.reservations.replace(reservation)
        return reservation

    def release(self, reservation_id: str) -> int:
        r=self.reservations.get(reservation_id)
        if r is None:
            raise LedgerReject('unknown_reservation')
        self.reservations.replace(r.model_copy(update={'amount':0,'remaining_units':0}))
        return r.amount

    def transaction(self, trade: PortfolioTrade, *, tx_id=None) -> LedgerTransaction:
        t=PortfolioTrade.model_validate(trade)
        if t.asset_id not in self.assets:
            raise LedgerReject('unknown_asset')
        value=notional(t.price_ticks,self.assets[t.asset_id].price_tick_minor,t.quantity_units)
        buy=sell=fee(value,self.fees.commission)
        tax=fee(value,self.fees.tax)
        tx_id=tx_id or f'tx:{t.trade_id}'
        amounts=[(t.buyer_id,self.currency,-checked(value+buy)),
                 (t.seller_id,self.currency,value-sell-tax),('fees',self.currency,checked(buy+sell+tax)),
                 (t.buyer_id,t.asset_id,t.quantity_units),(t.seller_id,t.asset_id,-t.quantity_units)]
        return LedgerTransaction(tx_id=tx_id,trade=t,day=self.day,fees=self.fees,notional_minor=value,
            buyer_fee_minor=buy,seller_fee_minor=sell,tax_minor=tax,
            postings=[Posting(posting_id=f'{tx_id}:{i}',tx_id=tx_id,account_id=a,resource_id=r,amount=n)
                      for i,(a,r,n) in enumerate(amounts)])

    def settle(self, transaction: LedgerTransaction) -> LedgerTransaction:
        tx=LedgerTransaction.model_validate(transaction)
        existing=self._journal.get(tx.tx_id)
        if existing:
            if tx!=existing:
                raise ValueError('tx_id payload conflict')
            return existing
        t=tx.trade
        if t.trade_id in self._trade_ids:
            raise ValueError('trade_id already settled')
        if t.quantity_units==0:
            raise LedgerReject('zero_quantity')
        if t.buyer_id==t.seller_id:
            raise LedgerReject('self_trade')
        if any(a not in self._accounts or a=='fees' for a in (t.buyer_id,t.seller_id)):
            raise LedgerReject('unknown_account')
        if tx!=self.transaction(t,tx_id=tx.tx_id):
            raise ValueError('Transaction amount/fees/postings/day mismatch')
        replacements=[]
        for rid,actor,oid,side in ((t.buyer_reservation_id,t.buyer_id,t.buy_order_id,'buy'),
                                   (t.seller_reservation_id,t.seller_id,t.sell_order_id,'sell')):
            r=self.reservations.get(rid)
            if r is None or (r.command.account_id,r.command.asset_id,r.command.order_id,r.command.side)!=(actor,t.asset_id,oid,side):
                raise LedgerReject('reservation_identity_mismatch')
            if t.quantity_units>r.remaining_units:
                raise LedgerReject('reservation_quantity_exceeded')
            if ((side=='buy' and t.price_ticks>r.command.limit_price_ticks)
                    or (side=='sell' and t.price_ticks<r.command.limit_price_ticks)):
                raise LedgerReject('limit_price_exceeded')
            remaining=r.remaining_units-t.quantity_units
            replacements.append(r.model_copy(update={'remaining_units':remaining,'amount':self._required(r.command,remaining)}))
        updates={actor:self._accounts[actor].model_dump() for actor in (t.buyer_id,t.seller_id,'fees')}
        for p in tx.postings:
            updates[p.account_id]['balances'][p.resource_id]=checked(updates[p.account_id]['balances'][p.resource_id]+p.amount)
        buyer,seller=updates[t.buyer_id],updates[t.seller_id]
        buyer['cost_minor'][t.asset_id]=checked(buyer['cost_minor'].get(t.asset_id,0)+tx.notional_minor+tx.buyer_fee_minor)
        before=self._accounts[t.seller_id]
        seller['cost_minor'][t.asset_id]=(before.cost_minor.get(t.asset_id,0)*seller['balances'][t.asset_id]
                                         // before.balances[t.asset_id])
        if self.locks_purchase(t.asset_id,t.tick):
            buyer['locked'][t.asset_id]=checked(buyer['locked'].get(t.asset_id,0)+t.quantity_units)
        candidates={actor:AccountState.model_validate(raw) for actor,raw in updates.items()}
        for actor,a in candidates.items():
            for resource,balance in a.balances.items():
                reserved=self.reservations.total(actor,resource)
                for r in replacements:
                    if (r.command.account_id,r.resource_id)==(actor,resource):
                        reserved+=r.amount-self.reservations.get(r.reservation_id).amount
                if balance-a.locked.get(resource,0)-reserved<0:
                    raise LedgerReject('insufficient_reserved_resources')
        self._accounts.update(candidates)
        for r in replacements:
            self.reservations.replace(r)
        self._journal[tx.tx_id]=tx
        self._trade_ids.add(t.trade_id)
        self.assert_invariants()
        return tx

    def new_day(self, day: int) -> None:
        checked(day)
        if day!=self.day+1:
            raise ValueError('Day must advance exactly once')
        self._accounts={k:a.model_copy(update={'locked':{}}) for k,a in self._accounts.items()}
        self.day=day

    def change_fees(self, fees: FeeSchedule) -> list[str]:
        """Rebudget outstanding buys in acceptance order; cancel those no longer affordable."""
        fees=FeeSchedule.model_validate(fees)
        proposed=[]
        used={}
        cancelled=[]
        for r in self.reservations.records():
            amount=self._required(r.command,r.remaining_units,fees)
            key=(r.command.account_id,r.resource_id)
            capacity=self._accounts[key[0]].balances[key[1]]-self._accounts[key[0]].locked.get(key[1],0)
            if amount+used.get(key,0)>capacity:
                cancelled.append(r.command.order_id)
                proposed.append(r.model_copy(update={'remaining_units':0,'amount':0}))
            else:
                proposed.append(r.model_copy(update={'amount':amount}))
                used[key]=used.get(key,0)+amount
        self.fees=fees
        for r in proposed:
            self.reservations.replace(r)
        self.assert_invariants()
        return cancelled

    def assert_invariants(self) -> dict[str,int]:
        if any(set(a.balances)!=set(self._totals) or not set(a.locked)<=self.assets.keys()
               or not set(a.cost_minor)<=self.assets.keys() for a in self._accounts.values()):
            raise ValueError('Account resources mismatch')
        residual={r:sum(a.balances[r] for a in self._accounts.values())-total for r,total in self._totals.items()}
        if any(residual.values()):
            raise ValueError('Resource conservation failure')
        for actor,a in self._accounts.items():
            for resource in self._totals:
                if self.available(actor,resource)<0:
                    raise ValueError('Negative available balance / excess reservation')
            if any(a.cost_minor.get(asset,0) for asset in self.assets if a.balances[asset]==0):
                raise ValueError('Empty position must have zero cost')
        return residual

    def validate_state(self) -> None:
        self.assert_invariants()
        expected={k:a.model_dump() for k,a in self._initial.items()}
        if self.day>0:
            for account in expected.values():
                account['locked']={}
        fills={}
        previous_day=0
        for tx in self._journal.values():
            t=tx.trade
            if not previous_day<=tx.day<=self.day or t.quantity_units==0 or t.buyer_id==t.seller_id:
                raise ValueError('Invalid transaction time/quantity/parties')
            previous_day=tx.day
            for rid,actor,oid,side in ((t.buyer_reservation_id,t.buyer_id,t.buy_order_id,'buy'),
                                       (t.seller_reservation_id,t.seller_id,t.sell_order_id,'sell')):
                r=self.reservations.get(rid)
                if r is None or (r.command.account_id,r.command.order_id,r.command.side,r.command.asset_id)!=(actor,oid,side,t.asset_id):
                    raise ValueError('Journal/reservation identity mismatch')
                if (side=='buy' and t.price_ticks>r.command.limit_price_ticks) or (side=='sell' and t.price_ticks<r.command.limit_price_ticks):
                    raise ValueError('Journal limit price mismatch')
                fills[rid]=fills.get(rid,0)+t.quantity_units
            buyer,seller=expected[t.buyer_id],expected[t.seller_id]
            old_quantity=seller['balances'][t.asset_id]
            for p in tx.postings:
                expected[p.account_id]['balances'][p.resource_id]+=p.amount
            buyer['cost_minor'][t.asset_id]=buyer['cost_minor'].get(t.asset_id,0)+tx.notional_minor+tx.buyer_fee_minor
            if old_quantity<t.quantity_units:
                raise ValueError('Journal oversell')
            seller['cost_minor'][t.asset_id]=seller['cost_minor'].get(t.asset_id,0)*seller['balances'][t.asset_id]//old_quantity
            if tx.day==self.day and self.locks_purchase(t.asset_id,t.tick):
                buyer['locked'][t.asset_id]=buyer['locked'].get(t.asset_id,0)+t.quantity_units
        for actor,raw in expected.items():
            actual=self._accounts[actor]
            if (raw['balances']!=actual.balances or raw['cost_minor']!=actual.cost_minor
                    or raw['locked']!=actual.locked):
                raise ValueError('Account/lock/cost journal mismatch')
        for r in self.reservations.records():
            filled=fills.get(r.reservation_id,0)
            if filled>r.command.quantity_units or r.remaining_units not in (0,r.command.quantity_units-filled):
                raise ValueError('Reservation/journal quantity mismatch')
        totals={}
        for r in self.reservations.records():
            if r.amount!=self._required(r.command,r.remaining_units) or r.remaining_units>r.command.quantity_units:
                raise ValueError('Invalid reservation state')
            key=(r.command.account_id,r.resource_id)
            totals[key]=totals.get(key,0)+r.amount
        if totals!=self.reservations._totals:
            raise ValueError('Reservation totals mismatch')

    def export_state(self) -> dict:
        # Records are already immutable/validated; import validates the complete external envelope.
        return {'schema_version':3,'state_id':'portfolio_ledger','backend':'fixedpoint_multiasset',
            'cash_scale':self.cash_scale,'assets':[a.model_dump() for a in self.assets.values()],
            'initial_accounts':[a.model_dump() for a in self._initial.values()],
            'accounts':[a.model_dump() for a in self._accounts.values()],'day':self.day,'fees':self.fees.model_dump(),
            'reservations':[r.model_dump() for r in self.reservations.records()],
            'journal':[t.model_dump() for t in self._journal.values()]}

    def import_state(self, state: dict) -> None:
        state=PortfolioLedgerState.model_validate(state)
        require_unique([a.account_id for a in state.accounts],'account')
        require_unique([r.reservation_id for r in state.reservations],'reservation')
        require_unique([t.tx_id for t in state.journal],'transaction')
        require_unique([t.trade.trade_id for t in state.journal],'trade')
        candidate=PortfolioLedger(state.assets,[a for a in state.initial_accounts if a.account_id!='fees'],
                                  fees=state.fees,cash_scale=state.cash_scale,purchase_lock_schedule=self.purchase_lock_schedule)
        if list(candidate._initial.values())!=state.initial_accounts:
            raise ValueError('Initial fee account mismatch')
        candidate._accounts={a.account_id:a for a in state.accounts}
        if set(candidate._accounts)!=set(candidate._initial):
            raise ValueError('Account identity mismatch')
        candidate.day=state.day
        for r in state.reservations:
            if (r.reservation_id!=r.command.reservation_id or r.command.account_id not in candidate._accounts
                    or r.command.asset_id not in candidate.assets or r.resource_id!=(candidate.currency if r.command.side=='buy' else r.command.asset_id)):
                raise ValueError('Reservation identity mismatch')
            candidate.reservations.replace(r)
        candidate._journal={tx.tx_id:tx for tx in state.journal}
        candidate._trade_ids={tx.trade.trade_id for tx in state.journal}
        # Recompute recorded fees/postings using each historical schedule, never the current rate.
        current_fees=candidate.fees
        for tx in state.journal:
            candidate.fees=tx.fees
            day=candidate.day
            candidate.day=tx.day
            if tx.day>state.day or tx!=candidate.transaction(tx.trade,tx_id=tx.tx_id):
                raise ValueError('Invalid journal transaction')
            candidate.day=day
        candidate.fees=current_fees
        candidate.validate_state()
        if content_hash(candidate.export_state())!=content_hash(state):
            raise ValueError('Noncanonical ledger state')
        self.__dict__.update(candidate.__dict__)
