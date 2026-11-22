from civitas.contracts.observations import Message
from civitas.contracts.time import SimTime


def compile_messages(bundle, tick: int) -> list[Message]:
    return [Message(message_id=draft.message_id, sender_id=bundle.actor_id, recipient_id=draft.recipient_id,
        created_at=SimTime(tick=tick, phase="FEEDBACK"), visible_at=SimTime(tick=tick + 1, phase="OBSERVE"),
        body=draft.body) for draft in bundle.message_drafts]
