"""Integer venues emit trades; only the shared PortfolioLedger moves resources."""
from civitas.contracts.base import content_hash, require_unique
from civitas.contracts.financial import CancelIntent
from civitas.contracts.portfolio import (MatchingVenueState, MultiAssetState, PortfolioCommand, PortfolioExecutionReceipt,
    PortfolioOrder, PortfolioOrderEvent, PortfolioOrderIntent, PortfolioTrade, ReserveCommand)
from civitas.resources.fixedpoint import checked
from civitas.resources.ledger import LedgerReject, PortfolioLedger


class MatchingVenue:
    def __init__(self, asset_id, initial_price):
        self.asset_id=asset_id
        self.last_price_ticks=initial_price
        self.arrival_sequence=self.trade_count=0
        self.orders={}

    def plan(self, command: ReserveCommand, *, cancel_incoming_remainder=False):
        opposite=[o for o in self.orders.values() if o.status=='active' and o.command.side!=command.side
                  and (o.command.limit_price_ticks<=command.limit_price_ticks if command.side=='buy'
                       else o.command.limit_price_ticks>=command.limit_price_ticks)]
        opposite.sort(key=lambda o:((o.command.limit_price_ticks if command.side=='buy' else -o.command.limit_price_ticks),o.arrival_sequence))
        left=command.quantity_units
        plan=[]
        for maker in opposite:
            if not left:
                break
            if maker.command.account_id==command.account_id:
                if cancel_incoming_remainder:
                    break
                raise LedgerReject('self_trade')
            quantity=min(left,maker.remaining_units)
            plan.append((maker,quantity))
            left-=quantity
        return plan

    def match(self, command, decision_id, tick, plan):
        self.arrival_sequence+=1
        incoming=PortfolioOrder(command=command,decision_id=decision_id,accepted_tick=tick,
            arrival_sequence=self.arrival_sequence,remaining_units=command.quantity_units,status='active')
        trades=[]
        for maker,quantity in plan:
            self.trade_count+=1
            buy,sell=(command,maker.command) if command.side=='buy' else (maker.command,command)
            trade=PortfolioTrade(trade_id=f'{self.asset_id}-trade-{self.trade_count:08d}',asset_id=self.asset_id,
                buyer_id=buy.account_id,seller_id=sell.account_id,buy_order_id=buy.order_id,sell_order_id=sell.order_id,
                buyer_reservation_id=buy.reservation_id,seller_reservation_id=sell.reservation_id,
                maker_order_id=maker.command.order_id,taker_order_id=command.order_id,price_ticks=maker.command.limit_price_ticks,
                quantity_units=quantity,tick=tick)
            trades.append(trade)
            remaining=maker.remaining_units-quantity
            self.orders[maker.command.order_id]=maker.model_copy(update={'remaining_units':remaining,'status':'active' if remaining else 'filled'})
            remaining=incoming.remaining_units-quantity
            incoming=incoming.model_copy(update={'remaining_units':remaining,'status':'active' if remaining else 'filled'})
            self.last_price_ticks=trade.price_ticks
        self.orders[command.order_id]=incoming
        return trades

    def cancel(self, order_id):
        order=self.orders.get(order_id)
        if order is None or order.status!='active':
            return False
        self.orders[order_id]=order.model_copy(update={'status':'cancelled'})
        return True

    def quote(self):
        bids=[o.command.limit_price_ticks for o in self.orders.values() if o.status=='active' and o.command.side=='buy']
        asks=[o.command.limit_price_ticks for o in self.orders.values() if o.status=='active' and o.command.side=='sell']
        return {'last_price_ticks':self.last_price_ticks,'best_bid_ticks':max(bids) if bids else None,
                'best_ask_ticks':min(asks) if asks else None}

    def export_state(self):
        return {'schema_version':3,'asset_id':self.asset_id,'last_price_ticks':self.last_price_ticks,
            'arrival_sequence':self.arrival_sequence,'trade_count':self.trade_count,
            'orders':[o.model_dump() for o in self.orders.values()]}



