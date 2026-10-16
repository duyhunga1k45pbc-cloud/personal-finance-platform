import asyncio

import pytest

from app.routers import activity_shared_stream as shared


def test_new_subscriptions_rejected_after_drain():
    async def scenario():
        previous = shared._draining

        try:
            shared._draining = False

            await shared.shutdown_shared_pollers()

            assert shared._draining is True

            with pytest.raises(RuntimeError, match="draining"):
                await shared._subscribe(987654321)

            assert not shared._pollers

        finally:
            shared._draining = previous

    asyncio.run(scenario())
