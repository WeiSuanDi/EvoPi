"""Minimal real-model Agent without the CodingHarness."""

import asyncio

from evopi.ai.models import model_from_environment
from evopi.core.agent import Agent


async def run() -> None:
    agent = Agent(model=model_from_environment(), system_prompt="Answer concisely.")
    answer = await agent.prompt("Say hello from EvoPi in one sentence.")
    print(answer.content)


if __name__ == "__main__":
    asyncio.run(run())
