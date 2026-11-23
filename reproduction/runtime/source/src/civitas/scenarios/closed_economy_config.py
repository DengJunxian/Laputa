"""Explicit synthetic C/D experiment configuration; no calibrated trait claims."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from civitas.contracts.households import HouseholdPopulation
from civitas.economy.fiscal import FiscalPolicy
from civitas.economy.banking import BankingConfig, actors_for
from civitas.contracts.base import Id, NonNegative, Positive, Record
from civitas.contracts.economic import Amount, CooperationAction, PositiveAmount

Bps = Annotated[int, Field(ge=0, le=10000)]


class PayrollWindowConfig(Record):
    purchase_units: Positive | None = None
    reserve_two_wages: bool = Field(default=False, exclude_if=lambda value: not value)
    cancel_second_wage_slot: Literal[1, 2] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_cancellation(self):
        if self.cancel_second_wage_slot is not None and not self.reserve_two_wages:
            raise ValueError("Second-wage cancellation requires two reserved wages")
        return self


class SharedPayrollConfig(Record):
    second_candidate_indices: list[NonNegative] = Field(min_length=1)
    cancel_before_start_slot: Literal[1, 2] | None = None
    payroll_window: PayrollWindowConfig | None = Field(default=None, exclude_if=lambda value: value is None)


class RehireConfig(Record):
    offer_tick: Positive
    minimum_trust_bps: Bps | None = Field(default=None, exclude_if=lambda value: value is None)
    alternative_employer_index: NonNegative | None = Field(default=None, exclude_if=lambda value: value is None)
    additional_alternative_employer_index: NonNegative | None = Field(default=None, exclude_if=lambda v: v is None)
    switch_fee_minor: Amount = Field(default=0, exclude_if=lambda value: value == 0)
    allow_switch: bool = Field(default=True, exclude_if=lambda value: value)
    random_offer_choice: bool = Field(default=False, exclude_if=lambda value: not value)

    @property
    def alternative_indices(self):
        return tuple(index for index in (self.alternative_employer_index, self.additional_alternative_employer_index)
                     if index is not None)

    @property
    def switch_only(self):
        return self.additional_alternative_employer_index is not None


class LaborTransferConfig(Record):
    worker_index: NonNegative
    employer_index: NonNegative
    offer_tick: Annotated[int, Field(ge=2)]
    duration_ticks: Positive
    min_previous_sales_units: Positive
    additional_candidate_indices: list[NonNegative] = Field(default_factory=list, exclude_if=lambda value: not value)
    shared_payroll: SharedPayrollConfig | None = Field(default=None, exclude_if=lambda value: value is None)
    outcome_evidence: bool = Field(default=False, exclude_if=lambda value: not value)
    performance_trust: bool = Field(default=False, exclude_if=lambda value: not value)
    rehire: RehireConfig | None = Field(default=None, exclude_if=lambda value: value is None)
    second_rehire: RehireConfig | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def rehire_windows(self):
        return tuple(window for window in (self.rehire, self.second_rehire) if window is not None)

    def rehire_at(self, tick):
        return next((window for window in self.rehire_windows if window.offer_tick == tick), None)

    @model_validator(mode="after")
    def trust_requires_outcomes(self):
        if self.performance_trust and not self.outcome_evidence:
            raise ValueError("Wage performance trust requires labor outcome evidence")
        if self.rehire is not None:
            if self.rehire.switch_only:
                raise ValueError("The additional alternative employer is available only in the second window")
            if self.rehire.random_offer_choice:
                raise ValueError("Random offer choice applies only to the second rehire window")
            if self.additional_candidate_indices or self.shared_payroll is not None:
                raise ValueError("Rehire requires a single directed predecessor contract")
            if self.rehire.offer_tick <= self.offer_tick + self.duration_ticks:
                raise ValueError("Rehire offer requires the original contract to have ended")
        if self.second_rehire is not None:
            # shortcut: two windows with the same counterparties, extend for a repeated market.
            if (self.rehire is None or self.rehire.alternative_employer_index is None
                    or self.second_rehire.alternative_employer_index != self.rehire.alternative_employer_index):
                raise ValueError("Both rehire windows require the same distinct employers")
            if self.second_rehire.offer_tick < self.rehire.offer_tick + 2:
                raise ValueError("The second rehire window requires the first to have ended")
            if self.second_rehire.switch_only and (
                    not self.performance_trust or not self.second_rehire.allow_switch
                    or self.second_rehire.minimum_trust_bps is not None):
                raise ValueError("A switch-only window requires wage scoring, switching enabled and no trust gate")
            if self.second_rehire.random_offer_choice and (
                    not self.second_rehire.allow_switch or self.second_rehire.minimum_trust_bps is not None):
                raise ValueError("Random offer choice requires switching enabled and no trust gate")
        if not self.performance_trust and any(w.minimum_trust_bps is not None for w in self.rehire_windows):
            raise ValueError("A rehire trust threshold requires wage performance trust")
        return self


class CooperationComparisonTrial(Record):
    alternate_index: NonNegative
    offer_tick: Positive
    action: CooperationAction


class CooperationTrustRanking(Record):
    enabled: bool
    trials: list[CooperationComparisonTrial] = Field(min_length=6, max_length=6)
    incumbent_trials: list[CooperationComparisonTrial] = Field(default_factory=list, max_length=3, exclude_if=lambda value: not value)

    @property
    def scheduled_trials(self):
        return (*self.trials, *self.incumbent_trials)


class CooperationPartnerChoice(Record):
    alternate_index: NonNegative
    fee_minor: PositiveAmount
    random_choice: bool
    comparison_trial: CooperationComparisonTrial | None = Field(default=None, exclude_if=lambda value: value is None)
    trust_ranking: CooperationTrustRanking | None = Field(default=None, exclude_if=lambda value: value is None)
    pin_incumbent: bool = Field(default=False, exclude_if=lambda value: not value)

    @property
    def alternate_indices(self):
        if self.trust_ranking is not None:
            return tuple(sorted({t.alternate_index for t in self.trust_ranking.trials}))
        if self.comparison_trial is None:
            return (self.alternate_index,)
        return (self.alternate_index, self.comparison_trial.alternate_index)


class CooperationFollowup(Record):
    offer_tick: Positive
    use_partner_result: bool
    partner_choice: CooperationPartnerChoice | None = Field(default=None, exclude_if=lambda value: value is None)
    partner_outcome_rule: Literal["cumulative", "recent"] | None = Field(default=None, exclude_if=lambda value: value is None)
    partner_noise_bps: Bps | None = Field(default=None, exclude_if=lambda value: value is None)


class CooperationConfig(Record):
    offer_tick: Positive
    household_indices: list[NonNegative] = Field(min_length=2, max_length=2)
    actions: list[CooperationAction] = Field(min_length=2, max_length=2)
    followup: CooperationFollowup | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def comparison_trial(self):
        choice = self.followup.partner_choice if self.followup is not None else None
        return choice.comparison_trial if choice is not None else None

    @property
    def trust_ranking(self):
        choice = self.followup.partner_choice if self.followup is not None else None
        return choice.trust_ranking if choice is not None else None


class SupplierCognition(Record):
    mode: Literal['L', 'M']
    source: Literal['synthetic_test', 'archived_response', 'live'] = 'synthetic_test'
    household_indices: list[NonNegative] = Field(default_factory=lambda: [0], min_length=1, max_length=8)
    ticks: list[Positive] = Field(min_length=1, max_length=16)
    social_weight_bps: Bps = 0
    reflect: bool = True
    retrieve_memory: bool = Field(default=True, exclude_if=lambda value: value)

    @model_validator(mode='after')
    def unique_slots_and_owner(self):
        for values in (self.household_indices, self.ticks):
            if values != sorted(set(values)):
                raise ValueError('Cognition slots must be unique and sorted')
        if (self.mode == 'M') != bool(self.social_weight_bps):
            raise ValueError('Only M takes a positive numeric social weight')
        return self


class SecurityMarketSettings(Record):
    shock_tick: Positive = 2
    initial_price_minor: PositiveAmount = 100
    fundamental_drop_minor: PositiveAmount = 5
    signal_gap_minor: PositiveAmount = 12
    maker_cash_minor: Amount = 3000
    selective_exposure: bool = True
    herding: bool = True
    binding_maker_bid: Literal["prior_close", "fundamental"] | None = Field(
        default=None, exclude_if=lambda value: value is None)


class SecurityFeedbackSettings(Record):
    holder_indices: list[NonNegative] = Field(min_length=1)
    shock_enabled: bool = True
    wealth_observed: bool = True


class ReferenceLossSettings(Record):
    schema_version: Literal[1] = 1
    lambda_bps: Annotated[int, Field(ge=10000)] = 10000
    peer_disclosure: bool = True


JOINT_CHOICE_TASK = 'joint_policy_choice'


class JointPolicySeat(Record):
    actor_id: Id
    strategy: Literal['behavioral', 'structured']
    ticks: list[Positive] = Field(min_length=1, max_length=16)
    prudence_minor: Amount = 0


class JointPolicySettings(Record):
    source: Literal['synthetic_test', 'archived_response', 'live'] = 'synthetic_test'
    seats: list[JointPolicySeat] = Field(min_length=1, max_length=3)


class ConsumptionDemandSettings(Record):
    peer_uplift_units: Literal[0, 1] = 0
    hiring_adapts: bool = True
    fixed_sales_units: NonNegative = 0
    initially_unemployed_indices: list[NonNegative] = Field(default_factory=list)


class LaborIncomeShock(Record):
    tick: Positive
    household_indices: list[NonNegative] = Field(min_length=1)
    work_minutes: Annotated[int, Field(ge=1, lt=480)]


class LaborShiftIntervention(Record):
    schema_version: Literal[1] = 1
    kind: Literal['labor_income_shock']
    shock: LaborIncomeShock


class OfflineChoiceSeat(Record):
    household_index: NonNegative
    source: Literal['synthetic_test', 'archived_response'] = 'archived_response'


class DisclosureSeat(OfflineChoiceSeat):
    tick: Annotated[int, Field(ge=2)]


class ClosedEconomyConfig(Record):
    schema_version: Literal[1] = 1
    scenario_id: Literal["closed_economy"] = "closed_economy"
    scenario_version: Literal["1"] = "1"
    ticks: Positive = 8
    stopping_hazard_bps: Annotated[int, Field(ge=1, le=10000)] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    cooperation: CooperationConfig | None = Field(default=None, exclude_if=lambda value: value is None)
    offline_cooperation_action: Literal['cooperate', 'defect'] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    banking: BankingConfig | None = Field(default=None, exclude_if=lambda value: value is None)
    fiscal: FiscalPolicy | None = Field(default=None, exclude_if=lambda value: value is None)
    securities: SecurityMarketSettings | None = Field(default=None, exclude_if=lambda value: value is None)
    security_feedback: SecurityFeedbackSettings | None = Field(default=None, exclude_if=lambda value: value is None)
    reference_loss: ReferenceLossSettings | None = Field(default=None, exclude_if=lambda value: value is None)
    joint_policy: JointPolicySettings | None = Field(default=None, exclude_if=lambda value: value is None)
    demand: ConsumptionDemandSettings | None = Field(default=None, exclude_if=lambda value: value is None)
    population_input: HouseholdPopulation | None = Field(default=None, exclude_if=lambda value: value is None)
    supplier_cognition: SupplierCognition | None = Field(default=None, exclude_if=lambda value: value is None)
    withdrawal_cognition: OfflineChoiceSeat | None = Field(default=None, exclude_if=lambda value: value is None)
    credit_cognition: OfflineChoiceSeat | None = Field(default=None, exclude_if=lambda value: value is None)
    labor_cognition: OfflineChoiceSeat | None = Field(default=None, exclude_if=lambda value: value is None)
    cooperation_cognition: OfflineChoiceSeat | None = Field(default=None, exclude_if=lambda value: value is None)
    disclosure_cognition: DisclosureSeat | None = Field(default=None, exclude_if=lambda value: value is None)
    archive_observations: bool = Field(default=False, exclude_if=lambda enabled: not enabled)
    observation_archive_format: Literal["observe.v1", "observe.v2"] = Field(
        default="observe.v1", exclude_if=lambda value: value == "observe.v1")
    root_seed: NonNegative = 7
    replica: NonNegative = 0
    concurrency: Positive = 1
    households: Annotated[int, Field(ge=2, le=10000)] = 8
    firms: Annotated[int, Field(ge=2, le=100)] = 2
    social_mode: Literal["adaptive", "static", "none"] = "adaptive"
    fault_tick: Positive | None = 3
    fault_seller_index: NonNegative = 0
    initial_household_cash_minor: NonNegative = 60
    cash_jitter_minor: NonNegative = 10
    initial_firm_cash_minor: NonNegative = 400
    wage_minor: Positive = 12
    output_per_worker: Positive = 4
    initial_price_minor: Positive = 10
    firm_price_gap_minor: NonNegative = 2
    min_price_minor: Positive = 6
    max_price_minor: Positive = 30
    price_adjustment_minor: Positive = 1
    cash_reserve_minor: NonNegative = 5
    target_units_min: Positive = 1
    target_units_max: Positive = 2
    basket_share_min_bps: Bps = 5000
    basket_share_max_bps: Bps = 7500
    trust_learning_bps: Bps = 5000
    peer_weight_bps: Bps = 5000
    reliability_penalty_minor: NonNegative = 10
    network_neighbors: NonNegative = 2
    attention_limit: Positive = 8
    introductions: bool = Field(default=False, exclude_if=lambda value: not value)
    link_budget_per_tick: Literal[0, 1] | None = Field(default=None, exclude_if=lambda value: value is None)
    link_fee_minor: Amount = Field(default=0, exclude_if=lambda value: value == 0)
    link_withdrawal_tick: Positive | None = Field(default=None, exclude_if=lambda value: value is None)
    disabled_actions: list[str] = Field(default_factory=list)
    labor_transfer: LaborTransferConfig | None = Field(default=None, exclude_if=lambda value: value is None)
    labor_income_shock: LaborIncomeShock | None = Field(default=None, exclude_if=lambda value: value is None)

    def household_contacts(self, index):
        if self.population_input is not None:
            behavior = self.population_input.household_profiles[index].behavior
            if behavior is not None:
                return list(behavior.contact_ids)
        return [f'household:{(index + offset + 1) % self.households:04d}'
                for offset in range(self.network_neighbors)]

    @property
    def consumption_peer_uplift(self):
        if self.demand is not None:
            return self.demand.peer_uplift_units
        if self.reference_loss is not None and self.reference_loss.peer_disclosure and self.social_mode != "none":
            return 1
        return None

    @property
    def structured_sources(self):
        sources = {settings.source for settings in (
            self.supplier_cognition, self.withdrawal_cognition, self.labor_cognition,
            self.cooperation_cognition, self.disclosure_cognition, self.credit_cognition,
        ) if settings is not None}
        if self.joint_model_slots:
            sources.add(self.joint_policy.source)
        return sources

    @property
    def joint_model_slots(self):
        if self.joint_policy is None:
            return ()
        return tuple((JOINT_CHOICE_TASK, seat.actor_id, tick)
                     for seat in self.joint_policy.seats if seat.strategy == 'structured' for tick in seat.ticks)

    @property
    def cognition_mode(self):
        return next(iter(self.structured_sources), 'rule' if self.offline_cooperation_action is None else 'synthetic_test')

    @property
    def choice_slots(self):
        slots = []
        if self.withdrawal_cognition is not None:
            slots.append(('cd_withdrawal', self.withdrawal_cognition.household_index, self.banking.run_notice.tick))
        if self.credit_cognition is not None:
            slots.append(('cd_credit', self.credit_cognition.household_index, self.banking.credit_offer.tick))
        if self.labor_cognition is not None:
            slots.append(('cd_labor', self.labor_cognition.household_index, self.labor_transfer.offer_tick))
        if self.cooperation_cognition is not None:
            seat = self.cooperation_cognition.household_index
            slots.append(('cd_cooperation_choice', seat, self.cooperation.offer_tick))
            if self.cooperation.followup is not None:
                slots.append(('cd_cooperation_choice', seat, self.cooperation.followup.offer_tick))
        if self.disclosure_cognition is not None:
            settings = self.disclosure_cognition
            slots.append(('cd_disclosure_choice', settings.household_index, settings.tick))
        return slots

    @property
    def partner_choice(self):
        followup = self.cooperation.followup if self.cooperation is not None else None
        return followup.partner_choice if followup is not None else None

    @model_validator(mode="after")
    def compatible_configuration(self):
        if not self.archive_observations and self.observation_archive_format != "observe.v1":
            raise ValueError("An indexed observation format requires observation archiving")
        if self.reference_loss is not None and self.security_feedback is None:
            raise ValueError("Reference loss requires the joint economy's security wealth facts")
        if self.joint_policy is not None:
            policy = self.joint_policy
            if self.security_feedback is None or not self.archive_observations or any((
                    self.supplier_cognition, self.withdrawal_cognition, self.labor_cognition,
                    self.cooperation_cognition, self.disclosure_cognition, self.credit_cognition,
                    self.offline_cooperation_action)):
                raise ValueError('Joint policy requires archived observations in the joint economy and owns its seats')
            households = {f'household:{i:04d}' for i in range(self.households)}
            firms = {f'firm:{i:02d}' for i in range(self.firms)}
            actors = {seat.actor_id for seat in policy.seats}
            if (len(actors) != len(policy.seats) or not actors <= households | firms
                    or len(actors & households) > 2 or len(actors & firms) > 1
                    or any(seat.ticks != sorted(set(seat.ticks)) or seat.ticks[-1] > self.ticks
                           or (seat.strategy == 'behavioral' and seat.actor_id not in households)
                           or (seat.strategy != 'behavioral' and seat.prudence_minor)
                           for seat in policy.seats)
                    or (not self.joint_model_slots and policy.source != 'synthetic_test')):
                raise ValueError('Joint policy requires unique in-range seats for up to two households and one firm')
        if self.security_feedback is not None:
            holders = self.security_feedback.holder_indices
            security = self.securities
            if (security is None or security.shock_tick != 2 or self.ticks < 5
                    or security.initial_price_minor <= security.fundamental_drop_minor
                    or not 8 <= self.households <= 16 or self.firms != 2
                    or self.banking is None or len(self.banking.initial_bank_cash) != 2 or self.fiscal is None
                    or any((self.demand, self.cooperation, self.population_input, self.labor_income_shock,
                            self.stopping_hazard_bps))
                    or (self.cognition_mode != "rule" and self.joint_policy is None)
                    or len(set(holders)) != len(holders) or len(holders) >= self.households
                    or max(holders) >= self.households):
                raise ValueError("Security feedback requires the bounded offline joint economy and distinct household holders")
        elif self.securities is not None:
            security = self.securities
            if (any((self.banking, self.fiscal, self.cooperation, self.labor_transfer, self.demand,
                     self.supplier_cognition, self.withdrawal_cognition, self.labor_cognition, self.credit_cognition,
                    self.population_input, self.offline_cooperation_action, self.cooperation_cognition, self.disclosure_cognition,
                     self.link_budget_per_tick, self.labor_income_shock))
                    or self.disabled_actions or self.stopping_hazard_bps is not None
                    or not 1 < security.shock_tick < self.ticks
                    or security.initial_price_minor <= security.fundamental_drop_minor + 1):
                raise ValueError("Security sessions require a pre-shock and later window and an exclusive funded asset")
        if self.demand is not None:
            unemployed = self.demand.initially_unemployed_indices
            if (len(set(unemployed)) != len(unemployed) or any(i >= self.households for i in unemployed)
                    or self.banking is not None or self.cooperation is not None or self.cognition_mode != "rule"
                    or self.social_mode == "none" or self.link_budget_per_tick is not None):
                raise ValueError("Consumption demand requires a fixed disclosed network and known offline labor inputs")
        if self.banking is not None:
            actors_for(self.banking, [*(f'household:{i:04d}' for i in range(self.households)),
                                     *(f'firm:{i:02d}' for i in range(self.firms))])
            if any(op.tick > self.ticks for op in self.banking.operations):
                raise ValueError('Banking operations must be within the run horizon')
            if self.banking.credit_offer is not None and self.banking.credit_offer.tick >= self.ticks:
                raise ValueError('A credit application requires a subsequent funding period')
            if self.banking.run_notice is not None and self.banking.run_notice.tick >= self.ticks:
                raise ValueError('Bank notice needs a later response boundary')
            if self.banking.requires_treasury and self.fiscal is None:
                raise ValueError('Funded bank policy requires a fiscal treasury')
            if self.banking.treasury_support is not None and self.banking.treasury_support.maturity_tick > self.ticks:
                raise ValueError('Treasury support maturity must fall within the run horizon')
        if self.fiscal is not None and (self.fiscal.effective_tick > self.ticks or any(
                i >= self.households for i in (*self.fiscal.audience, *self.fiscal.transfer_recipients))):
            raise ValueError('Fiscal policy requires household recipients and a period of legal effect')
        if self.population_input is not None:
            rows = self.population_input.household_profiles
            if len(rows) != self.households:
                raise ValueError('Population input requires one profile per household')
            households = {f'household:{i:04d}' for i in range(self.households)}
            firms = {f'firm:{i:02d}' for i in range(self.firms)}
            shock = self.labor_income_shock
            for i, row in enumerate(rows):
                behavior = row.behavior
                if behavior is None:
                    continue
                if behavior.regular_wage_minor < self.wage_minor:
                    raise ValueError('Heterogeneous pay must fund the standard verified production cost')
                contacts = set(behavior.contact_ids)
                if not contacts <= households or f'household:{i:04d}' in contacts:
                    raise ValueError('Behavior contacts must name other registered households')
                if set(behavior.supplier_priors_bps) != firms:
                    raise ValueError('Behavior priors must cover exactly the registered suppliers')
                if shock and i in shock.household_indices and behavior.regular_wage_minor * shock.work_minutes % 480:
                    raise ValueError('Heterogeneous wages must prorate exactly under the registered work shift')
        if len(self.structured_sources) > 1 or (self.structured_sources and self.offline_cooperation_action is not None):
            raise ValueError('Structured slots must share an execution source')
        if self.cooperation_cognition is not None:
            if (self.cooperation is None or not self.archive_observations or
                    self.cooperation_cognition.household_index not in self.cooperation.household_indices):
                raise ValueError('Cooperation cognition requires an authorized recorded cooperation seat')
        disclosure = self.disclosure_cognition
        if disclosure is not None and (self.social_mode == 'none' or not self.archive_observations
                                       or disclosure.household_index >= self.households or disclosure.tick >= self.ticks):
            raise ValueError('Disclosure cognition requires a recorded social seat and a later delivery period')
        supplier = self.supplier_cognition
        if supplier is not None:
            if self.offline_cooperation_action is not None or supplier.household_indices[-1] >= self.households or supplier.ticks[-1] > self.ticks:
                raise ValueError('Supplier cognition requires distinct registered household/tick slots')
            if supplier.mode == 'M' and self.social_mode != 'adaptive':
                raise ValueError('M requires adaptive numeric social appraisal')
        if self.credit_cognition is not None:
            offer = self.banking.credit_offer if self.banking is not None else None
            if (offer is None or offer.actor != f'household:{self.credit_cognition.household_index:04d}'
                    or not self.archive_observations or 'apply_credit' in self.disabled_actions):
                raise ValueError('Offline credit requires a recorded offer to the configured household')
        if self.withdrawal_cognition is not None:
            notice = self.banking.run_notice if self.banking is not None else None
            seat = self.withdrawal_cognition.household_index
            if (notice is None or notice.response_mode != 'actor' or not self.archive_observations
                    or seat >= self.households or f'household:{seat:04d}' not in notice.withdrawal_by_recipient
                    or 'withdraw_deposit' in self.disabled_actions):
                raise ValueError('Offline withdrawal requires one recorded, authorized actor-notice slot')
        if self.labor_cognition is not None:
            transfer = self.labor_transfer
            if (transfer is None or not transfer.additional_candidate_indices or transfer.shared_payroll
                    or not self.archive_observations or self.labor_cognition.household_index >= self.households
                    or 'apply_labor_vacancy' in self.disabled_actions):
                raise ValueError('Offline labor requires one recorded competitive vacancy and a registered seat')
        # shortcut: one model decision in a standalone round; extend when repeated model slots are registered.
        if self.offline_cooperation_action is not None and (self.cooperation is None or self.cooperation.followup is not None):
            raise ValueError('Offline cooperation requires a single configured round')
        if self.cooperation is not None:
            indices = self.cooperation.household_indices
            if len(set(indices)) != 2 or max(indices) >= self.households or self.cooperation.offer_tick >= self.ticks:
                raise ValueError("Cooperation requires distinct eligible households and a remaining settlement tick")
            followup = self.cooperation.followup
            if followup is not None and not self.cooperation.offer_tick + 2 <= followup.offer_tick < self.ticks:
                raise ValueError("Follow-up must allow the first settlement to commit and leave a settlement tick")
            assigned_outcome = followup is not None and followup.partner_outcome_rule is not None
            if assigned_outcome and (followup.use_partner_result or self.cooperation.trust_ranking is None):
                raise ValueError("A scripted partner outcome requires trust trials and independent follow-up actions")
            if followup and followup.partner_noise_bps is not None and (
                    not assigned_outcome or not self.cooperation.trust_ranking.incumbent_trials):
                raise ValueError("Continuation noise requires scripted outcomes and incumbent trials")
            if self.partner_choice is not None:
                focal, incumbent = indices
                def edge(source, target):
                    return f'household:{target:04d}' in self.household_contacts(source)

                for alternate in self.partner_choice.alternate_indices:
                    anchor = (alternate - 1) % self.households
                    if (alternate >= self.households or len({focal, incumbent, anchor, alternate}) != 4
                            or not edge(focal, incumbent) or not edge(anchor, alternate)
                            or edge(focal, alternate) or edge(anchor, incumbent)):
                        raise ValueError("Partner exchange requires two existing and two absent network edges")
                trial = self.cooperation.comparison_trial
                if trial is not None and (len(set(self.partner_choice.alternate_indices)) != 2
                                          or trial.offer_tick + 2 > self.cooperation.offer_tick):
                    raise ValueError("Partner ranking needs two distinct alternates and separated trial settlements")
                ranking = self.cooperation.trust_ranking
                if self.partner_choice.pin_incumbent and (
                        not ranking or not ranking.incumbent_trials or self.partner_choice.random_choice):
                    raise ValueError("A static graph requires an incumbent history and no random exchange")
                if ranking is not None:
                    ticks = [t.offer_tick for t in ranking.scheduled_trials]
                    candidates = self.partner_choice.alternate_indices
                    if (trial is not None or len(candidates) != 2
                            or self.partner_choice.alternate_index not in candidates
                            or any(sum(t.alternate_index == a for t in ranking.trials) != 3 for a in candidates)
                            or any(right - left < 2 for left, right in zip(ticks, ticks[1:]))
                            or ticks[-1] + 3 > followup.offer_tick):
                        raise ValueError("Trust ranking needs six separated trials and three results per candidate")
                    if ranking.incumbent_trials and (not assigned_outcome or len(ranking.incumbent_trials) != 3
                            or any(t.alternate_index != incumbent or t.action == "decline" for t in ranking.incumbent_trials)):
                        raise ValueError("A fixed-partner comparison requires three binary trials for the incumbent")
                    if assigned_outcome:
                        histories = {tuple(t.action for t in ranking.trials if t.alternate_index == a)
                                     for a in candidates}
                        if histories != {("cooperate", "cooperate", "defect"), ("defect", "defect", "cooperate")}:
                            raise ValueError("Scripted partner outcomes need opposing cumulative and recent signals")
                if (self.link_budget_per_tick != 1 or self.introductions or self.link_withdrawal_tick is not None
                        or self.link_fee_minor or followup.offer_tick < self.cooperation.offer_tick + 3
                        or not (followup.use_partner_result or assigned_outcome)):
                    raise ValueError("Partner choice requires an isolated link slot and a mature result window")
        if self.firms > self.households:
            raise ValueError("Every initial firm needs at least one employed household")
        if self.fault_seller_index >= self.firms:
            raise ValueError("Unknown fault seller index")
        if self.initial_household_cash_minor < self.cash_jitter_minor:
            raise ValueError("Cash jitter may not produce negative initial cash")
        if self.wage_minor % self.output_per_worker:
            raise ValueError("Integer labor recipe requires wage divisible by output per worker")
        shock = self.labor_income_shock
        if shock is not None and (shock.tick > self.ticks
                or len(set(shock.household_indices)) != len(shock.household_indices)
                or max(shock.household_indices) >= self.households
                or any(value * shock.work_minutes % 480 for value in (self.wage_minor, self.output_per_worker))):
            raise ValueError("Income shock requires distinct in-range households and an integral wage and production recipe")
        if not (self.min_price_minor <= self.initial_price_minor
                and self.initial_price_minor + (self.firms - 1) * self.firm_price_gap_minor
                <= self.max_price_minor):
            raise ValueError("Initial quotes must lie within the declared price bounds")
        if self.target_units_min > self.target_units_max:
            raise ValueError("Inverted household target bounds")
        if self.basket_share_min_bps > self.basket_share_max_bps:
            raise ValueError("Inverted basket-share bounds")
        if self.network_neighbors >= self.households:
            raise ValueError("A household cannot follow itself")
        if self.link_budget_per_tick is not None and not self.introductions and self.partner_choice is None:
            raise ValueError("Link proposals require evidence-backed introductions")
        if self.link_fee_minor and self.link_budget_per_tick is None:
            raise ValueError("Link fees require a configured link budget")
        if self.link_withdrawal_tick is not None and (
                self.link_budget_per_tick is None or self.link_withdrawal_tick >= self.ticks):
            raise ValueError("Withdrawal requires a communication graph and a remaining boundary")
        if len(set(self.disabled_actions)) != len(self.disabled_actions):
            raise ValueError("Duplicate disabled action")
        transfer = self.labor_transfer
        if transfer:
            for offer_window in transfer.rehire_windows:
                if offer_window.offer_tick >= self.ticks:
                    raise ValueError("Rehire requires a remaining work boundary")
                alternatives = offer_window.alternative_indices
                if ((not alternatives and offer_window.switch_fee_minor)
                        or len(set(alternatives)) != len(alternatives)
                        or any(index >= self.firms or index == transfer.employer_index for index in alternatives)):
                    raise ValueError("A labor switch requires a distinct alternative employer")
            candidates = [transfer.worker_index, *transfer.additional_candidate_indices]
            windows = 0
            if transfer.shared_payroll is not None:
                candidates.extend(transfer.shared_payroll.second_candidate_indices)
                window = transfer.shared_payroll.payroll_window
                windows = int(window is not None)
                if window is not None and window.reserve_two_wages and transfer.duration_ticks != 2:
                    raise ValueError("Two reserved wages require a two-period labor term")
            if (len(set(candidates)) != len(candidates) or transfer.employer_index >= self.firms
                    or any(i >= self.households or i % self.firms == transfer.employer_index for i in candidates)
                    or transfer.offer_tick + transfer.duration_ticks + windows > self.ticks):
                raise ValueError("Labor transfer requires distinct eligible workers, a different employer and a term within the run")
        return self

    @property
    def labor_cost_per_unit_minor(self) -> int:
        return self.wage_minor // self.output_per_worker
