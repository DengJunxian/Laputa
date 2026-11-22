"""One offline policy with controlled social ablations and typed v4 intents."""
from fractions import Fraction

from civitas.contracts.decisions import HoldIntent, MessageDraft
from civitas.contracts.economic import (ConsumeIntent, CooperationIntent, CooperationPartnerProposal, EconomicCommand,
    EconomicDecisionBundle, LaborAcceptIntent, LaborApplyIntent, NetworkProposal, NetworkWithdrawal,
    CreditApplicationIntent, PurchaseIntent, QuoteIntent, WithdrawDepositIntent)
from civitas.core.rng import DrawAddress
from .closed_economy_demand import consumption_disclosure, consumption_message_id, demand_comparison
from .closed_economy_cooperation import trial_partner_result
from .closed_economy_introductions import INTRODUCTION_KIND, introduction_from
from .closed_economy_labor_outcomes import LABOR_ASSESSMENT, REHIRE_OFFER, SWITCH_OFFER, labor_assessments
from .closed_economy_partner import PARTNER_DRAW, PARTNER_OFFER, PARTNER_TRUST, select_partner
from .closed_economy_relations import (DELIVERY_OUTCOMES, appraise_social, assess_rehire,
                                      delivery_disclosure, outcome_root, social_context)
from .closed_economy_rules import ActionCapabilities, affordable_quantity, household_budget_minor


LABOR_DRAW = "labor_random_draw"


def labor_wage_rank(offer_id, score):
    # Unknown employers keep the wage model's symmetric prior.
    probability = Fraction(score["a"], score["a"] + score["b"]) if score is not None else Fraction(1, 2)
    return -probability, offer_id


def labor_random_draw(rng, config, actor, tick, offer_ids):
    address = DrawAddress(root_seed=config.root_seed, replica=config.replica, process="closed.labor.switch-control",
                          entity=actor, tick=tick, semantic_slot="offer-selection")
    rank = sorted(offer_ids)
    draw = rng.u64(address.process, actor, tick, address.semantic_slot)
    return {"address": address.model_dump(), "u64_hex": f"{draw:016x}",
            "candidate_offer_ids": rank, "selected_offer_id": rank[draw % len(rank)]}


class EconomicIntentCompiler:
    def compile(self, bundle, arrival_key):
        return [EconomicCommand(command_id="command:" + intent.intent_id,
            decision_id=bundle.decision_id, actor_id=bundle.actor_id,
            tick=bundle.tick, batch_id=bundle.batch_id,
            observation_version=bundle.observation_version,
            world_version=bundle.world_version, arrival_key=arrival_key,
            local_order_index=index, intent=intent)
            for index, intent in enumerate(bundle.economic_intents)]


