"""Receipt-backed consumption comparison with one-period sharing delay."""
from collections import Counter

KIND = "disclosed_consumption"


def consumption_message_id(sender, recipient, root_claim_id):
    return f"demand:{sender}:{recipient}:{root_claim_id}"


def consumption_disclosure(receipt):
    return {"kind": KIND, "root_claim_id": f"consumption:{receipt['command_id']}",
            "receipt_command_id": receipt["command_id"], "units": receipt["executed_quantity"]}


def consumption_source(body, receipts, sender, sent_tick):
    receipt = receipts.get(body.get("receipt_command_id", "")) if type(body.get("receipt_command_id")) is str else None
    if (receipt is not None and receipt.actor_id == sender and receipt.tick == sent_tick - 1
            and receipt.outcome == "consumed" and body == consumption_disclosure(receipt.model_dump())):
        return receipt
    return None


def demand_comparison(observation, peer_uplift_units):
    window = observation.visible_until.tick - 2
    own = sum(f.value["executed_quantity"] for f in observation.private_memory_refs
              if f.value.get("outcome") == "consumed" and f.value.get("tick") == window)
    peers = Counter()
    refs = []
    for message in observation.delivered_messages:
        if message.body.get("kind") == KIND and message.created_at.tick == window+1:
            peers[message.sender_id] += message.body["units"]
            refs.append(message.message_id)
    max_peer = max(peers.values(), default=0)
    uplift = peer_uplift_units if max_peer > own else 0
    return {"consumption_source_tick": window if refs else None,
            "prior_own_units": own, "max_peer_units": max_peer, "peer_uplift_units": uplift,
            "consumption_message_ids": sorted(refs)}
