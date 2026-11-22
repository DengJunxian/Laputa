"""Bind the model runtime to committed invocation receipts."""


def bind_invocation_receipts(world, runtime):
    def record(receipt):
        if receipt.attempt_id not in {r.attempt_id for r in world.invocations}:
            world.record_invocation(receipt)

    runtime.on_receipt = record