class ClosedEconomyPolicy:
    policy_id = "closed_economy_rule"
    version = "1"

    def __init__(self, config, rng):
        self.config, self.rng = config, rng
        self.capabilities = ActionCapabilities.from_config(
            {"disabledActions": list(config.disabled_actions)})

    async def decide(self, observation, *, supplier_scores=None, integrate_peer=True):
        tick, actor = observation.visible_until.tick, observation.actor_id
        account_fact = observation.account_view[0]
        account = account_fact.value
        profile_fact = next(f for f in observation.market_view
                            if f.value.get("kind") in {"household_preferences", "firm_plan"})
        profile = profile_fact.value
        evidence = {account_fact.fact_id, profile_fact.fact_id}
        intents, drafts, patch = [], [], {}
        network_proposals = []
        prefix = f"intent:{tick}:{actor}"
        if profile["kind"] == "household_preferences":
            if self.config.banking and self.config.banking.credit_offer and self.capabilities.is_enabled("apply_credit"):
                offer = next((f for f in observation.market_view if f.value.get("kind") == "credit_offer"), None)
                if offer is not None:
                    intents.append(CreditApplicationIntent(intent_id=prefix + ":apply-credit",
                        bank_id=offer.value["bank"], offer_fact_id=offer.fact_id, amount_minor=offer.value["amount_minor"]))
                    evidence.add(offer.fact_id)
            notice = next((f for f in observation.market_view if f.value.get("kind") == "verified_withdrawal_delay"
                           and f.visible_at.tick == tick and "withdrawal_limit_minor" in f.value), None)
            if notice is not None and self.capabilities.is_enabled("withdraw_deposit"):
                intents.append(WithdrawDepositIntent(intent_id=prefix + ":withdraw-deposit",
                    bank_id=notice.value["bank"], notice_fact_id=notice.fact_id,
                    amount_minor=notice.value["withdrawal_limit_minor"]))
                evidence.add(notice.fact_id)
            option = next((f for f in observation.market_view if f.value.get("kind") == PARTNER_OFFER), None)
            if option is not None and not self.config.partner_choice.pin_incumbent:
                trust = next((f for f in observation.market_view if f.value.get("kind") == PARTNER_TRUST), None)
                candidate, result_ids, draw = select_partner(
                    self.config, actor, tick, observation.private_memory_refs, option.value, self.rng,
                    trust_scores=trust.value["scores"] if trust else None)
                if trust is not None and draw is None:
                    evidence.add(trust.fact_id)
                network_proposals.append(CooperationPartnerProposal(
                    option_id=option.fact_id, candidate_id=candidate, fee_limit_minor=option.value["fee_minor"]))
                evidence.add(option.fact_id)
                evidence.update(result_ids)
                if draw is not None:
                    patch[PARTNER_DRAW] = draw
            cooperation = next((f for f in observation.market_view if f.value.get("kind") == "cooperation_offer"), None)
            if cooperation is not None:
                settings = self.config.cooperation
                action = settings.actions[cooperation.value["seat"]]
                trial = (next((t for t in settings.trust_ranking.scheduled_trials if t.offer_tick == tick), None)
                         if settings.trust_ranking is not None else settings.comparison_trial)
                if trial is not None and tick == trial.offer_tick and cooperation.value["seat"] == 1:
                    action = trial.action
                if settings.followup is not None and tick == settings.followup.offer_tick:
                    action = cooperation.value.get("assigned_action", "cooperate")
                    if settings.followup.use_partner_result:
                        result = trial_partner_result(
                            observation.private_memory_refs, settings, cooperation.value["partner_id"])
                        if result is not None:
                            action = result.value["partner_action"]
                            evidence.add(result.fact_id)
                intents.append(CooperationIntent(intent_id=prefix + ":cooperation", opportunity_id=cooperation.fact_id,
                                                  action=action))
                evidence.add(cooperation.fact_id)
            vacancy = next((f for f in observation.market_view if f.value.get("kind") == "labor_vacancy"), None)
            if vacancy is not None and self.capabilities.is_enabled("apply_labor_vacancy"):
                intents.append(LaborApplyIntent(intent_id=prefix + ":labor", vacancy_id=vacancy.fact_id))
                evidence.add(vacancy.fact_id)
            offers = sorted((f for f in observation.market_view
                             if f.value.get("kind") in ("labor_offer", REHIRE_OFFER, SWITCH_OFFER)),
                            key=lambda f: f.value["kind"] == SWITCH_OFFER)
            if offers and self.capabilities.is_enabled("accept_labor_offer"):
                evidence.update(f.fact_id for f in offers)
                window = self.config.labor_transfer.rehire_at(tick)
                if window is not None and window.switch_only:
                    offers = [f for f in offers if f.value["kind"] == SWITCH_OFFER]
                    if not window.random_offer_choice:
                        offers.sort(key=lambda f: labor_wage_rank(
                            f.fact_id, assess_rehire(observation, f.value["employer_id"], None)[0]))
                if offers and window is not None and window.random_offer_choice:
                    draw = labor_random_draw(self.rng, self.config, actor, tick, [f.fact_id for f in offers])
                    patch[LABOR_DRAW] = draw
                    offers = [f for f in offers if f.fact_id == draw["selected_offer_id"]]
                for offer in offers:
                    if offer.value["kind"] != "labor_offer":
                        if offer.value["kind"] == SWITCH_OFFER and not window.allow_switch:
                            continue
                        _, blocked = assess_rehire(observation, offer.value["employer_id"],
                                                  window.minimum_trust_bps)
                        if blocked:
                            continue
                    intents.append(LaborAcceptIntent(intent_id=prefix + ":labor", offer_id=offer.fact_id,
                                                    max_switch_fee_minor=offer.value.get("switch_fee_minor")))
                    break
            reserve = profile["cash_reserve_minor"]
            if behavior := profile.get("behavior"):
                reserve += behavior["prudence_buffer_minor"]
            announcement = next((f for f in observation.market_view if f.value.get("kind") == "fiscal_announcement"), None)
            if announcement is not None and (precaution := announcement.value.get("precautionary_reserve_minor", 0)):
                reserve += precaution
                evidence.add(announcement.fact_id)
                patch["fiscal_precaution"] = {"reserve_minor": precaution, "evidence_id": announcement.fact_id}
            budget = household_budget_minor(account["available_cash_minor"], reserve, profile["basket_share_bps"])
            scores, used = appraise_social(observation, self.config, None if integrate_peer else set())
            ranked_scores = scores if supplier_scores is None else supplier_scores(scores)
            social_fact = social_context(observation)
            if social_fact is not None:
                evidence.add(social_fact.fact_id)
            evidence.update(x["message_id"] for x in used)
            quotes = [f for f in observation.market_view if f.value.get("kind") == "goods_quote"]
            ranked = []
            for fact in quotes:
                q = fact.value
                effective = (q["unit_price_minor"] * 10000
                    + (10000 - ranked_scores.get(q["seller_id"], 10000))
                    * self.config.reliability_penalty_minor)
                tie = self.rng.u64("closed.choice", actor, tick, "seller:" + q["seller_id"])
                ranked.append((effective, tie, q["seller_id"], fact))
            ranked.sort(key=lambda x: x[:3])
            own_units = account["goods_units"]
            target = profile["target_units"]
            demand = {}
            if self.config.demand is not None:
                demand = demand_comparison(observation, self.config.demand)
                target += demand["peer_uplift_units"]
                if demand["peer_uplift_units"]:
                    evidence.update(demand["consumption_message_ids"])
                for fact in observation.private_memory_refs:
                    receipt = fact.value
                    if receipt.get("outcome") != "consumed" or receipt.get("tick") != tick-1:
                        continue
                    body = consumption_disclosure(receipt)
                    evidence.add(fact.fact_id)
                    for neighbor in social_fact.value["neighbors"]:
                        drafts.append(MessageDraft(message_id=consumption_message_id(actor, neighbor, body["root_claim_id"]),
                                                   recipient_id=neighbor, body=body))
            old_consumption = min(target, own_units)
            if old_consumption and self.capabilities.is_enabled("consume"):
                intents.append(ConsumeIntent(intent_id=prefix + ":consume-held",
                                             quantity_units=old_consumption))
            chosen, quantity = None, 0
            if self.capabilities.is_enabled("purchase"):
                for _, _, seller, fact in ranked:
                    q = fact.value
                    quantity = affordable_quantity(target, own_units, q["available_units"],
                                                   budget, q["unit_price_minor"])
                    if quantity:
                        chosen = seller
                        evidence.add(fact.fact_id)
                        intents.append(PurchaseIntent(intent_id=prefix + ":purchase",
                            seller_id=seller, quote_id=q["quote_id"],
                            quantity_units=quantity, max_unit_price_minor=q["unit_price_minor"]))
                        if self.capabilities.is_enabled("consume"):
                            intents.append(ConsumeIntent(intent_id=prefix + ":consume-purchase",
                                                         quantity_units=quantity))
                        break
            if social_fact is not None and self.config.social_mode != "none":
                shared = set(social_fact.value["shared_roots"])
                for fact in observation.private_memory_refs:
                    receipt = fact.value
                    if (receipt.get("outcome") not in DELIVERY_OUTCOMES
                            or receipt.get("actor_id") != actor):
                        continue
                    root = outcome_root(receipt["command_id"])
                    if root in shared:
                        continue
                    evidence.add(fact.fact_id)
                    for neighbor in social_fact.value["neighbors"]:
                        drafts.append(MessageDraft(
                            message_id=f"experience:{actor}:{neighbor}:{root}",
                            recipient_id=neighbor, body=delivery_disclosure(
                                receipt["command_id"], receipt["counterparty_id"], receipt["outcome"],
                                allow_introduction=self.config.introductions,
                                allow_follow=self.config.link_budget_per_tick is not None)))
                if self.config.introductions:
                    for neighbor in social_fact.value["neighbors"]:
                        for message in observation.delivered_messages:
                            introduction = introduction_from(message, neighbor, tick)
                            if introduction is not None:
                                evidence.add(message.message_id)
                                drafts.append(introduction.draft())
                                break
                if self.config.link_budget_per_tick is not None:
                    for message in observation.delivered_messages:
                        if message.body.get("kind") == INTRODUCTION_KIND and message.body.get("allow_follow") is True:
                            evidence.add(message.message_id)
                            network_proposals.append(NetworkProposal(introduction_message_id=message.message_id,
                                candidate_id=message.body["candidate_id"],
                                fee_limit_minor=social_fact.value.get("link_fee_minor")))
                if self.config.link_withdrawal_tick == tick:
                    for edge in social_fact.value["withdrawable_links"]:
                        network_proposals.append(NetworkWithdrawal(edge_delta_id=edge["edge_delta_id"]))
            patch["closed_choice"] = {"chosen_seller": chosen,
                "target_units": target, "ordered_units": quantity,
                "budget_minor": budget, "cash_minor": account["cash_minor"],
                "effective_scores_bps": scores,
                "consumed_roots": [x["root_claim_id"] for x in used],
                "quoted_prices": {f.value["seller_id"]: f.value["unit_price_minor"] for f in quotes},
                **demand}
        elif self.capabilities.is_enabled("quote"):
            current = profile["unit_price_minor"]
            sold = profile["last_sold_units"]
            capacity = profile["capacity_units"]
            if capacity and sold >= capacity and account["goods_units"] <= capacity:
                price = min(self.config.max_price_minor, current + self.config.price_adjustment_minor)
            elif sold * 2 < capacity and account["goods_units"] > capacity:
                price = max(self.config.min_price_minor, current - self.config.price_adjustment_minor)
            else:
                price = current
            intents.append(QuoteIntent(intent_id=prefix + ":quote",
                quote_id=f"quote:{tick + 1}:{actor}", unit_price_minor=price,
                ttl_ticks=self.config.ticks + 1))
        if "window_purchase_units" in profile and self.capabilities.is_enabled("purchase"):
            supplier = next((f for f in observation.market_view if f.value.get("kind") == "goods_quote"
                             and f.value["seller_id"] != actor), None)
            if supplier is not None:
                intents.append(PurchaseIntent(intent_id=prefix + ":window-purchase",
                    seller_id=supplier.value["seller_id"], quote_id=supplier.value["quote_id"],
                    max_unit_price_minor=supplier.value["unit_price_minor"], quantity_units=profile["window_purchase_units"]))
                evidence.add(supplier.fact_id)
        if not intents:
            intents.append(HoldIntent(intent_id=prefix + ":hold"))
        if self.config.labor_transfer and self.config.labor_transfer.outcome_evidence:
            assessment = labor_assessments(observation, newly_visible=True)
            if assessment:
                patch[LABOR_ASSESSMENT] = list(assessment.values())
                evidence.update(assessment)
        return EconomicDecisionBundle(tick=tick,
            decision_id=f"decision:{tick}:{actor}", actor_id=actor,
            observation_id=observation.observation_id, batch_id=observation.batch_id,
            observation_version=observation.observation_version,
            world_version=observation.world_version, policy_id=self.policy_id,
            policy_version=self.version, economic_intents=intents,
            message_drafts=drafts, belief_patch=patch, evidence_ids=sorted(evidence),
            network_proposals=network_proposals)
