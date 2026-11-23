"""A real rule-only household/firm C/D loop on the v4 economic participant."""
from copy import deepcopy
from dataclasses import dataclass
from itertools import groupby
from typing import Any

from civitas.contracts.base import content_hash
from civitas.contracts.economic import (
    CooperationIntent, EconomicExecutionReceipt, FirstPayrollHold, LaborAcceptIntent, LaborApplyIntent, LaborContract, LaborOffer,
    LaborVacancyClosure, PayrollCashReservation, PayrollInstallment, PurchaseIntent,
    SECURITY_ASSET, SecurityOrderIntent,
)
from civitas.contracts.events import Event
from civitas.contracts.observations import ObservationFact
from civitas.contracts.state import BatchExecutionResult, Feedback
from civitas.core.rng import AddressedRNG
from civitas.economy import EconomicRuntime
from civitas.economy.runtime import SECURITY_MARK_EVENT, SecurityOpening
from civitas.economy.fiscal import OPERATIONS, TREASURY, fiscal_facts, fiscal_metrics
from civitas.economy.banking import GUARANTOR, ISSUER, bank_facts
from civitas.relations.store import RelationStore
from .closed_economy_config import ClosedEconomyConfig
from .security_market import execute_security_session, security_period, validate_security_receipts
from .closed_economy_cooperation import (
    COMPARISON_ROUND, FIRST_ROUND, FOLLOWUP_ROUND, cooperation_fact, cooperation_outcome_facts, cooperation_slot,
    offered_round, partner_continuation, record_cooperation_choice, settle_pending_cooperation, trust_trial_slot,
)
from .closed_economy_labor_outcomes import REHIRE_OFFER, SWITCH_OFFER, labor_outcome_facts
from .closed_economy_partner import PARTNER_TRACE, alternate_actors, partner_fee_id
from .closed_economy_relations import (ClosedSocialWire, SOCIAL_FIELD,
                                      initial_social, materialize_social, validate_social)
from .closed_economy_rules import production_from_minutes

SCENARIO_FIELD = "closed_economy"


@dataclass(frozen=True)
class _FollowupWindow:
    state_key: str
    switch_fee_minor: int
    offers: tuple[LaborOffer, ...]


