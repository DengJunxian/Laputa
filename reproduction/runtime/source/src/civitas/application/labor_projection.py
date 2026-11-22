"""Research labor and wage edges derived from one committed C/D snapshot."""

from collections import Counter, defaultdict

from civitas.contracts.economic import LaborContract


def _wage_tx_ids(shifts):
    return [shift[key] for shift in shifts for key in ("accrual_tx_id", "payment_tx_id")]


def _opportunity_counts(rows):
    return {
        "candidates": len(rows),
        "visible": sum(r["visible"] for r in rows),
        "worked": sum(r["worked"] for r in rows),
        "by_status": dict(Counter(r["status"] for r in rows)),
    }


def project_labor_graph(snapshot):
    tick = snapshot.tick
    state = snapshot.data["closed_economy"]
    social = snapshot.data["closed_social"]
    book = snapshot.data["economic_runtime"]
    households, employers = sorted(state["population"]), sorted(social["sellers"])
    journal = {entry["tx_id"]: entry for entry in book["journal"]}
    fees = {
        entry["payload"]["contract_id"]: entry["tx_id"]
        for entry in journal.values()
        if entry["kind"] == "labor_switch_fee"
    }

    by_contract, by_pair, current_work = defaultdict(list), defaultdict(list), {}
    for shift in state["labor_events"]:
        worker, employer = shift["worker_id"], shift["employer_id"]
        by_pair[employer, worker].append(shift)
        if "contract_id" in shift:
            by_contract[shift["contract_id"]].append(shift)
        if shift["tick"] == tick:
            if worker in current_work:
                raise ValueError("Labor projection found multiple shifts for one worker at the cursor")
            current_work[worker] = shift

    raw_contracts = [
        state.get("labor_contract"),
        state.get("rehire_contract"),
        state.get("second_rehire_contract"),
        *state.get("shared_payroll", {}).get("contracts", {}).values(),
    ]
    contracts = []
    for raw in raw_contracts:
        if raw is None:
            continue
        contract = LaborContract.model_validate(raw)
        offer = contract.offer
        end = contract.early_end_tick or offer.end_tick_exclusive
        if contract.activation_failed:
            status = "not_activated"
        elif tick < offer.start_tick:
            status = "pending"
        elif tick >= end:
            status = "expired"
        else:
            status = "active"
        shifts = by_contract[contract.contract_id]
        contracts.append(
            {
                "contract_id": contract.contract_id,
                "offer_id": offer.offer_id,
                "worker_id": offer.worker_id,
                "employer_id": offer.employer_id,
                "status": status,
                "offered_tick": offer.offered_tick,
                "start_tick": offer.start_tick,
                "end_tick_exclusive": end,
                "scheduled_end_tick_exclusive": offer.end_tick_exclusive,
                "work_event_ids": [s["event_id"] for s in shifts],
                "wage_tx_ids": _wage_tx_ids(shifts),
                "switch_fee_tx_id": fees.get(contract.contract_id),
            }
        )
    contracts.sort(key=lambda row: (row["offered_tick"], row["contract_id"]))

    if tick == 0:
        active = [
            {
                "worker_id": worker,
                "employer_id": state["population"][worker]["employer_id"],
                "origin": "initial_assignment",
                "contract_id": None,
                "work_event_id": None,
                "delivered_minutes": 0,
                "wage_tx_ids": [],
            }
            for worker in households
        ]
    else:
        active = [
            {
                "worker_id": worker,
                "employer_id": shift["employer_id"],
                "origin": "contract" if "contract_id" in shift else "initial_assignment",
                "contract_id": shift.get("contract_id"),
                "work_event_id": shift["event_id"],
                "delivered_minutes": shift["delivered_minutes"],
                "wage_tx_ids": _wage_tx_ids([shift]),
            }
            for worker, shift in sorted(current_work.items())
        ]
    scheduled_ids = {row["contract_id"] for row in contracts if row["status"] == "active"}
    delivered_ids = {edge["contract_id"] for edge in active if edge["contract_id"] is not None}
    if scheduled_ids != delivered_ids:
        raise ValueError("Labor projection found active contracts without corresponding shifts")
    unemployed = sorted(set(households) - {row["worker_id"] for row in active})

    # The book clears wages by employer/worker pair; contract references identify contributing shifts.
    claims = []
    for entry in book["wage_obligations"]:
        if not entry["outstanding_minor"]:
            continue
        pair = (entry["employer_id"], entry["worker_id"])
        shifts = by_pair[pair]
        tx_ids = _wage_tx_ids(shifts)
        if not tx_ids or journal[tx_ids[-1]]["details"]["outstanding_minor"] != entry["outstanding_minor"]:
            raise ValueError("Labor projection wage claim disagrees with its payment history")
        claims.append(
            {
                **entry,
                "wage_tx_ids": tx_ids,
                "contributing_contract_ids": sorted({s["contract_id"] for s in shifts if "contract_id" in s}),
                "includes_initial_assignment": any("contract_id" not in s for s in shifts),
            }
        )

    repeated = "second_rehire_contract" in state
    contract_status = {row["contract_id"]: row["status"] for row in contracts}
    opportunities = []
    by_window = defaultdict(list)
    for fact in social["trace"]:
        if fact["kind"] not in ("labor_initial_choice", "rehire_choice"):
            continue
        offer = fact["offer"]
        contract_id = fact["command_id"] if fact["status"] == "accepted" else None
        opportunities.append(
            {
                "tick": fact["tick"],
                "worker_id": fact["actor_id"],
                "employer_id": offer["employer_id"]
                if offer is not None
                else fact.get("candidate_employer_id"),
                "offer_id": offer["offer_id"] if offer is not None else None,
                "visible": offer is not None,
                "status": fact["status"],
                "decision_id": fact["decision_id"],
                "observation_id": fact["observation_id"],
                "command_id": fact.get("command_id"),
                "reason_codes": list(fact.get("reason_codes", ())),
                "worked": bool(by_contract.get(contract_id)),
                **(
                    {
                        "window_kind": "initial" if fact["kind"] == "labor_initial_choice" else "followup",
                        "score_known": fact["score"] is not None,
                        "contract_status": contract_status.get(contract_id),
                    }
                    if repeated
                    else {}
                ),
            }
        )
        if repeated:
            by_window[str(fact["tick"])].append(opportunities[-1])
    window_counts = {
        tick: {
            **_opportunity_counts(rows),
            "visible_unknown_score": sum(r["visible"] and not r["score_known"] for r in rows),
            "signed_not_started": sum(r["status"] == "accepted" and not r["worked"] for r in rows),
        }
        for tick, rows in by_window.items()
    }
    return {
        "projection_version": "labor-graph.v1",
        "currency_id": book["currency_id"],
        "household_ids": households,
        "employer_ids": employers,
        "active": active,
        "contracts": contracts,
        "unemployed_worker_ids": unemployed,
        "wage_obligation_basis": "employer_worker_pair",
        "wage_obligations": claims,
        "opportunity_scope": "recorded_initial_and_followup_candidates"
        if repeated
        else "recorded_followup_candidates",
        "opportunities": opportunities,
        "counts": {
            "households": len(households),
            "employed": len(active),
            "unemployed": len(unemployed),
            "worked_at_cursor": len(current_work),
        },
        "opportunity_counts": _opportunity_counts(opportunities),
        **({"window_counts": window_counts} if repeated else {}),
    }
