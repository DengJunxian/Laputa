"""Contract evidence derived from completed work and the authoritative economic journal."""
from typing import Literal

from pydantic import model_validator

from civitas.contracts.base import Id, NonNegative, Positive, Record, content_hash
from civitas.contracts.economic import Amount, PayrollInstallment
from civitas.contracts.observations import ObservationFact


LABOR_OUTCOME_KIND = "labor_contract_outcome"
LABOR_OUTCOME_PREFIX = "labor-outcome:"
LABOR_ASSESSMENT = "labor_outcome_assessment"
REHIRE_OFFER = "labor_rehire_offer"
SWITCH_OFFER = "labor_switch_offer"


class BinaryPerformanceCounts(Record):
    """Unit-weight Beta counts with a symmetric prior."""
    a: Positive = 1
    b: Positive = 1

    def counted(self, success):
        return {"a": self.a + int(success), "b": self.b + int(not success)}


class WagePerformanceTrust(BinaryPerformanceCounts):
    """Counts for one worker's evidence about one employer."""


class ContractOutcomeEvent(Record):
    schema_version: Literal[1] = 1
    contract_id: Id
    employer_id: Id
    worker_id: Id
    due_tick: Positive
    outcome_tick: Positive
    delivered_minutes: NonNegative
    accrued_minor: Amount
    paid_minor: Amount
    outstanding_after_minor: Amount | None
    ledger_tx_ids: tuple[Id, Id]

    @model_validator(mode="after")
    def settlement(self):
        if self.outstanding_after_minor is None:
            if self.outcome_tick >= self.due_tick or any((self.delivered_minutes, self.accrued_minor, self.paid_minor)):
                raise ValueError("Cancellation evidence requires an unearned wage")
        elif (self.outcome_tick != self.due_tick or not self.delivered_minutes or not self.accrued_minor
              or self.paid_minor > self.accrued_minor
              or self.outstanding_after_minor < self.accrued_minor - self.paid_minor):
            raise ValueError("Wage evidence must agree with the earned amount and remaining obligation")
        return self

    def private_facts(self):
        obligation = content_hash([self.contract_id, self.due_tick])
        root_id = LABOR_OUTCOME_PREFIX + obligation
        if self.outstanding_after_minor is None:
            outcome = "cancelled_before_work"
        else:
            outcome = "paid" if self.paid_minor == self.accrued_minor else "arrears"
        payload = {**self.model_dump(mode="json"), "kind": LABOR_OUTCOME_KIND, "outcome": outcome,
                   "target_id": "labor-wage-obligation:" + obligation, "root_claim_id": root_id}
        return [ObservationFact(fact_id=f"{root_id}:{actor}", category="memory", owner_id=actor,
                    visible_at={"tick": self.outcome_tick + 1, "phase": "OBSERVE"}, value=payload)
                for actor in (self.worker_id, self.employer_id)]


def labor_outcome_facts(tick, work_events, journal):
    transactions = {entry.tx_id: entry for entry in journal}
    reserves = {entry.event_id: entry for entry in journal if entry.kind == "payroll_reserve"}
    facts = []
    for work in work_events:
        if "contract_id" not in work:
            continue
        accrual = transactions[work["accrual_tx_id"]]
        payment = transactions[work["payment_tx_id"]]
        employer, worker = work["employer_id"], work["worker_id"]
        wage = accrual.details["accrued_minor"]
        for entry, kind in ((accrual, "wage_accrual"), (payment, "wage_payment")):
            if (entry.kind, entry.tick, entry.payload["employer_id"], entry.payload["worker_id"],
                    entry.payload["amount_minor"]) != (kind, tick, employer, worker, wage):
                raise ValueError("Labor evidence does not match its journal entries")
        facts.extend(ContractOutcomeEvent(contract_id=work["contract_id"], employer_id=employer,
            worker_id=worker, due_tick=tick, outcome_tick=tick, delivered_minutes=work["delivered_minutes"],
            accrued_minor=wage, paid_minor=payment.details["paid_minor"],
            outstanding_after_minor=payment.details["outstanding_minor"],
            ledger_tx_ids=(accrual.tx_id, payment.tx_id)).private_facts())

    for release in journal:
        if release.tick != tick or release.kind != "payroll_release":
            continue
        installment = PayrollInstallment.model_validate(release.payload)
        reserve = reserves[f"payroll-reserve:{installment.reservation_id}"]
        facts.extend(ContractOutcomeEvent(contract_id=installment.contract_id,
            employer_id=reserve.payload["employer_id"], worker_id=reserve.payload["worker_id"],
            due_tick=reserve.payload["due_tick"], outcome_tick=tick, delivered_minutes=0,
            accrued_minor=0, paid_minor=0, outstanding_after_minor=None,
            ledger_tx_ids=(reserve.tx_id, release.tx_id)).private_facts())
    return facts


def labor_assessments(observation, *, newly_visible=False):
    return {fact.fact_id: {"fact_id": fact.fact_id, "root_claim_id": fact.value["root_claim_id"],
                          "target_id": fact.value["target_id"], "outcome": fact.value["outcome"]}
            for fact in observation.private_memory_refs
            if fact.fact_id.startswith(LABOR_OUTCOME_PREFIX)
            and (not newly_visible or fact.visible_at == observation.visible_until)}


def research_labor_outcomes(memories):
    return [fact.value for fact in memories
            if fact.fact_id.startswith(LABOR_OUTCOME_PREFIX) and fact.owner_id == fact.value["worker_id"]]
