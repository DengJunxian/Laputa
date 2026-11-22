"""A funded single-asset session on the same v4 economic participant."""
from collections import defaultdict

from civitas.contracts.economic import EconomicDecisionBundle, EconomicExecutionReceipt, SecurityOrderIntent
from civitas.contracts.events import Event
from civitas.contracts.observations import ObservationFact
from civitas.contracts.portfolio import ReserveCommand
from civitas.contracts.state import BatchExecutionResult, Feedback
from civitas.economy.runtime import EconomicRuntime
from civitas.markets.multiasset import MatchingVenue
from civitas.resources.fixedpoint import notional
from civitas.resources.ledger import LedgerReject

ASSET = "security:A"
STATE = "security_market"
SHOCK = "event:security:fundamental"
SOURCE = "source:security:"
SESSION_UNITS = 3


def validate_security_receipts(snapshot):
    entries = {entry["tx_id"]: entry for entry in snapshot.data["economic_runtime"]["journal"]
               if entry["kind"] == "security_trade"}
    orders = {f"command:{intent.intent_id}": (decision, intent)
              for decision in snapshot.decisions for intent in decision.economic_intents}
    references = defaultdict(set)
    if orders.keys() != {receipt.command_id for receipt in snapshot.receipts}:
        raise ValueError("Security orders and receipts differ")
    for receipt in snapshot.receipts:
        decision, intent = orders[receipt.command_id]
        if (receipt.unit != "security_units" or receipt.tick != decision.tick
                or receipt.actor_id != decision.actor_id or receipt.decision_id != decision.decision_id):
            raise ValueError("Security receipt does not identify its decision")
        if receipt.status == "rejected":
            continue
        if (not isinstance(intent, SecurityOrderIntent) or intent.asset_id != ASSET
                or receipt.accepted_quantity != intent.quantity_units):
            raise ValueError("Security receipt does not identify an accepted order")
        buying = intent.side == "buy"
        party, order = ("buyer_id", "buy_order_id") if buying else ("seller_id", "sell_order_id")
        cash = quantity = 0
        for ref in receipt.ledger_tx_ids:
            fill = entries.get(ref)
            if fill is None:
                raise ValueError("Security receipt references an unknown fill")
            trade = fill["payload"]
            if (fill["tick"] != receipt.tick or trade[party] != receipt.actor_id
                    or trade[order] != intent.intent_id or party in references[ref]
                    or (trade["price_ticks"] > intent.price_ticks if buying
                        else trade["price_ticks"] < intent.price_ticks)):
                raise ValueError("Security receipt does not match the fill's order or limit")
            references[ref].add(party)
            quantity += trade["quantity_units"]
            cash += fill["details"]["gross_minor"] * (-1 if buying else 1)
        if (receipt.cash_delta_minor, receipt.executed_quantity) != (cash, quantity):
            raise ValueError("Security receipt differs from its journal settlement")
    if any(references[ref] != {"buyer_id", "seller_id"} for ref in entries):
        raise ValueError("Security settlement is missing a bilateral receipt")


class SecurityPolicy:
    policy_id = "security_session_rule"
    version = "1"

    def __init__(self, settings, rng):
        self.settings = settings
        self.rng = rng

    async def decide(self, obs):
        actor, tick = obs.actor_id, obs.visible_until.tick
        account, = obs.account_view
        market = next(f for f in obs.market_view if f.value["kind"] == "security_market")
        exposure = next((f for f in obs.market_view if f.value["kind"] == "security_signal"), None)
        value = market.value
        previous = value["last_price_minor"]
        fundamental = value["fundamental_price_minor"]
        signal = exposure.value["sentiment_minor"] if exposure else 0
        weight = signal if self.settings.herding else 0
        maker = actor.startswith("maker:")
        jitter = self.rng.u64("security.quote", actor, tick, "price") % 3 - 1
        if maker:
            price = {"prior_close": previous, "fundamental": fundamental}.get(
                self.settings.binding_maker_bid, max(previous, fundamental) + 2)
        else:
            price = max(1, fundamental + weight + jitter)
        units = 2 if maker else min(SESSION_UNITS, account.value["security_units"])
        intent = SecurityOrderIntent(intent_id=f"security:{tick}:{actor}", asset_id=ASSET,
                    side="buy" if maker else "sell", price_ticks=price, quantity_units=units) if units else None
        return EconomicDecisionBundle(actor_id=actor, tick=tick, decision_id=f"decision:{tick}:{actor}",
            observation_id=obs.observation_id, batch_id=obs.batch_id, observation_version=obs.observation_version,
            world_version=obs.world_version, policy_id=self.policy_id, policy_version=self.version,
            evidence_ids=[account.fact_id, market.fact_id, *([exposure.fact_id] if exposure else [])],
            belief_patch={"security_appraisal": {"fundamental_minor": fundamental, "prior_trade_minor": previous,
                "raw_signal_minor": signal, "applied_herding_minor": weight,
                "exposed_source": exposure.value["source"] if exposure else None, "limit_minor": price}},
            economic_intents=[intent] if intent else [])


