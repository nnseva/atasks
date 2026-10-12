"""Tests for optional service liveness mechanisms."""
import asyncio
import sys
import time
import types
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from aiohttp import ClientSession

from atasks.commands.service import (
    _parse_health_http,
    _positive_seconds,
    _SelfWatchdog,
    _start_health_http,
    _start_systemd_watchdog,
    _systemd_watchdog_loop,
)


class HealthHttpParserTest(TestCase):
    """Health HTTP command-line address parsing tests."""

    def test_defaults_port_and_path(self):
        """A host alone uses the documented default port and path."""
        self.assertEqual(_parse_health_http('0.0.0.0'), ('0.0.0.0', 80, '/'))

    def test_host_port_and_path(self):
        """An explicit host, port, and path are retained."""
        self.assertEqual(_parse_health_http('127.0.0.1:8080/healthz'), ('127.0.0.1', 8080, '/healthz'))

    def test_bracketed_ipv6(self):
        """Bracketed IPv6 addresses are accepted."""
        self.assertEqual(_parse_health_http('[::1]:8080/live'), ('::1', 8080, '/live'))

    def test_invalid_addresses_are_rejected(self):
        """Malformed health endpoint values fail during argument parsing."""
        for value in ('', ':80', 'host:0', 'host:65536', 'host:not-a-port', '::1:80', 'host?query=yes'):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    _parse_health_http(value)

    def test_positive_seconds_requires_finite_positive_value(self):
        """Watchdog durations reject zero, negatives, and non-finite values."""
        for value in ('0', '-1', 'nan', 'inf', 'not-a-number'):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    _positive_seconds(value)


class HealthHttpTest(IsolatedAsyncioTestCase):
    """aiohttp liveness endpoint tests."""

    async def test_endpoint_returns_ok_only_at_configured_path(self):
        """The configured path responds successfully and other paths do not."""
        runner = await _start_health_http(('127.0.0.1', 0, '/healthz'))
        try:
            site = next(iter(runner.sites))
            port = site._server.sockets[0].getsockname()[1]
            async with ClientSession() as session:
                async with session.get('http://127.0.0.1:%s/healthz' % port) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.text(), 'OK\n')
                async with session.get('http://127.0.0.1:%s/' % port) as response:
                    self.assertEqual(response.status, 404)
        finally:
            await runner.cleanup()


class SelfWatchdogTest(TestCase):
    """Self-watchdog thread tests."""

    def test_responsive_loop_never_signals(self):
        """A loop that runs the scheduled acknowledgement remains healthy."""
        class ResponsiveLoop(object):
            def call_soon_threadsafe(self, callback):
                callback()

        watchdog = _SelfWatchdog(ResponsiveLoop(), period=0.01, timeout=0.01, kill_after=0.01)
        with patch('atasks.commands.service.os.kill') as kill:
            watchdog.start()
            time.sleep(0.04)
            watchdog.stop()
        kill.assert_not_called()

    def test_abort_sends_sigabrt_then_sigkill(self):
        """An unresponsive loop uses SIGABRT followed by the hard-kill fallback."""
        watchdog = _SelfWatchdog(None, period=1, timeout=3, kill_after=5)
        with patch('atasks.commands.service.os.kill') as kill, patch('atasks.commands.service.time.sleep') as sleep:
            watchdog._abort()
        self.assertEqual(kill.call_count, 2)
        self.assertEqual(kill.call_args_list[0].args[1], __import__('signal').SIGABRT)
        self.assertEqual(kill.call_args_list[1].args[1], __import__('signal').SIGKILL)
        sleep.assert_called_once_with(5)


class SystemdWatchdogTest(IsolatedAsyncioTestCase):
    """Optional systemd watchdog client tests."""

    def test_disabled_watchdog_environment_fails_startup(self):
        """The explicit systemd option rejects an inactive systemd environment."""
        module = types.ModuleType('systemd_watchdog')

        class DisabledWatchDog(object):
            is_enabled = False

        module.WatchDog = DisabledWatchDog
        with patch.dict(sys.modules, {'systemd_watchdog': module}):
            with self.assertRaises(RuntimeError):
                _start_systemd_watchdog()

    async def test_heartbeat_runs_on_main_loop(self):
        """The notifier calls ready and sends periodic pings from an asyncio task."""
        class WatchDog(object):
            timeout = 1000

            def __init__(self):
                self.ready_calls = 0
                self.ping_calls = 0

            def ready(self):
                self.ready_calls += 1

            def ping(self):
                self.ping_calls += 1

        watchdog = WatchDog()
        task = asyncio.create_task(_systemd_watchdog_loop(watchdog))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(watchdog.ready_calls, 1)
        self.assertGreater(watchdog.ping_calls, 1)