class ClosedEconomy:
    scenario_id = "closed_economy"
    version = "1"
    owned_fields = frozenset({SCENARIO_FIELD})
    context_fields = frozenset({SCENARIO_FIELD, SOCIAL_FIELD})

    def __init__(self, config):
        self.config = ClosedEconomyConfig.model_validate(config)
        c = self.config
        self.households = [f"household:{i:04d}" for i in range(c.households)]
        self.firms = [f"firm:{i:02d}" for i in range(c.firms)]
        self.actors = sorted([*self.households, *self.firms])
        affected = c.labor_income_shock.household_indices if c.labor_income_shock else ()
        self.income_shock_workers = frozenset(self.households[i] for i in affected)
        rng = AddressedRNG(c.root_seed, c.replica)
        self._rng = rng

        def draw(actor, slot, low, high):
            return low + rng.u64("closed.population", actor, 0, slot) % (high - low + 1)

        self.population = {actor: {
            "data_origin": "synthetic_engineering_parameters",
            "initial_cash_minor": c.initial_household_cash_minor + draw(
                actor, "cash-jitter", -c.cash_jitter_minor, c.cash_jitter_minor),
            "target_units": draw(actor, "target-stock", c.target_units_min, c.target_units_max),
            "basket_share_bps": draw(actor, "basket-share", c.basket_share_min_bps, c.basket_share_max_bps),
            "cash_reserve_minor": c.cash_reserve_minor,
            "employer_id": self.firms[i % c.firms],
        } for i, actor in enumerate(self.households)}
        if c.population_input is not None:
            supplied = c.population_input
            for actor, row in zip(self.households, supplied.household_profiles, strict=True):
                self.population[actor] = {**row.model_dump(mode='json'),
                    "data_origin": supplied.data_origin, "source_id": supplied.source_id,
                    "employer_id": self.population[actor]["employer_id"]}
        if c.demand is not None:
            for index in c.demand.initially_unemployed_indices:
                self.population[self.households[index]]["employer_id"] = None
        initial_cash = {a: self.population[a]["initial_cash_minor"] for a in self.households}
        initial_cash.update({f: c.initial_firm_cash_minor for f in self.firms})
        if c.fiscal is not None:
            initial_cash.update({TREASURY: c.fiscal.initial_cash_minor, OPERATIONS: 0})
        if c.banking is not None:
            initial_cash.update({**c.banking.initial_bank_cash, ISSUER: 0, GUARANTOR: c.banking.guarantee_budget_minor})
        self.security_event = None
        security_opening = None
        if c.security_feedback is not None:
            security_opening = SecurityOpening(
                units={self.households[i]: 1 for i in c.security_feedback.holder_indices},
                price_minor=c.securities.initial_price_minor, mark_tick=c.securities.shock_tick)
            self.security_event = Event(event_id=SECURITY_MARK_EVENT, root_event_id=SECURITY_MARK_EVENT,
                tick=security_opening.mark_tick, phase="EVENTS", kind="security_mark", actors=[],
                payload={"asset_id": SECURITY_ASSET},
                visible_at={"tick": security_opening.mark_tick + 1, "phase": "OBSERVE"})
        self.economy = EconomicRuntime(initial_cash,
            labor_cost_per_unit_minor=c.labor_cost_per_unit_minor, fiscal=c.fiscal, banking=c.banking,
            security_opening=security_opening)
        for index, firm in enumerate(self.firms):
            self.economy.set_quote(f"quote:1:{firm}", firm,
                c.initial_price_minor + index * c.firm_price_gap_minor,
                effective_tick=1, ttl_ticks=c.ticks + 1)
        self.participants = (self.economy,)
        transfer = c.labor_transfer
        self.labor_offer = None
        self.followup_windows: dict[int, _FollowupWindow] = {}
        self.labor_slots = {}
        self.shared_payroll = transfer.shared_payroll if transfer is not None else None
        self.payroll_window = self.shared_payroll.payroll_window if self.shared_payroll is not None else None
        self.payroll_installments = (None, 2) if self.payroll_window and self.payroll_window.reserve_two_wages else (None,)
        self.competitive_labor = False
        if transfer is not None:
            worker = self.households[transfer.worker_index]
            firm = self.firms[transfer.employer_index]
            candidates = frozenset(
                self.households[i] for i in [transfer.worker_index, *transfer.additional_candidate_indices])
            self.competitive_labor = len(candidates) > 1 or self.shared_payroll is not None
            offer_id = (f"labor-vacancy:{transfer.offer_tick}:{firm}" if self.competitive_labor else
                        f"labor-offer:{transfer.offer_tick}:{worker}:{firm}")
            start_tick = transfer.offer_tick + 1 + int(self.payroll_window is not None)
            self.labor_offer = LaborOffer(offer_id=offer_id,
                worker_id=worker, employer_id=firm, offered_tick=transfer.offer_tick,
                start_tick=start_tick, end_tick_exclusive=start_tick + transfer.duration_ticks,
                wage_minor=c.wage_minor)
            self.labor_slots[offer_id] = (self.labor_offer, candidates)
            for window_index, window in enumerate(transfer.rehire_windows):
                tick = window.offer_tick
                original = LaborOffer(offer_id=f"labor-rehire:{tick}:{worker}:{firm}",
                    worker_id=worker, employer_id=firm, offered_tick=tick, start_tick=tick + 1,
                    end_tick_exclusive=tick + 2, wage_minor=c.wage_minor)
                offers = [original]
                for candidate in window.alternative_indices:
                    alternative = self.firms[candidate]
                    offers.append(original.model_copy(update={"employer_id": alternative,
                        "offer_id": f"labor-switch:{tick}:{worker}:{alternative}"}))
                slot = "rehire_contract" if window_index == 0 else "second_rehire_contract"
                self.followup_windows[tick] = _FollowupWindow(slot, window.switch_fee_minor, tuple(offers))
                for offer in offers:
                    self.labor_slots[offer.offer_id] = (offer, candidates)
            if self.shared_payroll is not None:
                indices = self.shared_payroll.second_candidate_indices
                second = self.labor_offer.model_copy(update={
                    "offer_id": offer_id + ":2", "worker_id": self.households[indices[0]]})
                self.labor_slots[second.offer_id] = (second, frozenset(self.households[i] for i in indices))
        self.events = [self.security_event] if self.security_event is not None else []
        if c.fault_tick is not None:
            event_id = f"delivery-fault:{c.fault_tick}:{self.firms[c.fault_seller_index]}"
            self.events.append(Event(event_id=event_id, root_event_id=event_id,
                tick=c.fault_tick, phase="EVENTS", kind="delivery_fault_schedule",
                actors=[self.firms[c.fault_seller_index]],
                visible_at={"tick": c.fault_tick + 1, "phase": "OBSERVE"},
                payload={"seller_id": self.firms[c.fault_seller_index]}))

    def initialize(self):
        return {SCENARIO_FIELD: {
            "population": deepcopy(self.population),
            "population_hash": content_hash(self.population),
            "labor_carry": {firm: 0 for firm in self.firms},
            "labor_events": [], "production_events": [], "macro_history": [],
            "fault_trace": [], "active_fault_seller": None,
            **({"security_session": None} if self.security_event is not None else {}),
            "last_macro": {"tick": 0, "sold_units_by_seller": {f: 0 for f in self.firms}},
            **({"labor_contract": None} if self.labor_offer is not None and self.shared_payroll is None else {}),
            **{window.state_key: None for window in self.followup_windows.values()},
            **({FIRST_ROUND: None} if self.config.cooperation is not None and self.config.cooperation.trust_ranking is None else {}),
            **({trust_trial_slot(t.offer_tick): None for t in self.config.cooperation.trust_ranking.scheduled_trials}
               if self.config.cooperation and self.config.cooperation.trust_ranking else {}),
            **({COMPARISON_ROUND: None} if self.config.cooperation and self.config.cooperation.comparison_trial else {}),
            **({FOLLOWUP_ROUND: None} if self.config.cooperation and self.config.cooperation.followup else {}),
            **({"vacancy_closure": None} if self.competitive_labor and self.shared_payroll is None else {}),
            **({"shared_payroll": {"contracts": {}, "closures": {}, "holds": {}}}
               if self.shared_payroll is not None else {}),
        }, SOCIAL_FIELD: ClosedSocialWire(initial_social(self.config, self.households, self.firms))}

    def _contracts_by_worker(self, own, tick):
        contracts = (own["shared_payroll"]["contracts"].values() if self.shared_payroll is not None
                     else [own.get("labor_contract")])
        by_worker = {c["offer"]["worker_id"]: c for c in contracts if c is not None}
        for window in self.followup_windows.values():
            rehire = own[window.state_key]
            if rehire is not None and not rehire["activation_failed"] and tick >= rehire["offer"]["start_tick"]:
                by_worker[rehire["offer"]["worker_id"]] = rehire
        return by_worker

    def _switch_terms(self, own, window, offer):
        earlier = [own[w.state_key] for tick, w in self.followup_windows.items() if tick < offer.offered_tick]
        predecessor = next(contract for contract in (*reversed(earlier), own["labor_contract"])
                           if contract is not None and not contract["activation_failed"])
        employer = predecessor["offer"]["employer_id"]
        return (window.switch_fee_minor, employer) if offer.employer_id != employer else None

    def _employer_at(self, worker, contract, tick):
        if contract is not None and not contract["activation_failed"]:
            offer = contract["offer"]
            if offer["worker_id"] == worker and tick >= offer["start_tick"]:
                end = contract.get("early_end_tick", offer["end_tick_exclusive"])
                return offer["employer_id"] if tick < end else None
        return self.population[worker]["employer_id"]

    def _offer_visible(self, own, tick, offer):
        if offer is None or tick != offer.offered_tick:
            return False
        if offer != self.labor_offer:
            predecessor = own["labor_contract"]
            if predecessor is None or predecessor["activation_failed"]:
                return False
        prior = own["macro_history"][tick - 2]
        if prior["tick"] != tick - 1:
            raise ValueError("Labor offer requires the prior period's settled sales")
        demand = self.config.demand
        sold = (demand.fixed_sales_units if demand is not None and not demand.hiring_adapts
                else prior["sold_units_by_seller"][offer.employer_id])
        return sold >= self.config.labor_transfer.min_previous_sales_units

    def _visible_labor_offer(self, own, tick):
        window = self.followup_windows.get(tick)
        offer = window.offers[0] if window is not None else self.labor_offer
        return offer if self._offer_visible(own, tick, offer) else None

    def _first_payroll_funding(self, offer, *, new_workers=1):
        view = self.economy.account_view(offer.employer_id)
        incumbents = sum(p["employer_id"] == offer.employer_id for p in self.population.values())
        if (offer.offered_tick in self.followup_windows
                and self.population[offer.worker_id]["employer_id"] == offer.employer_id):
            incumbents -= 1
        wages = incumbents + new_workers * len(self.payroll_installments)
        return view.available_cash_minor, view.wage_payable_minor + wages * offer.wage_minor

    def _planned_payroll(self, contract_id, offer):
        return [FirstPayrollHold(contract_id=contract_id, installment=part,
                    amount_minor=offer.wage_minor, due_tick=offer.start_tick + int(part == 2))
                for part in self.payroll_installments]

    def _release_payroll_hold(self, payroll, contract, installment):
        part = PayrollInstallment(contract_id=contract["contract_id"], installment=installment)
        if self.payroll_window is not None:
            self.economy.release_payroll(part.contract_id, installment=part.installment)
        payroll["holds"][part.reservation_id]["status"] = "released"

    def _prepare_shared_payroll(self, own, tick):
        payroll = own["shared_payroll"]
        cancel = self.shared_payroll.cancel_before_start_slot
        if cancel is not None and tick == self.labor_offer.start_tick - int(self.payroll_window is not None):
            contract = payroll["contracts"].get(tuple(self.labor_slots)[cancel - 1])
            if contract is not None:
                for part in self.payroll_installments:
                    self._release_payroll_hold(payroll, contract, part)
                contract["activation_failed"] = True
        if self.payroll_window is None and tick == self.labor_offer.start_tick:
            active = sum(hold["status"] == "held" for hold in payroll["holds"].values())
            if active:
                available, required = self._first_payroll_funding(self.labor_offer, new_workers=active)
                if available < required:
                    raise ValueError("Committed first-payroll funds are no longer available")

    def _link_fee(self, delta_id, source, target):
        if self.config.link_fee_minor:
            return target, self.firms[0], self.config.link_fee_minor
        if self.config.partner_choice is not None and delta_id == partner_fee_id(self.config):
            return source, self.firms[0], self.config.partner_choice.fee_minor
        return None

    def boundary(self, context):
        self.economy.begin_tick(context.tick, context.world_version)
        data = context.working_dump()["data"]
        if self.config.link_fee_minor or self.config.partner_choice:
            for delta in data[SOCIAL_FIELD]["network"]["pending"]:
                if delta["operation"] == "upsert" and delta["effective_tick"] == context.tick:
                    terms = self._link_fee(delta["delta_id"], delta["source_id"], delta["target_id"])
                    if terms:
                        self.economy.settle_link_fee(delta["delta_id"], *terms)
        own = data[SCENARIO_FIELD]
        own["active_fault_seller"] = None
        slot = cooperation_slot(self.config.cooperation, context.tick - 1)
        if slot is not None:
            settle_pending_cooperation(own[slot], self.economy, context.tick)
        window = self.followup_windows.get(context.tick - 1)
        if window is not None and (contract := own[window.state_key]) is not None:
            offer = LaborOffer.model_validate(contract["offer"])
            available, required = self._first_payroll_funding(offer)
            fee, recipient = self._switch_terms(own, window, offer) or (0, None)
            if available < required or (fee and self.economy.account_view(offer.worker_id).available_cash_minor < fee):
                contract["activation_failed"] = True
            elif fee:
                self.economy.settle_labor_switch_fee(contract["contract_id"], offer.worker_id, recipient, fee)
        contracts = self._contracts_by_worker(own, context.tick)
        if contracts:
            if self.shared_payroll is not None:
                self._prepare_shared_payroll(own, context.tick)
            elif context.tick == self.labor_offer.start_tick:
                available, required = self._first_payroll_funding(self.labor_offer)
                if available < required:
                    next(iter(contracts.values()))["activation_failed"] = True
        delivered = dict.fromkeys(self.firms, 0)
        shock = self.config.labor_income_shock
        shock_due = shock is not None and context.tick == shock.tick
        for worker in self.households:
            contract = contracts.get(worker)
            firm = self._employer_at(worker, contract, context.tick)
            if firm is None:
                continue
            labor_id = f"labor:{context.tick}:{worker}"
            follows_contract = contract is not None and firm == contract["offer"]["employer_id"]
            shortened = shock_due and worker in self.income_shock_workers and not follows_contract
            minutes = shock.work_minutes if shortened else 480
            behavior = self.population[worker].get("behavior")
            wage_rate = (behavior["regular_wage_minor"] if behavior and not follows_contract
                         else self.config.wage_minor)
            wage = wage_rate * minutes // 480
            delivered[firm] += minutes
            hold = reservation_id = None
            if follows_contract and self.shared_payroll is not None:
                offset = context.tick - self.labor_offer.start_tick
                if 0 <= offset < len(self.payroll_installments):
                    part = PayrollInstallment(contract_id=contract["contract_id"],
                                              installment=self.payroll_installments[offset])
                    hold = own["shared_payroll"]["holds"][part.reservation_id]
                    if self.payroll_window is not None:
                        reservation_id = part.reservation_id
            standard_cost = self.config.wage_minor * minutes // 480
            accrued = self.economy.accrue_wage(labor_id + ":accrual", firm, worker, wage,
                production_cost_minor=standard_cost if wage > standard_cost else None)
            paid = self.economy.pay_wage(labor_id + ":payment", firm, worker, wage, reservation_id=reservation_id)
            labor = {"event_id": labor_id, "tick": context.tick,
                "worker_id": worker, "employer_id": firm, "delivered_minutes": minutes,
                "accrued_minor": wage,
                "paid_minor": paid.details["paid_minor"],
                "outstanding_minor": paid.details["outstanding_minor"],
                "accrual_tx_id": accrued.tx_id, "payment_tx_id": paid.tx_id}
            if shortened:
                labor["income_shock"] = True
            if follows_contract:
                labor["contract_id"] = contract["contract_id"]
                if hold is not None:
                    if paid.details["paid_minor"] != hold["amount_minor"] or hold["status"] != "held":
                        raise ValueError("Payroll did not settle its contract hold")
                    hold.update(status="paid", payment_tx_id=paid.tx_id)
            own["labor_events"].append(labor)
        self.economy.fiscal_period()
        self.economy.banking_period()
        if self.payroll_window is not None:
            cancel_second = self.payroll_window.cancel_second_wage_slot
            if cancel_second is not None and context.tick == self.labor_offer.start_tick:
                payroll = own["shared_payroll"]
                contract = payroll["contracts"].get(tuple(self.labor_slots)[cancel_second - 1])
                if contract is not None and not contract["activation_failed"]:
                    self._release_payroll_hold(payroll, contract, 2)
                    contract["early_end_tick"] = context.tick + 1
        for firm in self.firms:
            units, carry = production_from_minutes(delivered[firm],
                self.config.output_per_worker, 480, own["labor_carry"][firm])
            own["labor_carry"][firm] = carry
            event_id = f"production:{context.tick}:{firm}"
            produced = (self.economy.produce(event_id, firm, units,
                labor_cost_minor=units * self.config.labor_cost_per_unit_minor) if units else None)
            own["production_events"].append({"event_id": event_id,
                "tick": context.tick, "firm_id": firm, "units": units,
                "delivered_minutes": delivered[firm], "carry_numerator": carry,
                "ledger_tx_id": produced.tx_id if produced else None})
        data[SOCIAL_FIELD] = ClosedSocialWire(context.data[SOCIAL_FIELD], tick=context.tick)
        return data

    def apply_event(self, context, event):
        if event == self.security_event:
            self.economy.mark_security(self._security_mark_price())
            return context.data
        if (event.kind != "delivery_fault_schedule" or event.phase != "EVENTS"
                or event.payload.get("seller_id") not in self.firms):
            raise ValueError("Unknown closed-economy event")
        data = context.working_dump()["data"]
        data[SCENARIO_FIELD]["active_fault_seller"] = event.payload["seller_id"]
        return data

    def _security_mark_price(self):
        terms = self.config.securities
        return terms.initial_price_minor - (terms.fundamental_drop_minor if self.config.security_feedback.shock_enabled else 0)

    async def prepare_observations(self, context):
        return context.data

    def active_quotes(self, tick):
        by_seller = {}
        for quote in self.economy.quotes():
            if quote.effective_tick <= tick <= quote.expires_after_tick:
                old = by_seller.get(quote.seller_id)
                if old is None or (quote.effective_tick, quote.quote_id) > (old.effective_tick, old.quote_id):
                    by_seller[quote.seller_id] = quote
        return by_seller

    def _offered_cooperation(self, context):
        config = self.config
        alternate = None
        if config.partner_choice and context.tick == config.cooperation.followup.offer_tick:
            focal, incumbent = (self.households[i] for i in config.cooperation.household_indices)
            social = materialize_social(context.data[SOCIAL_FIELD], context.tick)
            store = RelationStore(social.network)
            if config.partner_choice.pin_incumbent:
                admitted = any(r["kind"] == PARTNER_TRACE and r["actor_id"] == focal and r["tick"] == context.tick - 1
                               and r["status"] == "fixed_incumbent" for r in social.trace)
                if admitted and store.can_deliver(focal, incumbent):
                    alternate = incumbent
            else:
                alternate = next((a for a in alternate_actors(config, self.households) if store.can_deliver(focal, a)), None)
        return offered_round(config.cooperation, self.households, self.firms[0], self.economy, context.tick,
                             alternate=alternate)

    def project_observation(self, context, actor_id):
        if context.tick != self.economy.tick:
            raise ValueError("Cannot project a stale context from the live economic participant")
        now = {"tick": context.tick, "phase": "OBSERVE"}
        own = context.data[SCENARIO_FIELD]
        contracts = self._contracts_by_worker(own, context.tick)
        facts = [ObservationFact(fact_id=f"account:{context.tick}:v{context.world_version}:{actor_id}",
            category="account", owner_id=actor_id, visible_at=now,
            value=self.economy.account_view(actor_id).model_dump())]
        if self.security_event is not None:
            units = self.economy.security_units(actor_id)
            maker = actor_id == self.firms[-1]
            if context.tick == self.security_event.tick and (units or maker):
                facts.append(ObservationFact(fact_id=f"security-order:{context.tick}:{actor_id}",
                    category="market", owner_id=actor_id, visible_at=now,
                    value={"kind": "security_order_terms", "asset_id": SECURITY_ASSET,
                           "side": "buy" if maker else "sell", "quantity_units": 1,
                           "price_ticks": self.config.securities.initial_price_minor}))
            mark = self.economy.security_mark
            if (mark is not None and mark.tick < context.tick and units and actor_id in self.population
                    and self.config.security_feedback.wealth_observed):
                facts.append(ObservationFact(fact_id=f"security-wealth:{context.tick}:{actor_id}",
                    category="market", owner_id=actor_id, visible_at=now,
                    value={"kind": "security_wealth", **self.economy.security_wealth(actor_id)}))
        shock = self.config.labor_income_shock
        if shock is not None and shock.tick == context.tick and actor_id in self.income_shock_workers:
            work = next((e for e in reversed(own["labor_events"]) if e["tick"] == context.tick
                         and e["worker_id"] == actor_id and e.get("income_shock")), None)
            if work is not None:
                facts.append(ObservationFact(fact_id=f"labor-income-shock:{context.tick}:{actor_id}",
                    category="market", owner_id=actor_id, visible_at=now,
                    value={"kind": "labor_income_shock", "delivered_minutes": work["delivered_minutes"],
                           "paid_minor": work["paid_minor"], "payment_tx_id": work["payment_tx_id"]}))
        if self.config.fiscal is not None:
            facts.extend(fiscal_facts(self.economy, actor_id))
        if self.config.banking is not None:
            facts.extend(bank_facts(self.economy, actor_id))
        round_ = self._offered_cooperation(context)
        assigned_action = None
        if round_ and actor_id in round_["offered"] and actor_id == round_["seats"][1]:
            settings = self.config.cooperation
            if settings.followup and settings.followup.partner_outcome_rule and context.tick == settings.followup.offer_tick:
                assigned_action = partner_continuation(
                    settings, self.households.index(actor_id), actor_id, self._rng)["assigned_action"]
        choice_fact = cooperation_fact(round_, actor_id, assigned_action=assigned_action)
        if choice_fact is not None:
            facts.append(choice_fact)
        if self.config.stopping_hazard_bps is not None:
            facts.append(ObservationFact(fact_id="experiment:stopping-rule", category="market", public=True,
                visible_at={"tick": 0, "phase": "OBSERVE"}, value={"kind": "experiment_stopping_rule",
                    "hazard_bps": self.config.stopping_hazard_bps, "administrative_cap_tick": self.config.ticks}))
        quotes = self.active_quotes(context.tick)
        for seller, quote in sorted(quotes.items()):
            repeat = len(self.followup_windows) == 2
            terms = quote.model_dump(exclude={"expires_after_tick"} if repeat else None)
            facts.append(ObservationFact(fact_id=f"quote-view:{context.tick}:v{context.world_version}:{quote.quote_id}", category="market",
                public=True, visible_at={"tick": quote.effective_tick, "phase": "OBSERVE"},
                value={"kind": "goods_quote", **terms,
                    "available_units": self.economy.account_view(seller).goods_units}))
        if actor_id in self.population:
            profile = {"kind": "household_preferences", **self.population[actor_id],
                       "employer_id": self._employer_at(actor_id, contracts.get(actor_id), context.tick)}
            if self._offer_visible(own, context.tick, self.labor_offer):
                for offer, candidates in self.labor_slots.values():
                    if actor_id not in candidates or offer.offered_tick != context.tick:
                        continue
                    value = ({"kind": "labor_vacancy", **offer.model_dump(exclude={"worker_id", "offer_id"})}
                             if self.competitive_labor else {"kind": "labor_offer", **offer.model_dump()})
                    facts.append(ObservationFact(fact_id=offer.offer_id, category="market",
                        owner_id=actor_id, visible_at=now, value=value))
            window = self.followup_windows.get(context.tick)
            if window is not None:
                for offer in window.offers:
                    if actor_id != offer.worker_id or not self._offer_visible(own, context.tick, offer):
                        continue
                    change = self._switch_terms(own, window, offer)
                    terms = ({"switch_fee_minor": change[0], "fee_recipient_id": change[1]} if change else {})
                    facts.append(ObservationFact(fact_id=offer.offer_id, category="market", owner_id=actor_id,
                        visible_at=now, value={"kind": SWITCH_OFFER if change else REHIRE_OFFER,
                                               **offer.model_dump(), **terms}))
        else:
            index = self.firms.index(actor_id)
            quote = quotes.get(actor_id)
            profile = {"kind": "firm_plan",
                "capacity_units": sum(self._employer_at(worker, contracts.get(worker), context.tick) == actor_id
                                      for worker in self.households)
                    * self.config.output_per_worker,
                "unit_price_minor": quote.unit_price_minor if quote else
                    self.config.initial_price_minor + index * self.config.firm_price_gap_minor,
                "last_sold_units": own["last_macro"]["sold_units_by_seller"].get(actor_id, 0)}
            if (self.payroll_window is not None and self.payroll_window.purchase_units is not None
                    and actor_id == self.labor_offer.employer_id and context.tick == self.labor_offer.start_tick - 1):
                profile["window_purchase_units"] = self.payroll_window.purchase_units
        facts.append(ObservationFact(fact_id=f"profile:{context.tick}:v{context.world_version}:{actor_id}", category="market",
            owner_id=actor_id, visible_at={"tick": 0, "phase": "OBSERVE"}, value=profile))
        facts.append(ObservationFact(fact_id=f"published-macro:{context.tick}:v{context.world_version}:{own['last_macro']['tick']}",
            category="market", public=True, visible_at=now,
            value={"kind": "published_previous_period", **dict(own["last_macro"])}))
        return facts

    def execute_batch(self, context, commands):
        data = context.working_dump()["data"]
        own = data[SCENARIO_FIELD]
        security_receipts = {}
        if self.security_event is not None:
            ordered = [(context.execution_seq + i, command) for i, command in enumerate(commands, 1)
                       if isinstance(command.intent, SecurityOrderIntent)]
            if context.tick == self.security_event.tick:
                settled, entries = execute_security_session(self.economy, context,
                    [command for _, command in ordered], last_price=self.config.securities.initial_price_minor)
                # The prefunded session clears first; receipts retain the kernel's command order.
                security_receipts = {receipt.command_id: receipt.model_copy(update={"execution_seq": sequence})
                    for (sequence, _), receipt in zip(ordered, settled, strict=True)}
                own["security_session"] = security_period(context.tick, entries)
            elif ordered:
                raise ValueError("Security orders require the registered joint session")
        cooperation = self._offered_cooperation(context)
        if cooperation is not None:
            own[cooperation_slot(self.config.cooperation, context.tick)] = cooperation
        target = own["active_fault_seller"]
        failures = set()
        if target is not None:
            quotes = {q.quote_id: q for q in self.economy.quotes()}
            for command in commands:
                intent = command.intent
                if not isinstance(intent, PurchaseIntent) or intent.seller_id != target:
                    continue
                quote = quotes.get(intent.quote_id)
                if (quote is not None and quote.effective_tick <= context.tick <= quote.expires_after_tick
                        and intent.max_unit_price_minor >= quote.unit_price_minor
                        and self.economy.account_view(command.actor_id).available_cash_minor
                            >= intent.quantity_units * quote.unit_price_minor
                        and self.economy.account_view(target).goods_units >= intent.quantity_units):
                    failures.add(command.command_id)
                    break
        offer = self._visible_labor_offer(own, context.tick)
        funded = False
        window = self.followup_windows.get(context.tick)
        choice_window = window is not None and len(window.offers) > 1
        if offer is not None and not self.competitive_labor and not choice_window:
            available, required = self._first_payroll_funding(offer)
            funded = available >= required
        receipts = []
        applications = []
        followups = []
        def command_group(command):
            if command.command_id in security_receipts:
                return "security"
            if isinstance(command.intent, CooperationIntent):
                return "cooperation"
            if isinstance(command.intent, (LaborAcceptIntent, LaborApplyIntent)):
                return "labor"
            return "economy"

        for group_kind, batch in groupby(commands, key=command_group):
            start = context.execution_seq + len(receipts)
            if group_kind == "security":
                receipts.extend(security_receipts[command.command_id] for command in batch)
            elif group_kind == "cooperation":
                receipts.extend(record_cooperation_choice(cooperation, command, start + i)
                                for i, command in enumerate(batch, 1))
            elif group_kind == "labor":
                for i, command in enumerate(batch, 1):
                    if isinstance(command.intent, LaborApplyIntent):
                        applications.append((len(receipts), command))
                        receipts.append(None)
                    elif choice_window:
                        followups.append((len(receipts), command))
                        receipts.append(None)
                    else:
                        receipts.append(self._accept_labor_offer(own, offer, funded, command, start + i))
            else:
                group = list(batch)
                receipts.extend(self.economy.execute_batch(group, execution_seq_start=start,
                    delivery_failures=failures.intersection(c.command_id for c in group)))
        closing = self.competitive_labor and context.tick == self.labor_offer.offered_tick
        if applications or closing:
            # Approve first payroll after the last competing cash outflow, preserving kernel receipt order.
            self._close_labor_vacancies(own, offer if closing else None, context, applications, receipts, closing)
        if followups:
            # Funding checks see the completed goods wave and keep the original receipt order.
            visible = {candidate.offer_id: candidate for candidate in window.offers
                       if self._offer_visible(own, context.tick, candidate)}
            switch_only = self.config.labor_transfer.rehire_at(context.tick).switch_only
            attempted = False
            for index, command in followups:
                selected = visible.get(command.intent.offer_id)
                sequence = context.execution_seq + index + 1
                if switch_only and selected is not None and selected.worker_id == command.actor_id:
                    if self._switch_terms(own, window, selected) is None:
                        receipts[index] = self._labor_receipt(selected, command, sequence, "labor_switch_required")
                        continue
                    if attempted:
                        receipts[index] = self._labor_receipt(selected, command, sequence, "labor_switch_attempt_limit")
                        continue
                    attempted = True
                available, required = self._first_payroll_funding(selected) if selected is not None else (0, 1)
                receipts[index] = self._accept_labor_offer(own, selected, available >= required, command, sequence)
        if target is not None:
            own["fault_trace"].append({"tick": context.tick,
                "seller_id": target, "selected_commands": sorted(failures),
                "realized_commands": [r.command_id for r in receipts if r.quality_failure]})
        return BatchExecutionResult(data=data, receipts=receipts)

    def _accept_labor_offer(self, own, offer, funded, command, sequence):
        reason = None
        window = self.followup_windows.get(command.tick)
        slot = window.state_key if window is not None else "labor_contract"
        if (self.competitive_labor or offer is None or command.actor_id != offer.worker_id
                or command.intent.offer_id != offer.offer_id
                or "accept_labor_offer" in self.config.disabled_actions):
            reason = "labor_offer_unavailable"
        elif own[slot] is not None:
            reason = "labor_offer_already_accepted"
        elif not funded:
            reason = "insufficient_labor_funding"
        elif window is not None and (change := self._switch_terms(own, window, offer)) is not None:
            limit = command.intent.max_switch_fee_minor
            if limit is None or limit < change[0]:
                reason = "labor_switch_fee_not_authorized"
            elif self.economy.account_view(offer.worker_id).available_cash_minor < change[0]:
                reason = "insufficient_labor_switch_fee_cash"
        if reason is None:
            own[slot] = self._signed_contract(offer, command)
        return self._labor_receipt(offer, command, sequence, reason)

    def _close_labor_vacancies(self, own, offer, context, applications, receipts, closing):
        by_vacancy = {}
        for index, command in applications:
            by_vacancy.setdefault(command.intent.vacancy_id, []).append((index, command))
        available, required = self._first_payroll_funding(offer) if offer is not None else (None, None)
        if closing:
            for slot, candidates in self.labor_slots.values():
                applicants, contract = self._resolve_applications(
                    slot if offer is not None else None, candidates, by_vacancy.pop(slot.offer_id, []),
                    available, required, context.execution_seq, receipts)
                status = "not_opened" if offer is None else "unfilled"
                if applicants:
                    status = "filled" if contract is not None else "unfunded"
                closure = LaborVacancyClosure(vacancy_id=slot.offer_id,
                    tick=context.tick, status=status, applicant_ids=applicants,
                    available_cash_minor=available, first_payroll_minor=required).model_dump()
                if self.shared_payroll is None:
                    own["vacancy_closure"] = closure
                    own["labor_contract"] = contract
                else:
                    payroll = own["shared_payroll"]
                    payroll["closures"][slot.offer_id] = closure
                    if contract is not None:
                        for hold in self._planned_payroll(contract["contract_id"], slot):
                            if self.payroll_window is not None:
                                self.economy.reserve_payroll(hold.contract_id, slot.employer_id,
                                    contract["offer"]["worker_id"], hold.amount_minor, due_tick=hold.due_tick,
                                    installment=hold.installment)
                            payroll["holds"][hold.reservation_id] = hold.model_dump()
                            available -= hold.amount_minor
                        payroll["contracts"][slot.offer_id] = contract
        for group in by_vacancy.values():
            for index, command in group:
                receipts[index] = self._labor_receipt(
                    None, command, context.execution_seq + index + 1, "labor_vacancy_unavailable")

    def _resolve_applications(self, offer, candidates, applications, available, required, execution_seq, receipts):
        applicants = {}
        contract = None
        for index, command in applications:
            reason = None
            if (offer is None or command.actor_id not in candidates
                    or "apply_labor_vacancy" in self.config.disabled_actions):
                reason = "labor_vacancy_unavailable"
            elif command.actor_id in applicants:
                reason = "duplicate_labor_application"
            else:
                applicants[command.actor_id] = None
                if available < required:
                    reason = "insufficient_labor_funding"
                elif contract is not None:
                    reason = "labor_vacancy_filled"
                else:
                    contract = self._signed_contract(offer, command)
            receipts[index] = self._labor_receipt(offer, command, execution_seq + index + 1, reason)
        return list(applicants), contract

    @staticmethod
    def _signed_contract(offer, command):
        return LaborContract(contract_id=command.command_id,
            offer=offer.model_copy(update={"worker_id": command.actor_id})).model_dump()

    @staticmethod
    def _labor_receipt(offer, command, sequence, reason):
        return EconomicExecutionReceipt(command_id=command.command_id, decision_id=command.decision_id,
            actor_id=command.actor_id, execution_seq=sequence, tick=command.tick, world_version=command.world_version,
            status="rejected" if reason else "accepted", outcome="rejected" if reason else "labor_offer_accepted",
            unit="labor_minutes", executed_quantity=0, counterparty_id=None if reason else offer.employer_id,
            reason_codes=[reason] if reason else [])

    def feedback(self, context, receipts):
        data = context.working_dump()["data"]
        own = data[SCENARIO_FIELD]
        sold = {f: 0 for f in self.firms}
        expenditure = {f: 0 for f in self.firms}
        for receipt in receipts:
            if receipt.outcome == "purchased":
                sold[receipt.counterparty_id] += receipt.executed_quantity
                expenditure[receipt.counterparty_id] -= receipt.cash_delta_minor
        current_labor = [e for e in own["labor_events"] if e["tick"] == context.tick]
        current_production = [e for e in own["production_events"] if e["tick"] == context.tick]
        macro = {"tick": context.tick, "sold_units_by_seller": sold,
            "sales_minor_by_seller": expenditure,
            "consumed_units": sum(r.executed_quantity for r in receipts if r.outcome == "consumed"),
            "produced_units": sum(e["units"] for e in current_production),
            "wage_accrued_minor": sum(e["accrued_minor"] for e in current_labor),
            "wage_paid_minor": sum(e["paid_minor"] for e in current_labor),
            "quality_failures": sum(r.quality_failure for r in receipts),
            "rejections": sum(r.status == "rejected" for r in receipts),
            "scrapped_units": sum(r.accepted_quantity for r in receipts if r.quality_failure),
            "price_minor_by_seller": {f: q.unit_price_minor for f, q in self.active_quotes(context.tick).items()}}
        if self.labor_offer is not None:
            macro["employed_by_firm"] = {firm: sum(e["employer_id"] == firm for e in current_labor)
                                         for firm in self.firms}
        own["last_macro"] = macro
        own["macro_history"].append(macro)
        memories = [ObservationFact(fact_id="work-memory:" + e["event_id"],
            category="memory", owner_id=e["worker_id"],
            visible_at={"tick": context.tick + 1, "phase": "OBSERVE"},
            value={"kind": "verified_labor_settlement", **e}) for e in current_labor]
        if self.config.labor_transfer and self.config.labor_transfer.outcome_evidence:
            memories.extend(labor_outcome_facts(context.tick, current_labor, self.economy.journal))
        if self.config.cooperation and (self.config.cooperation.followup or self.config.offline_cooperation_action is not None):
            slot = cooperation_slot(self.config.cooperation, context.tick - 1)
            if slot is not None:
                memories.extend(cooperation_outcome_facts(own[slot], context.tick, self.economy.journal))
        return Feedback(data=data, memories=memories)

    def validate_snapshot(self, snapshot):
        if self.security_event is not None:
            validate_security_receipts(snapshot, mixed=True)

    def validate(self, context):
        own = context.data[SCENARIO_FIELD]
        if self.security_event is not None:
            tick = self.security_event.tick
            mark = self.economy.security_mark
            expected = None
            if context.tick >= tick:
                if mark is None or mark.tick != tick or mark.payload["price_minor"] != self._security_mark_price():
                    raise ValueError("Joint security mark differs from its schedule")
                expected = security_period(tick, [e for e in self.economy.journal if e.kind == "security_trade"])
            elif mark is not None:
                raise ValueError("Joint security mark precedes its schedule")
            if own["security_session"] != expected:
                raise ValueError("Joint security session differs from the journal")
        if (set(context.actors) != set(self.actors) or own["population"] != self.population
                or own["population_hash"] != content_hash(self.population)):
            raise ValueError("Closed-economy population/config identity changed")
        if set(own["labor_carry"]) != set(self.firms) or any(
                type(v) is not int or not 0 <= v < 480 for v in own["labor_carry"].values()):
            raise ValueError("Invalid carried labor numerator")
        social = validate_social(context.data[SOCIAL_FIELD])
        if (social.network is not None) != (self.config.link_budget_per_tick is not None):
            raise ValueError("Communication graph does not match the configured mode")
        if social.network is not None and social.network.tick != context.tick:
            raise ValueError("Communication graph does not match the world clock")
        if self.config.link_fee_minor or self.config.partner_choice:
            expected = {d.delta_id: (d.effective_tick, *terms) for d in social.network.applied if d.operation == "upsert"
                        if (terms := self._link_fee(d.delta_id, d.source_id, d.target_id))}
            actual = {e.payload["delta_id"]: (e.tick, e.payload["payer_id"], e.payload["recipient_id"],
                                              e.payload["amount_minor"])
                      for e in self.economy.journal if e.kind == "link_fee"}
            if actual != expected:
                raise ValueError("Link fees disagree with the committed communication changes")
        self._validate_labor(own, context.tick)
        if any(len(w.offers) > 1 for w in self.followup_windows.values()):
            expected = []
            for tick, window in self.followup_windows.items():
                contract = own[window.state_key]
                if contract is None or contract["activation_failed"] or context.tick <= tick:
                    continue
                offer = LaborOffer.model_validate(contract["offer"])
                change = self._switch_terms(own, window, offer)
                if change and change[0]:
                    fee, recipient = change
                    expected.append((offer.start_tick, {"contract_id": contract["contract_id"],
                        "payer_id": offer.worker_id, "recipient_id": recipient, "amount_minor": fee}))
            actual = [(e.tick, e.payload) for e in self.economy.journal if e.kind == "labor_switch_fee"]
            if actual != expected:
                raise ValueError("Labor switch fees disagree with the committed contract")
        if len({e["event_id"] for e in own["labor_events"]}) != len(own["labor_events"]):
            raise ValueError("Duplicate delivered labor event")
        if len({m["tick"] for m in own["macro_history"]}) != len(own["macro_history"]):
            raise ValueError("Duplicate macro period")

    def _validate_labor(self, own, tick):
        shared = self.shared_payroll is not None
        expected_fields = {"shared_payroll"} if shared else set()
        if self.labor_offer is not None and not shared:
            expected_fields.add("labor_contract")
            if self.competitive_labor:
                expected_fields.add("vacancy_closure")
        expected_fields.update(w.state_key for w in self.followup_windows.values())
        labor_fields = {"labor_contract", "vacancy_closure", "shared_payroll", "rehire_contract", "second_rehire_contract"}
        if set(own) & labor_fields != expected_fields:
            raise ValueError("Labor mechanism configuration changed")
        if self.payroll_window is None and self.economy.payroll_reservations:
            raise ValueError("Cash reservations require a payroll-window contract")
        if self.labor_offer is None:
            return
        closed = tick >= self.labor_offer.offered_tick
        opened = closed and self._visible_labor_offer(own, self.labor_offer.offered_tick) is not None
        if not shared:
            _, initial = self._validate_labor_slot(self.labor_offer.offer_id, own.get("labor_contract"),
                own.get("vacancy_closure"), tick, opened)
            contract_ids = {initial.contract_id} if initial is not None else set()
            for offer_tick, window in self.followup_windows.items():
                recorded = own[window.state_key]
                if recorded is None:
                    continue
                selected = next((offer for offer in window.offers if offer.offer_id == recorded["offer"]["offer_id"]), None)
                if selected is None:
                    raise ValueError("The committed rehire offer belongs to a different window")
                rehire_opened = tick >= offer_tick and self._offer_visible(own, offer_tick, selected)
                _, contract = self._validate_labor_slot(selected.offer_id,
                    recorded, None, tick, rehire_opened)
                if contract.contract_id in contract_ids:
                    raise ValueError("Rehire requires a distinct contract identity")
                contract_ids.add(contract.contract_id)
            return
        payroll = own["shared_payroll"]
        if set(payroll) != {"contracts", "closures", "holds"}:
            raise ValueError("Invalid shared payroll state fields")
        contracts, closures, holds = payroll["contracts"], payroll["closures"], payroll["holds"]
        if set(closures) != (set(self.labor_slots) if closed else set()) or not set(contracts) <= set(closures):
            raise ValueError("Shared payroll slots do not match the closed window")
        contract_ids = [contract["contract_id"] for contract in contracts.values()]
        if len(set(contract_ids)) != len(contract_ids):
            raise ValueError("Payroll contracts must have distinct identities")
        available, required = None, None
        expected_holds = set()
        active_reservations = []
        for number, vacancy_id in enumerate(self.labor_slots, 1):
            closure, contract = self._validate_labor_slot(vacancy_id, contracts.get(vacancy_id),
                closures.get(vacancy_id), tick, opened)
            if closure is not None and opened:
                if number == 1:
                    available, required = closure.available_cash_minor, closure.first_payroll_minor
                    if tick == closure.tick:
                        free, due = self._first_payroll_funding(self.labor_offer)
                        reserved = sum(r.amount_minor for r in self.economy.payroll_reservations)
                        if (available, required) != (free + reserved, due):
                            raise ValueError("Shared payroll approval disagrees with settled funding")
                if (closure.available_cash_minor, closure.first_payroll_minor) != (available, required):
                    raise ValueError("Vacancy approvals used inconsistent shared payroll funds")
            if contract is not None:
                start = contract.offer.start_tick
                cancel_start = (self.shared_payroll.cancel_before_start_slot == number
                                and tick >= start - int(self.payroll_window is not None))
                cancel_second = (self.payroll_window is not None
                                 and self.payroll_window.cancel_second_wage_slot == number and tick >= start
                                 and not cancel_start)
                if (contract.activation_failed, contract.early_end_tick) != (
                        cancel_start, start + 1 if cancel_second else None):
                    raise ValueError("Labor termination disagrees with the scheduled cancellation")
                for expected in self._planned_payroll(contract.contract_id, contract.offer):
                    key = expected.reservation_id
                    expected_holds.add(key)
                    hold = FirstPayrollHold.model_validate(holds.get(key))
                    self._validate_payroll_hold(hold, expected, contract, tick)
                    if self.payroll_window is not None and hold.status == "held":
                        active_reservations.append(PayrollCashReservation(contract_id=hold.contract_id,
                            installment=hold.installment, employer_id=contract.offer.employer_id,
                            worker_id=contract.offer.worker_id, amount_minor=hold.amount_minor, due_tick=hold.due_tick))
                    available -= hold.amount_minor
        if expected_holds != set(holds):
            raise ValueError("Payroll holds must match the scheduled installments")
        if tuple(sorted(active_reservations, key=lambda r: r.reservation_id)) != self.economy.payroll_reservations:
            raise ValueError("Active cash reservations do not match labor contracts")

    def _validate_labor_slot(self, vacancy_id, raw_contract, raw_closure, tick, opened):
        slot, candidates = self.labor_slots[vacancy_id]
        contract = None
        if raw_contract is not None:
            contract = LaborContract.model_validate(raw_contract)
            if (not opened or contract.offer.worker_id not in candidates
                    or contract.offer != slot.model_copy(update={"worker_id": contract.offer.worker_id})
                    or (contract.early_end_tick is not None and 2 not in self.payroll_installments)
                    or (contract.activation_failed
                        and tick < slot.start_tick - int(self.payroll_window is not None))):
                raise ValueError("Invalid committed labor contract")
        closure = None
        if self.competitive_labor and tick >= slot.offered_tick:
            closure = LaborVacancyClosure.model_validate(raw_closure)
            if ((closure.vacancy_id, closure.tick) != (slot.offer_id, slot.offered_tick)
                    or opened == (closure.status == "not_opened")
                    or not set(closure.applicant_ids) <= candidates
                    or (contract is not None) != (closure.status == "filled")
                    or (contract is not None and contract.offer.worker_id != closure.applicant_ids[0])):
                raise ValueError("Invalid committed vacancy closure")
        elif raw_closure is not None:
            raise ValueError("Vacancy closed before its observation window")
        return closure, contract

    def _validate_payroll_hold(self, hold, expected, contract, tick):
        offer = contract.offer
        status = "paid" if tick >= expected.due_tick else "held"
        cancel_tick = None
        if contract.activation_failed:
            cancel_tick = offer.start_tick - int(self.payroll_window is not None)
        elif expected.installment == 2 and contract.early_end_tick is not None:
            cancel_tick = contract.early_end_tick - 1
        if cancel_tick is not None:
            status = "released"
        if hold != expected.model_copy(update={"status": status, "payment_tx_id": hold.payment_tx_id}):
            raise ValueError("Payroll hold disagrees with its contract or due phase")
        if status == "paid":
            payment = next((j for j in self.economy.journal if j.tx_id == hold.payment_tx_id), None)
            if (payment is None or payment.kind != "wage_payment" or payment.tick != hold.due_tick
                    or payment.payload["worker_id"] != offer.worker_id
                    or payment.payload["employer_id"] != offer.employer_id
                    or payment.details["paid_minor"] != hold.amount_minor):
                raise ValueError("Paid payroll hold lacks its matching wage payment")
            if self.payroll_window is not None and payment.payload.get("reservation_id") != hold.reservation_id:
                raise ValueError("Wage payment did not settle the contract cash reservation")
        if status == "released" and self.payroll_window is not None:
            if not any(j.kind == "payroll_release" and j.tick == cancel_tick
                       and j.event_id == f"payroll-release:{hold.reservation_id}" for j in self.economy.journal):
                raise ValueError("Canceled contract lacks its cash reservation release")

    def metrics(self, context) -> dict[str, Any]:
        own = context.data[SCENARIO_FIELD]
        social = materialize_social(context.data[SOCIAL_FIELD], context.tick)
        views = {a: self.economy.account_view(a) for a in self.economy.actor_ids}
        scores = [v for a in social.current.values() for v in a.scores_bps.values()]
        history = own["macro_history"]
        choices = [e for e in social.trace if e.get("kind") == "decision" and e["tick"] == context.tick]
        return {"tick": context.tick, "social_mode": self.config.social_mode,
            "population_hash": own["population_hash"],
            **({"fiscal": fiscal_metrics(self.economy)} if self.config.fiscal is not None else {}),
            **({"banking": self.economy.banking_report()} if self.config.banking is not None else {}),
            "cash_total_minor": sum(v.cash_minor for v in views.values()),
            "cash_minor_by_actor": {a: v.cash_minor for a, v in views.items()},
            "inventory_units_by_actor": {a: v.goods_units for a, v in views.items()},
            "goods_total_units": sum(v.goods_units for v in views.values()),
            "wage_outstanding_minor": sum(views[a].wage_receivable_minor for a in self.households),
            "produced_units_total": sum(m["produced_units"] for m in history),
            "consumed_units_total": sum(m["consumed_units"] for m in history),
            "scrapped_units_total": sum(m["scrapped_units"] for m in history),
            "wage_paid_minor_total": sum(m["wage_paid_minor"] for m in history),
            "quality_failures_total": sum(m["quality_failures"] for m in history),
            "rejections_total": sum(m["rejections"] for m in history),
            "mean_active_fulfillment_bps": sum(scores) / len(scores) if scores else 10000.0,
            "peer_consumptions_total": sum(e.get("kind") == "peer_consumption" for e in social.trace),
            "exposures_total": sum(e.get("kind") == "exposure" for e in social.trace),
            "choices": choices, "last_macro": dict(own["last_macro"])}
