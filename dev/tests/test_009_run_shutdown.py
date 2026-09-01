"""
Regression test for ``python -m atasks.run`` process shutdown.

Before the fix, ``aiomain()`` connected the configured transport but never
disconnected it again - not even when there was nothing else to do (no
scenario files, ``-M client``). For the AMQP transport this left aio_pika's
background reader/writer/heartbeat/reconnect tasks running on the event
loop; when the interpreter exited, those tasks (and the loop itself) were
torn down out of order instead of shut down cleanly, which surfaced as
"Task was destroyed but it is pending!" / "Event loop is closed" /
"no running event loop" noise on stderr - even for a run with zero
scenarios. See the reported repro:

    python -m atasks.run -v 4 -M client -T amqp

These tests run the real command as a subprocess (exactly like the repro)
against a real broker and assert none of that noise appears, for both
``client`` and ``loopback`` mode. ``server`` mode is not covered here since
it blocks forever waiting for a signal.

Requires a reachable RabbitMQ (or other AMQP 0-9-1 broker) at
ATASKS_TEST_AMQP_URL (default amqp://guest:guest@localhost/). Tests are
skipped (not failed) if no broker is reachable.
"""
import asyncio
import os
import subprocess
import sys
from unittest import IsolatedAsyncioTestCase as TestCase

import aio_pika


AMQP_URL = os.environ.get('ATASKS_TEST_AMQP_URL', 'amqp://guest:guest@localhost/')
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# Substrings that only ever show up when a task/coroutine/loop is torn down
# out of order instead of cleanly - never part of normal atasks logging.
BAD_PATTERNS = (
    'Task was destroyed but it is pending',
    'Event loop is closed',
    'no running event loop',
    'was never awaited',
    'Exception ignored in',
)


async def _check_broker_reachable():
    try:
        connection = await asyncio.wait_for(aio_pika.connect(AMQP_URL), timeout=2)
        await connection.close()
        return True
    except Exception:
        return False


def _run_atasks(mode):
    return subprocess.run(
        [sys.executable, '-m', 'atasks.run', '-v', '4', '-M', mode, '-T', 'amqp', '-U', AMQP_URL],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


class RunAMQPShutdownTest(TestCase):
    """``python -m atasks.run ... -T amqp`` must exit cleanly, with no leftover tasks."""

    async def asyncSetUp(self):
        """Skip when no broker is reachable, rather than failing every test."""
        if not await _check_broker_reachable():
            self.skipTest('No AMQP broker reachable at %s' % AMQP_URL)

    def test_001_client_mode_no_scenario_shuts_down_cleanly(self):
        """Client mode with zero scenarios still connects the transport - and must
        disconnect it again, instead of leaving it (and its background tasks)
        dangling for the interpreter to clean up on exit."""
        result = _run_atasks('client')
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        for pattern in BAD_PATTERNS:
            self.assertNotIn(pattern, result.stderr, pattern)

    def test_002_loopback_mode_no_scenario_shuts_down_cleanly(self):
        """Loopback mode additionally registers a server callback on the transport,
        so both router.deactivate() and transport.disconnect() must run on exit."""
        result = _run_atasks('loopback')
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        for pattern in BAD_PATTERNS:
            self.assertNotIn(pattern, result.stderr, pattern)
