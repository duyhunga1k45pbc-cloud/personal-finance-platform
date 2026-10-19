import asyncio

from app.routers import activity_shared_stream as shared


def test_reconnect_while_previous_poller_is_cancelling(monkeypatch):
    async def scenario():
        user_id = 987654321

        old_started = asyncio.Event()
        old_cancelled = asyncio.Event()
        allow_old_cleanup = asyncio.Event()

        old_tasks = []
        old_poller = None

        async def fake_cursor(uid):
            assert uid == user_id
            return 0

        async def fake_reader(*, user_id, after_sequence, limit):
            await asyncio.sleep(3600)
            return []

        async def controlled_poller(poller):
            old_tasks.append(asyncio.current_task())
            old_started.set()

            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                old_cancelled.set()
                await allow_old_cleanup.wait()
                raise

        monkeypatch.setattr(shared, "current_activity_cursor", fake_cursor)
        monkeypatch.setattr(shared, "read_activity_events", fake_reader)
        monkeypatch.setattr(shared, "_run_poller", controlled_poller)

        assert not shared._pollers
        # Isolate from draining state left by previous tests.
        monkeypatch.setattr(shared, "_draining", False)

        try:
            old_poller, old_queue = await shared._subscribe(user_id)
            await asyncio.wait_for(old_started.wait(), timeout=3)

            print("T1: Old poller RUNNING")

            unsubscribe_task = asyncio.create_task(
                shared._unsubscribe(old_poller, old_queue)
            )

            await asyncio.wait_for(old_cancelled.wait(), timeout=3)

            assert user_id not in shared._pollers
            assert not unsubscribe_task.done()

            print("T2: Old poller CANCELLING, cleanup blocked")

            new_poller, new_queue = await shared._subscribe(user_id)

            assert new_poller is not old_poller
            assert shared._pollers[user_id] is new_poller
            assert new_queue in new_poller.subscribers

            print("T3: New poller CREATED while old cleanup pending")

            allow_old_cleanup.set()
            await asyncio.wait_for(unsubscribe_task, timeout=3)

            assert shared._pollers[user_id] is new_poller
            assert new_queue in new_poller.subscribers

            print("T4: Old cleanup FINISHED; new subscriber retained")

            await shared._unsubscribe(new_poller, new_queue)

            assert user_id not in shared._pollers

            print("T5: New poller CLEANED UP")

        finally:
            allow_old_cleanup.set()

            if old_poller is not None:
                tasks = [
                    old_poller.task,
                    shared._pollers.get(user_id).task
                    if user_id in shared._pollers
                    else None,
                ]

                for task in tasks:
                    if task is not None and not task.done():
                        task.cancel()

                await asyncio.gather(
                    *(task for task in tasks if task is not None),
                    return_exceptions=True,
                )

            shared._pollers.pop(user_id, None)

    asyncio.run(scenario())