class MultiAssetMarket:
    participant_id='multiasset_market'
    state_schema_version=3

    def __init__(self, ledger: PortfolioLedger, *, initial_prices: dict, fault_hook=None, cancel_incoming_remainder=False):
        if type(ledger) is not PortfolioLedger or set(initial_prices)!=set(ledger.assets):
            raise ValueError('Fixedpoint markets require one shared PortfolioLedger and all initial prices')
        self.ledger=ledger
        if type(cancel_incoming_remainder) is not bool:
            raise ValueError('Self-trade policy must be explicit boolean')
        self.cancel_incoming_remainder=cancel_incoming_remainder
        self.venues={asset:MatchingVenue(asset,price) for asset,price in initial_prices.items()}
        self.tick=self.world_version=0
        self.commands={}
        self.receipts={}
        self.events=[]
        self._order_ids=set()
        self.fault_hook=fault_hook
        self.diagnostics=[]
        self.validate_state()

    @property
    def trades(self):
        return tuple(tx.trade for tx in self.ledger.journal)

    def begin_tick(self, tick, world_version):
        checked(tick)
        checked(world_version)
        if tick!=self.tick+1 or world_version!=tick-1:
            raise ValueError('Invalid tick/world version')
        self.tick,self.world_version=tick,world_version

    def _event(self, order_id, event, remaining, reason=''):
        self.events.append(PortfolioOrderEvent(event_id=f'order-event:{len(self.events)}',order_id=order_id,tick=self.tick,
                                               event=event,remaining_units=remaining,reason=reason))

    def _fault(self, phase, value):
        if self.fault_hook:
            self.fault_hook(phase,value)

    def _find(self, order_id):
        for venue in self.venues.values():
            if order_id in venue.orders:
                return venue,venue.orders[order_id]
        return None,None

    def _cancel(self, order_id, *, actor_id=None, reason='manual'):
        venue,order=self._find(order_id)
        if order is None or order.status!='active' or (actor_id is not None and order.command.account_id!=actor_id):
            raise LedgerReject('not_active_or_not_owner')
        venue.cancel(order_id)
        self.ledger.release(order.command.reservation_id)
        self._event(order_id,'cancelled',order.remaining_units,reason)

    def expire(self, ttl):
        if type(ttl) is not int or ttl<1:
            raise ValueError('Positive TTL required')
        for venue in self.venues.values():
            for order in tuple(venue.orders.values()):
                if order.status=='active' and self.tick-order.accepted_tick>=ttl:
                    self._cancel(order.command.order_id,reason='time_to_live')

    def change_fees(self, fees):
        cancelled=self.ledger.change_fees(fees)
        for oid in cancelled:
            venue,order=self._find(oid)
            if order is not None and venue.cancel(oid):
                self._event(oid,'cancelled',order.remaining_units,'fee_reservation_insufficient')
        return cancelled

    def cancel_all(self, *, reason):
        for venue in self.venues.values():
            for order in tuple(venue.orders.values()):
                if order.status=='active':
                    self._cancel(order.command.order_id,reason=reason)

    def submit_orders(self, commands, *, execution_seq_start=0, atomic=False):
        commands=[PortfolioCommand.model_validate(c) for c in commands]
        checked(execution_seq_start)
        if type(atomic) is not bool:
            raise ValueError('Atomic flag must be bool')
        require_unique([c.command_id for c in commands],'command ID')
        orders=set(self._order_ids)
        for c in commands:
            if c.command_id in self.commands:
                if c!=self.commands[c.command_id]:
                    raise ValueError('command_id payload conflict')
                continue
            if (c.tick,c.world_version,c.observation_version,c.batch_id)!=(self.tick,self.world_version,self.world_version,f'tick:{self.tick}:base:{self.world_version}'):
                raise ValueError('Stale observation batch')
            if isinstance(c.intent,PortfolioOrderIntent):
                if c.intent.order_id in orders:
                    raise ValueError('order_id conflict')
                orders.add(c.intent.order_id)
        before=self.export_state()
        if atomic:
            candidate=MultiAssetMarket(self._empty_ledger(),initial_prices={a:v.last_price_ticks for a,v in self.venues.items()},fault_hook=self.fault_hook,
                cancel_incoming_remainder=self.cancel_incoming_remainder)
            candidate.import_state(before)
            try:
                result=candidate.submit_orders(commands,execution_seq_start=execution_seq_start)
            except BaseException as exc:
                self.diagnostics.append({'tick':self.tick,'error_type':type(exc).__name__,'message':str(exc)})
                raise
            if any(r.status=='rejected' for r in result):
                result=[self.receipts[c.command_id] if c.command_id in self.receipts else
                    PortfolioExecutionReceipt(command_id=c.command_id,decision_id=c.decision_id,actor_id=c.actor_id,
                        execution_seq=execution_seq_start+i,status='rejected',reason_codes=['atomic_bundle_rejected',*result[i-1].reason_codes],executed_quantity=0,
                        order_ids=[c.intent.order_id] if isinstance(c.intent,(PortfolioOrderIntent,CancelIntent)) else [])
                    for i,c in enumerate(commands,1)]
                for c,r in zip(commands,result,strict=True):
                    self._record(c,r)
                return result
            self.import_state(candidate.export_state())
            return result
        try:
            result=[self.receipts[c.command_id] if c.command_id in self.receipts else self._execute(c,execution_seq_start+i)
                    for i,c in enumerate(commands,1)]
            self.validate_state()
            return result
        except BaseException as exc:
            self.diagnostics.append({'tick':self.tick,'error_type':type(exc).__name__,'message':str(exc)})
            self.import_state(before)
            raise

    def _record(self, command, receipt):
        self.commands[command.command_id]=command
        self.receipts[command.command_id]=receipt
        if isinstance(command.intent,PortfolioOrderIntent):
            self._order_ids.add(command.intent.order_id)

    def _execute(self, c, seq):
        intent=c.intent
        trades=[]
        orders=[]
        reservations=[]
        status='accepted'
        reasons=[]
        if isinstance(intent,(PortfolioOrderIntent,CancelIntent)):
            orders=[intent.order_id]
        try:
            if isinstance(intent,CancelIntent):
                self._cancel(intent.order_id,actor_id=c.actor_id)
                status='cancelled'
            elif isinstance(intent,PortfolioOrderIntent):
                if intent.asset_id not in self.venues:
                    raise LedgerReject('unknown_asset')
                if intent.quantity_units==0:
                    raise LedgerReject('zero_quantity')
                order=ReserveCommand(command_id=c.command_id,order_id=intent.order_id,reservation_id='reservation:'+intent.order_id,
                    account_id=c.actor_id,asset_id=intent.asset_id,side=intent.side,limit_price_ticks=intent.price_ticks,
                    quantity_units=intent.quantity_units)
                venue=self.venues[intent.asset_id]
                plan=venue.plan(order,cancel_incoming_remainder=self.cancel_incoming_remainder)
                self.ledger.reserve(order)
                reservations=[order.reservation_id]
        except LedgerReject as exc:
            status='rejected'
            reasons=[str(exc)]
        if status=='accepted' and isinstance(intent,PortfolioOrderIntent):
            trades=venue.match(order,c.decision_id,self.tick,plan)
            self._fault('after_book',trades)
            for index,trade in enumerate(trades,1):
                self._fault('before_settlement',index)
                self.ledger.settle(self.ledger.transaction(trade))
                self._fault('after_settlement',index)
                maker=venue.orders[trade.maker_order_id]
                self._event(trade.maker_order_id,'partial' if maker.remaining_units else 'filled',maker.remaining_units)
            remaining=venue.orders[intent.order_id].remaining_units
            status='partially_executed' if trades and remaining else 'accepted'
            self._event(intent.order_id,'partial' if trades and remaining else 'filled' if not remaining else 'accepted',remaining)
            if self.cancel_incoming_remainder and remaining:
                own=[o for o in venue.orders.values() if o.status=='active' and o.command.side!=order.side
                    and o.command.account_id==order.account_id and
                    (o.command.limit_price_ticks<=order.limit_price_ticks if order.side=='buy' else
                     o.command.limit_price_ticks>=order.limit_price_ticks)]
                if own:
                    self._cancel(intent.order_id,reason='self_trade_cancel_incoming_remainder')
                    status='partially_executed' if trades else 'cancelled'
                    reasons=['self_trade_cancel_incoming_remainder']
        elif status=='rejected' and orders:
            self._event(intent.order_id,'rejected',getattr(intent,'quantity_units',0),reasons[0])
        receipt=PortfolioExecutionReceipt(command_id=c.command_id,decision_id=c.decision_id,actor_id=c.actor_id,
            execution_seq=seq,status=status,reason_codes=reasons,executed_quantity=sum(t.quantity_units for t in trades),
            order_ids=orders,trade_ids=[t.trade_id for t in trades],ledger_tx_ids=[f'tx:{t.trade_id}' for t in trades],reservation_ids=reservations)
        self._record(c,receipt)
        return receipt

    def _empty_ledger(self):
        return PortfolioLedger(list(self.ledger.assets.values()),[a for a in self.ledger._initial.values() if a.account_id!='fees'],
                               fees=self.ledger.fees,cash_scale=self.ledger.cash_scale,purchase_lock_schedule=self.ledger.purchase_lock_schedule)

    def validate_state(self):
        self.ledger.validate_state()
        if set(self.receipts)!=set(self.commands):
            raise ValueError('Command/receipt mismatch')
        filled={}
        by_taker={}
        by_asset={a:[] for a in self.venues}
        for trade in self.trades:
            by_asset[trade.asset_id].append(trade)
            by_taker.setdefault(trade.taker_order_id,[]).append(trade)
            for oid in (trade.buy_order_id,trade.sell_order_id):
                filled[oid]=filled.get(oid,0)+trade.quantity_units
        for cid,command in self.commands.items():
            receipt=self.receipts[cid]
            intent=command.intent
            taker_trades=by_taker.get(intent.order_id,[]) if isinstance(intent,PortfolioOrderIntent) else []
            if ((receipt.command_id,receipt.decision_id,receipt.actor_id)!=(cid,command.decision_id,command.actor_id)
                    or receipt.trade_ids!=[t.trade_id for t in taker_trades]
                    or receipt.ledger_tx_ids!=[f'tx:{t.trade_id}' for t in taker_trades]
                    or receipt.executed_quantity!=sum(t.quantity_units for t in taker_trades)):
                raise ValueError('Receipt/command/trade mismatch')
        seen=set()
        for asset,venue in self.venues.items():
            MatchingVenueState.model_validate(venue.export_state())
            if venue.trade_count!=len(by_asset[asset]) or venue.arrival_sequence!=len(venue.orders):
                raise ValueError('Venue counters mismatch')
            if by_asset[asset] and venue.last_price_ticks!=by_asset[asset][-1].price_ticks:
                raise ValueError('Last trade price mismatch')
            require_unique([o.arrival_sequence for o in venue.orders.values()],'arrival sequence')
            for oid,order in venue.orders.items():
                r=self.ledger.reservations.get(order.command.reservation_id)
                command=self.commands.get(order.command.command_id)
                if (command is None or not isinstance(command.intent,PortfolioOrderIntent)
                        or (order.decision_id,order.command.account_id,oid,order.command.asset_id,order.command.side,
                            order.command.limit_price_ticks,order.command.quantity_units)!=(command.decision_id,command.actor_id,
                            command.intent.order_id,command.intent.asset_id,command.intent.side,command.intent.price_ticks,command.intent.quantity_units)):
                    raise ValueError('Venue/command identity mismatch')
                if (oid in seen or order.command.asset_id!=asset or r is None or r.command!=order.command
                        or order.command.quantity_units-filled.get(oid,0)!=order.remaining_units
                        or r.remaining_units!=(order.remaining_units if order.status=='active' else 0)
                        or (order.status=='filled')!=(order.remaining_units==0)):
                    raise ValueError('Venue/reservation/fill mismatch')
                seen.add(oid)
        if self._order_ids!={c.intent.order_id for c in self.commands.values() if isinstance(c.intent,PortfolioOrderIntent)}:
            raise ValueError('Order identity registry mismatch')
        if not seen<=self._order_ids:
            raise ValueError('Venue order has no command')

    def export_state(self):
        return {'schema_version':3,'state_id':'multiasset_market','backend':'fixedpoint_multiasset',
            'ledger':self.ledger.export_state(),'venues':[v.export_state() for v in self.venues.values()],
            'tick':self.tick,'world_version':self.world_version,'commands':[c.model_dump() for c in self.commands.values()],
            'receipts':[r.model_dump() for r in self.receipts.values()],'events':[e.model_dump() for e in self.events]}

    def import_state(self, state):
        state=MultiAssetState.model_validate(state)
        require_unique([v.asset_id for v in state.venues],'venue')
        require_unique([c.command_id for c in state.commands],'command')
        require_unique([r.command_id for r in state.receipts],'receipt')
        require_unique([e.event_id for e in state.events],'event')
        ledger=self._empty_ledger()
        ledger.import_state(state.ledger.model_dump())
        candidate=MultiAssetMarket(self._empty_ledger(),initial_prices={v.asset_id:v.last_price_ticks for v in state.venues},
            cancel_incoming_remainder=self.cancel_incoming_remainder)
        candidate.ledger=ledger
        candidate.tick,candidate.world_version=state.tick,state.world_version
        for raw in state.venues:
            require_unique([o.command.order_id for o in raw.orders],'order')
            venue=candidate.venues[raw.asset_id]
            venue.orders={o.command.order_id:o for o in raw.orders}
            venue.arrival_sequence=raw.arrival_sequence
            venue.trade_count=raw.trade_count
        candidate.commands={c.command_id:c for c in state.commands}
        candidate.receipts={r.command_id:r for r in state.receipts}
        candidate._order_ids={c.intent.order_id for c in state.commands if isinstance(c.intent,PortfolioOrderIntent)}
        candidate.events=list(state.events)
        candidate.validate_state()
        if content_hash(candidate.export_state())!=content_hash(state):
            raise ValueError('Noncanonical multiasset state')
        # Keep the sole ledger object held by the scene; no duplicate balance authority.
        self.ledger.__dict__.update(ledger.__dict__)
        for name in ('venues','tick','world_version','commands','receipts','_order_ids','events'):
            setattr(self,name,getattr(candidate,name))