class SecurityMarket:
    scenario_id = STATE
    version = "1"
    owned_fields = frozenset({STATE})
    validate_snapshot = staticmethod(validate_security_receipts)

    def __init__(self, config, rng):
        self.config, self.settings, self.rng = config, config.securities, rng
        investors = [f"investor:{i:04d}" for i in range(config.households)]
        makers = [f"maker:{i:02d}" for i in range(config.firms)]
        self.actors = [*investors, *makers]
        units = config.ticks * SESSION_UNITS
        self.economy = EconomicRuntime({**dict.fromkeys(investors, 0),
                **dict.fromkeys(makers, self.settings.maker_cash_minor)},
            good_id=ASSET, initial_goods=dict.fromkeys(investors, units),
            initial_inventory_cost=dict.fromkeys(investors, notional(self.settings.initial_price_minor, 1, units)))
        self.participants = (self.economy,)
        self.events = [Event(event_id=SHOCK, root_event_id=SHOCK, kind="fundamental_shock", actors=[],
            tick=self.settings.shock_tick, phase="EVENTS",
            visible_at={"tick":self.settings.shock_tick, "phase":"OBSERVE"},
            payload={"fundamental_drop_minor": self.settings.fundamental_drop_minor})]

    def initialize(self):
        return {STATE: {"fundamental_price_minor": self.settings.initial_price_minor,
                       "last_price_minor": self.settings.initial_price_minor, "history": []}}

    def boundary(self, context):
        self.economy.begin_tick(context.tick, context.world_version)
        return dict(context.data)

    def apply_event(self, context, event):
        if event != self.events[0]:
            raise ValueError("Unregistered security event")
        state = context.data[STATE]
        return {**context.data, STATE: {**state,
            "fundamental_price_minor": state["fundamental_price_minor"] - event.payload["fundamental_drop_minor"]}}

    async def prepare_observations(self, context):
        return dict(context.data)

    def project_observation(self, context, actor_id):
        state, tick = context.data[STATE], context.tick
        now = {"tick": tick, "phase":"OBSERVE"}
        account = self.economy.account_view(actor_id)
        facts = [
            ObservationFact(fact_id=f"security-account:{tick}:{context.world_version}:{actor_id}",
                owner_id=actor_id, category="account", visible_at=now, value={"cash_minor": account.cash_minor,
                "security_units":account.goods_units, "carrying_cost_minor":account.inventory_cost_minor}),
            ObservationFact(fact_id=f"security-public:{tick}:{context.world_version}", public=True,
                category="market", visible_at=now, value={"kind":STATE, "asset_id":ASSET,
                "fundamental_price_minor":state["fundamental_price_minor"],
                "last_price_minor":state["last_price_minor"],
                "stale":not state["history"] or state["history"][-1]["close_minor"] is None}),
        ]
        if tick >= self.settings.shock_tick and not actor_id.startswith("maker:"):
            pessimistic = (self.settings.selective_exposure
                           or not self.rng.u64("security.exposure", actor_id, tick, "one-source") % 2)
            facts.append(ObservationFact(fact_id=f"security-exposure:{tick}:{context.world_version}:{actor_id}",
                owner_id=actor_id, category="market", visible_at=now, value={"kind":"security_signal",
                "root_event_id":SHOCK, "source": SOURCE + ("pessimistic" if pessimistic else "neutral"),
                "sentiment_minor": -self.settings.signal_gap_minor if pessimistic else 0}))
        return facts

    def execute_batch(self, context, commands):
        state, tick = context.data[STATE], context.tick
        venue = MatchingVenue(ASSET, state["last_price_minor"])
        venue.trade_count = sum(row["fill_count"] for row in state["history"])
        accounts = {a: self.economy.account_view(a) for a in context.actors}
        offered, cash = defaultdict(int), {a: v.available_cash_minor for a,v in accounts.items()}
        orders, rejected, filled = {}, {}, defaultdict(list)
        for command in commands:
            intent = command.intent
            if not isinstance(intent, SecurityOrderIntent) or intent.asset_id != ASSET:
                rejected[command.command_id] = "invalid_security_order"
                continue
            order = ReserveCommand(command_id=command.command_id, order_id=intent.intent_id,
                reservation_id="session:"+intent.intent_id, account_id=command.actor_id, asset_id=ASSET,
                side=intent.side, limit_price_ticks=intent.price_ticks, quantity_units=intent.quantity_units)
            orders[command.command_id] = order
            if order.side == "sell":
                if offered[order.account_id] + order.quantity_units > accounts[order.account_id].goods_units:
                    rejected[command.command_id] = "insufficient_security_units"
                    continue
                offered[order.account_id] += order.quantity_units
                venue.match(order, command.decision_id, tick, [])
        # Session sellers publish funded resting orders. Buyers execute in kernel
        # priority order and their residual cannot rest or fund any later order.
        entries = []
        for command in commands:
            order = orders.get(command.command_id)
            if order is None or order.side != "buy":
                continue
            try:
                required = notional(order.limit_price_ticks, 1, order.quantity_units)
            except (OverflowError, ValueError):
                rejected[command.command_id] = "invalid_security_notional"
                continue
            if required > cash[order.account_id]:
                rejected[command.command_id] = "insufficient_security_cash"
                continue
            try:
                plan = venue.plan(order)
            except LedgerReject as exc:
                rejected[command.command_id] = str(exc)
                continue
            for trade in venue.match(order, command.decision_id, tick, plan):
                entry = self.economy.settle_security(trade)
                gross = entry.details["gross_minor"]
                cash[trade.buyer_id] -= gross
                entries.append(entry)
                filled[trade.buy_order_id].append((trade.quantity_units, -gross, entry.tx_id))
                filled[trade.sell_order_id].append((trade.quantity_units, gross, entry.tx_id))
            venue.cancel(order.order_id)

        receipts = []
        for seq, command in enumerate(commands, context.execution_seq + 1):
            error, order = rejected.get(command.command_id), orders.get(command.command_id)
            fills = filled.get(order.order_id, []) if order else []
            quantity = sum(f[0] for f in fills)
            accepted = 0 if error else order.quantity_units
            receipts.append(EconomicExecutionReceipt(command_id=command.command_id, decision_id=command.decision_id,
                actor_id=command.actor_id, tick=tick, world_version=context.world_version, execution_seq=seq,
                status="rejected" if error else "partially_executed" if 0 < quantity < accepted else "accepted",
                outcome="rejected" if error else "security_filled" if fills else "security_unfilled",
                reason_codes=[error] if error else ["session_remainder_expired"] if quantity < accepted else [],
                unit="security_units", executed_quantity=quantity, accepted_quantity=accepted,
                cash_delta_minor=sum(f[1] for f in fills), ledger_tx_ids=[f[2] for f in fills]))
        row = self._period(tick, entries)
        return BatchExecutionResult(data={**context.data, STATE: {**state, "last_price_minor":venue.last_price_ticks,
                        "history": [*state["history"], row]}}, receipts=receipts)

    @staticmethod
    def _period(tick, entries):
        return {"tick": tick, "close_minor":entries[-1].payload["price_ticks"] if entries else None,
                "fill_count":len(entries), "volume_units":sum(e.payload["quantity_units"] for e in entries),
                "journal_tx_ids":[e.tx_id for e in entries]}

    def feedback(self, context, receipts):
        return Feedback(data=context.data)

    def validate(self, context):
        state = context.data[STATE]
        if self.economy.tick != context.tick or self.economy.actor_ids != tuple(context.actors):
            raise ValueError("Security clock or accounts differ from the world")
        by_tick = defaultdict(list)
        for entry in self.economy.journal:
            if entry.kind == "security_trade":
                by_tick[entry.tick].append(entry)
            elif entry.kind != "opening":
                raise ValueError("Security-only world contains an unrelated economic event")
        expected = [self._period(t, by_tick[t]) for t in range(1,context.tick+1)]
        closes = [row["close_minor"] for row in expected if row["close_minor"] is not None]
        fundamental = self.settings.initial_price_minor - (
            self.settings.fundamental_drop_minor if context.tick >= self.settings.shock_tick else 0)
        if (state["history"] != expected or state["fundamental_price_minor"] != fundamental
                or state["last_price_minor"] != (closes[-1] if closes else self.settings.initial_price_minor)):
            raise ValueError("Security price path differs from the journal or shock")

    def metrics(self, context):
        state = context.data[STATE]
        return {"asset_id":ASSET, "last_price_minor":state["last_price_minor"],
                "fills":sum(row["fill_count"] for row in state["history"]),
                "volume_units":sum(row["volume_units"] for row in state["history"]),
                "no_trade_periods":sum(row["close_minor"] is None for row in state["history"])}
