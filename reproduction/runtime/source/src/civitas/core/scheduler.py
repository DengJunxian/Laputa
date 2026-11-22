import asyncio

from civitas.contracts.decisions import Command, DecisionBundle
from civitas.contracts.codecs import decode_decision
from civitas.contracts.observations import Observation
from civitas.contracts.ports import Policy


async def decide_batch(policy: Policy, observations: list[Observation], concurrency: int) -> list[DecisionBundle]:
    semaphore = asyncio.Semaphore(concurrency)

    async def decide(observation):
        async with semaphore:
            return decode_decision(await policy.decide(observation))

    tasks = [asyncio.create_task(decide(observation)) for observation in observations]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def command_order(command: Command) -> tuple[int, str, int, str]:
    return command.arrival_key, command.actor_id, command.local_order_index, command.command_id
