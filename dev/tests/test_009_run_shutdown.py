"""
Regression tests for ``python -m atasks.commands.service`` process shutdown.

Before the fix, ``aiomain()`` connected the configured transport but never
disconnected it again - not even when there was nothing else to do (no
scenario files, a 'client'-only namespace). For the AMQP transport this left
aio_pika's background reader/writer/heartbeat/reconnect tasks running on the
event loop; when the interpreter exited, those tasks (and the loop itself)
were torn down out of order instead of shut down cleanly, which surfaced as
"Task was destroyed but it is pending!" / "Event loop is closed" /
"no running event loop" noise on stderr - even for a run with zero
scenarios. See the reported repro (pre-multi-namespace CLI):

    python -m atasks.commands.service -v 4 -M client -T amqp

``RunAMQPShutdownTest`` runs the real command as a subprocess (exactly like
the repro) against a real broker and asserts none of that noise appears, for
a 'client' namespace (no activation at all) and for a 'server' one (activated,
then shut down via a real OS signal). It also verifies a request already being
handled finishes and its response reaches the caller before shutdown completes.

``RunAMQPShutdownTimeoutTest`` covers the drain timeout: a handler that never
returns is cancelled once ``--shutdown-timeout`` expires, the caller receives
``ServiceShuttingDown``, and the exit code is 0 by default or 1 with
``--fail-on-shutdown-timeout``.

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
import tempfile
import time
import uuid
from unittest import IsolatedAsyncioTestCase as TestCase

from atasks.codecs import PickleCodec
from atasks.router import ServiceShuttingDown
from atasks.tasks import atask
from atasks.transport.backends.amqp import AMQPTransport
from dev.tests._amqp_environment import require_amqp


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


def _run_atasks(*namespace_specs, extra_args=()):
    """Run ``python -m atasks.commands.service`` to completion (no 'server' namespace among
    ``namespace_specs``, so it's expected to return on its own) and return the
    finished ``subprocess.CompletedProcess``."""
    args = [sys.executable, '-m', 'atasks.commands.service', '-v', '4']
    for spec in namespace_specs:
        args += ['-N', spec]
    args += list(extra_args)
    return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)


def _run_server_until_signaled(*namespace_specs, extra_args=(), timeout=15):
    """
    Spawn ``python -m atasks.commands.service`` with the given -N specs (at least one
    'server' among them), wait until it logs READY_PATTERN - proving it
    actually reached the "Listening for requests" wait loop - then send it
    SIGTERM, exactly as an operator/orchestrator would to stop the service.

    :returns: (returncode, combined stderr, stdout) of the finished process
    :raises AssertionError: if READY_PATTERN never appears within ``timeout``
    """
    args = [sys.executable, '-m', 'atasks.commands.service', '-v', '4']
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
    """AMQP-backed service shutdown must exit cleanly without abandoning work."""

    async def asyncSetUp(self):
        """Require AMQP unless this integration group was explicitly disabled."""
        if not await require_amqp():
            self.skipTest('AMQP integration tests explicitly disabled by ATASKS_SKIP_AMQP_TESTS=1')

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

    async def test_003_sigterm_waits_for_in_flight_rpc_to_reply(self):
        """SIGTERM during an RPC must not close the transport before its handler
        completes and publishes the response."""
        namespace = 'test-shutdown-%s' % uuid.uuid4().hex

        with tempfile.TemporaryDirectory() as directory:
            started_path = os.path.join(directory, 'started')
            release_path = os.path.join(directory, 'release')
            completed_path = os.path.join(directory, 'completed')
            scenario_path = os.path.join(directory, 'scenario.py')
            with open(scenario_path, 'w', encoding='utf-8') as scenario_file:
                scenario_file.write(
                    'import asyncio\n'
                    'import os\n'
                    'from atasks.tasks import atask\n'
                    '\n'
                    '@atask(namespace=%r, name="shutdown_probe")\n'
                    'async def shutdown_probe():\n'
                    '    open(os.environ["ATASKS_TEST_STARTED"], "w").close()\n'
                    '    while not os.path.exists(os.environ["ATASKS_TEST_RELEASE"]):\n'
                    '        await asyncio.sleep(0.02)\n'
                    '    open(os.environ["ATASKS_TEST_COMPLETED"], "w").close()\n'
                    '    return "completed"\n'
                    % namespace
                )

            proc = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'atasks.commands.service',
                '-v', '4',
                '-N', 'name=%s,mode=server,transport=amqp,url=%s' % (namespace, AMQP_URL),
                scenario_path,
                cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=dict(
                    os.environ,
                    PYTHONUNBUFFERED='1',
                    ATASKS_TEST_STARTED=started_path,
                    ATASKS_TEST_RELEASE=release_path,
                    ATASKS_TEST_COMPLETED=completed_path,
                ),
            )

            stderr_lines = []
            ready = asyncio.Event()
            signal_received = asyncio.Event()
            shutdown_started = asyncio.Event()

            async def read_stderr():
                async for raw_line in proc.stderr:
                    line = raw_line.decode(errors='replace')
                    stderr_lines.append(line)
                    if READY_PATTERN in line:
                        ready.set()
                    if 'Signal SIGTERM' in line:
                        signal_received.set()
                    if 'Execution stopped' in line:
                        shutdown_started.set()

            stderr_reader = asyncio.create_task(read_stderr())
            client_transport = None
            call_task = None
            signal_sent = False
            try:
                await asyncio.wait_for(ready.wait(), timeout=15)

                PickleCodec(namespace=namespace)
                client_transport = AMQPTransport(namespace=namespace, url=AMQP_URL)
                await client_transport.connect()

                @atask(namespace=namespace, name='shutdown_probe', timeout=20)
                async def shutdown_probe():
                    raise AssertionError('The client-side RPC stub must not run locally')

                call_task = asyncio.create_task(shutdown_probe())
                await asyncio.wait_for(
                    self._wait_for_file(started_path),
                    timeout=10,
                )

                proc.send_signal(signal.SIGTERM)
                signal_sent = True
                await asyncio.wait_for(signal_received.wait(), timeout=5)
                await asyncio.wait_for(shutdown_started.wait(), timeout=5)

                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                else:
                    self.fail(
                        'service exited while the RPC handler was held at its barrier:\n%s'
                        % ''.join(stderr_lines)
                    )

                with open(release_path, 'w', encoding='utf-8'):
                    pass

                result = await asyncio.wait_for(call_task, timeout=10)
                self.assertEqual(result, 'completed')
                await asyncio.wait_for(proc.wait(), timeout=10)
                await asyncio.wait_for(stderr_reader, timeout=5)

                self.assertEqual(proc.returncode, 0, msg=''.join(stderr_lines))
                self.assertTrue(os.path.exists(completed_path))
                for pattern in BAD_PATTERNS:
                    self.assertNotIn(pattern, ''.join(stderr_lines), pattern)
            finally:
                with open(release_path, 'w', encoding='utf-8'):
                    pass
                if call_task is not None and not call_task.done():
                    call_task.cancel()
                    try:
                        await call_task
                    except asyncio.CancelledError:
                        pass
                if proc.returncode is None:
                    if not signal_sent:
                        proc.send_signal(signal.SIGTERM)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                if client_transport is not None:
                    await client_transport.disconnect()
                if not stderr_reader.done():
                    await asyncio.wait_for(stderr_reader, timeout=5)

    @staticmethod
    async def _wait_for_file(path):
        while not os.path.exists(path):
            await asyncio.sleep(0.02)


class RunAMQPShutdownTimeoutTest(TestCase):
    """
    SIGTERM while a handler never finishes: after ``--shutdown-timeout`` the
    handler is cancelled, the caller receives ``ServiceShuttingDown`` and the
    process exits (0 by default, non-zero with ``--fail-on-shutdown-timeout``).
    """

    async def _run_stuck_handler(self, extra_args):
        namespace = 'test-shutdown-timeout-%s' % uuid.uuid4().hex

        with tempfile.TemporaryDirectory() as directory:
            started_path = os.path.join(directory, 'started')
            scenario_path = os.path.join(directory, 'scenario.py')
            with open(scenario_path, 'w', encoding='utf-8') as scenario_file:
                scenario_file.write(
                    'import asyncio\n'
                    'import os\n'
                    'from atasks.tasks import atask\n'
                    '\n'
                    '@atask(namespace=%r, name="stuck_probe")\n'
                    'async def stuck_probe():\n'
                    '    open(os.environ["ATASKS_TEST_STARTED"], "w").close()\n'
                    '    while True:\n'
                    '        await asyncio.sleep(0.02)\n'
                    % namespace
                )

            proc = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'atasks.commands.service',
                '-v', '4',
                '-N', 'name=%s,mode=server,transport=amqp,url=%s' % (namespace, AMQP_URL),
                *extra_args,
                scenario_path,
                cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=dict(os.environ, PYTHONUNBUFFERED='1', ATASKS_TEST_STARTED=started_path),
            )

            stderr_lines = []
            ready = asyncio.Event()

            async def read_stderr():
                async for raw_line in proc.stderr:
                    line = raw_line.decode(errors='replace')
                    stderr_lines.append(line)
                    if READY_PATTERN in line:
                        ready.set()

            stderr_reader = asyncio.create_task(read_stderr())
            client_transport = None
            call_task = None
            try:
                await asyncio.wait_for(ready.wait(), timeout=15)

                PickleCodec(namespace=namespace)
                client_transport = AMQPTransport(namespace=namespace, url=AMQP_URL)
                await client_transport.connect()

                @atask(namespace=namespace, name='stuck_probe', timeout=20)
                async def stuck_probe():
                    raise AssertionError('The client-side RPC stub must not run locally')

                call_task = asyncio.create_task(stuck_probe())
                await asyncio.wait_for(RunAMQPShutdownTest._wait_for_file(started_path), timeout=10)

                proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=10)
                await asyncio.wait_for(stderr_reader, timeout=5)

                with self.assertRaises(ServiceShuttingDown):
                    await asyncio.wait_for(call_task, timeout=10)

                return proc.returncode, ''.join(stderr_lines)
            finally:
                if call_task is not None and not call_task.done():
                    call_task.cancel()
                    try:
                        await call_task
                    except (asyncio.CancelledError, ServiceShuttingDown):
                        pass
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                if client_transport is not None:
                    await client_transport.disconnect()
                if not stderr_reader.done():
                    await asyncio.wait_for(stderr_reader, timeout=5)

    async def test_001_timeout_cancels_handler_and_exits_zero(self):
        returncode, stderr = await self._run_stuck_handler(['--shutdown-timeout', '1'])

        self.assertEqual(returncode, 0, msg=stderr)
        self.assertIn('Cancelling', stderr)
        self.assertIn('service is shutting down', stderr)
        for pattern in BAD_PATTERNS:
            self.assertNotIn(pattern, stderr, pattern)

    async def test_002_fail_on_timeout_exits_nonzero(self):
        returncode, stderr = await self._run_stuck_handler(
            ['--shutdown-timeout', '1', '--fail-on-shutdown-timeout'],
        )

        self.assertNotEqual(returncode, 0, msg=stderr)
        self.assertIn('Shutdown timeout expired', stderr)


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
