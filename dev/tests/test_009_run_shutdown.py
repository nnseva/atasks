"""
Regression tests for ``python -m atasks.run`` process shutdown.

Before the fix, ``aiomain()`` connected the configured transport but never
disconnected it again - not even when there was nothing else to do (no
scenario files, a 'client'-only namespace). For the AMQP transport this left
aio_pika's background reader/writer/heartbeat/reconnect tasks running on the
event loop; when the interpreter exited, those tasks (and the loop itself)
were torn down out of order instead of shut down cleanly, which surfaced as
"Task was destroyed but it is pending!" / "Event loop is closed" /
"no running event loop" noise on stderr - even for a run with zero
scenarios. See the reported repro (pre-multi-namespace CLI):

    python -m atasks.run -v 4 -M client -T amqp

``RunAMQPShutdownTest`` runs the real command as a subprocess (exactly like
the repro) against a real broker and asserts none of that noise appears, for
a 'client' namespace (no activation at all) and for a 'server' one (activated,
then shut down via a real OS signal - the only way to end a 'server'
namespace's "Listening for requests" wait, see atasks/run.py's aiomain()).

``RunMultiNamespaceListeningTest`` covers the core multi-namespace rule
itself - the process must enter "Listening for requests" as soon as *any*
configured namespace is 'server', even when every other namespace is
'client' - and needs no broker (loopback transport only).

Requires a reachable RabbitMQ (or other AMQP 0-9-1 broker) at
ATASKS_TEST_AMQP_URL (default amqp://guest:guest@localhost/) for
``RunAMQPShutdownTest`` only. Those tests are skipped (not failed) if no
broker is reachable.
"""
import asyncio
import os
import signal
import subprocess
import sys
import time
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

READY_PATTERN = 'Listening for requests'


async def _check_broker_reachable():
    try:
        connection = await asyncio.wait_for(aio_pika.connect(AMQP_URL), timeout=2)
        await connection.close()
        return True
    except Exception:
        return False


def _run_atasks(*namespace_specs, extra_args=()):
    """Run ``python -m atasks.run`` to completion (no 'server' namespace among
    ``namespace_specs``, so it's expected to return on its own) and return the
    finished ``subprocess.CompletedProcess``."""
    args = [sys.executable, '-m', 'atasks.run', '-v', '4']
    for spec in namespace_specs:
        args += ['-N', spec]
    args += list(extra_args)
    return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)


def _run_server_until_signaled(*namespace_specs, extra_args=(), timeout=15):
    """
    Spawn ``python -m atasks.run`` with the given -N specs (at least one
    'server' among them), wait until it logs READY_PATTERN - proving it
    actually reached the "Listening for requests" wait loop - then send it
    SIGTERM, exactly as an operator/orchestrator would to stop the service.

    :returns: (returncode, combined stderr, stdout) of the finished process
    :raises AssertionError: if READY_PATTERN never appears within ``timeout``
    """
    args = [sys.executable, '-m', 'atasks.run', '-v', '4']
    for spec in namespace_specs:
        args += ['-N', spec]
    args += list(extra_args)
    proc = subprocess.Popen(
        args, cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        # Unbuffered, so READY_PATTERN reaches us as soon as it's logged
        # instead of sitting in a pipe buffer until the process exits.
        env=dict(os.environ, PYTHONUNBUFFERED='1'),
    )
    stderr_lines = []
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stderr.readline()
            if line:
                stderr_lines.append(line)
                if READY_PATTERN in line:
                    break
            elif proc.poll() is not None:
                break
        else:
            raise AssertionError('process never printed %r within %ss' % (READY_PATTERN, timeout))

        proc.send_signal(signal.SIGTERM)
        remaining_stdout, remaining_stderr = proc.communicate(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    return proc.returncode, ''.join(stderr_lines) + remaining_stderr, remaining_stdout


class RunAMQPShutdownTest(TestCase):
    """``python -m atasks.run ... transport=amqp`` must exit cleanly, with no leftover tasks."""

    async def asyncSetUp(self):
        """Skip when no broker is reachable, rather than failing every test."""
        if not await _check_broker_reachable():
            self.skipTest('No AMQP broker reachable at %s' % AMQP_URL)

    def test_001_client_namespace_no_scenario_shuts_down_cleanly(self):
        """A 'client' namespace with zero scenarios still connects the transport -
        and must disconnect it again, instead of leaving it (and its background
        tasks) dangling for the interpreter to clean up on exit."""
        result = _run_atasks('name=default,mode=client,transport=amqp,url=%s' % AMQP_URL)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        for pattern in BAD_PATTERNS:
            self.assertNotIn(pattern, result.stderr, pattern)

    def test_002_server_namespace_shuts_down_cleanly_on_signal(self):
        """A 'server' namespace additionally registers a server callback on the
        transport, so both router.deactivate() and transport.disconnect() must
        run once the process is signaled to stop - not just transport.connect()
        being left dangling as in the original report."""
        returncode, stderr, _stdout = _run_server_until_signaled(
            'name=default,mode=server,transport=amqp,url=%s' % AMQP_URL,
        )
        self.assertEqual(returncode, 0, msg=stderr)
        for pattern in BAD_PATTERNS:
            self.assertNotIn(pattern, stderr, pattern)


class RunMultiNamespaceListeningTest(TestCase):
    """
    The core multi-namespace rule: the process must enter "Listening for
    requests" - and only exit once signaled - as soon as *any* configured
    namespace is 'server', regardless of how many other namespaces are
    'client'. No broker needed: every namespace here uses the loopback
    transport.
    """

    def test_all_client_namespaces_never_listen(self):
        """With every namespace 'client' (the default when mode= is omitted),
        the process must connect and disconnect every transport and return on
        its own - it must never reach the wait-for-signal loop at all."""
        result = _run_atasks('name=ns-a', 'name=ns-b,mode=client')
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn(READY_PATTERN, result.stderr)
        for pattern in BAD_PATTERNS:
            self.assertNotIn(pattern, result.stderr, pattern)

    def test_one_server_namespace_among_several_forces_listening(self):
        """Only one of several namespaces needs to be 'server' for the whole
        process to end up listening - and, once signaled, every namespace's
        transport (not just the 'server' one's) must still be disconnected."""
        returncode, stderr, _stdout = _run_server_until_signaled(
            'name=ns-client,mode=client',
            'name=ns-server,mode=server',
            'name=ns-client-2,mode=client',
        )
        self.assertEqual(returncode, 0, msg=stderr)
        for pattern in BAD_PATTERNS:
            self.assertNotIn(pattern, stderr, pattern)
        # Every namespace's transport must have been connected...
        for name in ('ns-client', 'ns-server', 'ns-client-2'):
            self.assertIn('Creating a transport <atasks.transport.base.LoopbackTransport object at 0x', stderr)
            self.assertIn(' in %s\n' % name, stderr)
        # ...and only the 'server' one's Router got activated/deactivated.
        self.assertIn('for the router of ns-server', stderr)
        self.assertNotIn('for the router of ns-client\n', stderr)
        self.assertNotIn('for the router of ns-client-2\n', stderr)
