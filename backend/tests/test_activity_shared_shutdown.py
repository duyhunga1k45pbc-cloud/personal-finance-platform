import asyncio

from app.routers import activity_shared_stream as shared


def test_shutdown_cancels_pollers_and_notifies_subscribers():
    async def scenario():
        user_id = 987654321
        poller = shared.UserPoller(user_id=user_id, cursor=0)
        queue = asyncio.Queue(maxsize=shared.QUEUE_SIZE)
        poller.subscribers.add(queue)

        started = asyncio.Event()

        async def background():
            started.set()
            await asyncio.Event().wait()

        poller.task = asyncio.create_task(background())
        await started.wait()

        async with shared._registry_lock:
            assert user_id not in shared._pollers
            shared._pollers[user_id] = poller

        try:
            await asyncio.wait_for(
                shared.shutdown_shared_pollers(),
                timeout=2,
            )

            assert poller.task.done()
            assert poller.task.cancelled()
            assert not poller.subscribers
            assert queue.get_nowait() is shared._STOP

            async with shared._registry_lock:
                assert user_id not in shared._pollers

            # Repeated shutdown must be safe.
            await shared.shutdown_shared_pollers()
        finally:
            if not poller.task.done():
                poller.task.cancel()
                await asyncio.gather(
                    poller.task,
                    return_exceptions=True,
                )

            async with shared._registry_lock:
                shared._pollers.pop(user_id, None)

    asyncio.run(scenario())
