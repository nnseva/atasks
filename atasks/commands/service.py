"""
Script to start atasks file in server mode
"""
import argparse
import asyncio
import importlib
import logging
import logging.config
import math
import os
import signal
import sys
import threading
import time


if __name__ == '__main__':
    logger = logging.getLogger('atasks.command.service')
else:
    logger = logging.getLogger(__name__)

exit_run = False

# Keys accepted inside one -N/--namespace SPEC (see _parse_namespace_spec).
_NAMESPACE_SPEC_KEYS = frozenset((
    'name', 'mode', 'transport', 'url', 'hostname',
    'max-trace-depth', 'trace-filter-modules', 'collect-await-frames',
))


def _parse_health_http(value):
    """Parse a ``HOST[:PORT][/PATH]`` health endpoint binding."""
    value = value.strip()
    if not value or '?' in value or '#' in value or '@' in value:
        raise argparse.ArgumentTypeError('health HTTP address must be HOST[:PORT][/PATH]')

    address, separator, path_suffix = value.partition('/')
    path = '/' + path_suffix if separator else '/'
    if not address:
        raise argparse.ArgumentTypeError('health HTTP address must include a host')

    if address.startswith('['):
        closing = address.find(']')
        if closing == -1:
            raise argparse.ArgumentTypeError('IPv6 health HTTP host must end with ]')
        host = address[1:closing]
        remainder = address[closing + 1:]
        if not remainder:
            port_value = '80'
        elif remainder.startswith(':'):
            port_value = remainder[1:]
        else:
            raise argparse.ArgumentTypeError('invalid IPv6 health HTTP address')
    elif address.count(':') <= 1:
        if ':' in address:
            host, port_value = address.rsplit(':', 1)
        else:
            host, port_value = address, '80'
    else:
        raise argparse.ArgumentTypeError('IPv6 health HTTP host must be enclosed in []')

    if not host:
        raise argparse.ArgumentTypeError('health HTTP address must include a host')
    try:
        port = int(port_value)
    except ValueError:
        raise argparse.ArgumentTypeError('health HTTP port must be an integer')
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('health HTTP port must be between 1 and 65535')
    return host, port, path


def _positive_seconds(value):
    """Parse a finite, strictly positive duration in seconds."""
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('duration must be a number of seconds')
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError('duration must be a finite positive number of seconds')
    return seconds


async def _start_health_http(address):
    """Start the optional aiohttp loop-liveness endpoint."""
    try:
        from aiohttp import web  # type: ignore[import-not-found]
    except ImportError as ex:
        raise RuntimeError('HTTP liveness requires the aiohttp extra: pip install atasks[aiohttp]') from ex

    host, port, path = address

    async def health_handler(request):
        logger.debug('Received health check request at %s', path)
        return web.Response(text='OK\n')

    application = web.Application()
    application.router.add_get(path, health_handler)
    runner = web.AppRunner(application)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host=host, port=port)
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    logger.info('Serving asyncio loop liveness endpoint at http://%s:%s%s', host, port, path)
    return runner


class _SelfWatchdog(object):
    """Probe the main event loop from a thread and abort if it stops responding."""
    def __init__(self, loop, period, timeout, kill_after):
        self.loop = loop
        self.period = period
        self.timeout = timeout
        self.kill_after = kill_after
        self._stop = threading.Event()
        self._acknowledgement = None
        self._thread = threading.Thread(target=self._run, name='atasks-self-watchdog', daemon=True)

    def start(self):
        """Start monitoring the configured event loop."""
        self._thread.start()

    def stop(self):
        """Stop monitoring and wait briefly for the worker thread to leave."""
        self._stop.set()
        if self._acknowledgement is not None:
            self._acknowledgement.set()
        logger.info('Stopping self-watchdog thread (<=%ss)', self.period + self.timeout + 1)
        self._thread.join(timeout=self.period + self.timeout + 1)

    def _run(self):
        while not self._stop.wait(self.period):
            acknowledgement = threading.Event()
            self._acknowledgement = acknowledgement
            try:
                self.loop.call_soon_threadsafe(acknowledgement.set)
            except RuntimeError:
                if self._stop.is_set():
                    return
                self._abort()
                return

            logger.debug('Waiting up to %s seconds for main asyncio loop acknowledgement', self.timeout)
            acknowledged = acknowledgement.wait(self.timeout)
            if acknowledged and not self._stop.is_set():
                logger.debug('Main asyncio loop acknowledged self-watchdog probe')
            if acknowledged or self._stop.is_set():
                continue
            self._abort()
            return

    def _abort(self):
        logger.critical('The main asyncio event loop did not respond within %s seconds; aborting', self.timeout)
        os.kill(os.getpid(), signal.SIGABRT)
        time.sleep(self.kill_after)
        os.kill(os.getpid(), signal.SIGKILL)


def _start_systemd_watchdog():
    """Create the optional systemd watchdog client and validate its environment."""
    try:
        import systemd_watchdog  # type: ignore[import-not-found]
    except ImportError as ex:
        raise RuntimeError(
            'systemd watchdog support requires the systemd-watchdog extra: '
            'pip install atasks[systemd-watchdog]'
        ) from ex

    watchdog = systemd_watchdog.WatchDog()
    if not watchdog.is_enabled:
        raise RuntimeError('--systemd-watchdog requires a configured systemd watchdog environment')
    return watchdog


async def _systemd_watchdog_loop(watchdog):
    """Send systemd watchdog heartbeats from the main asyncio event loop."""
    interval = watchdog.timeout / (2 * 10**6)  # Convert microseconds to seconds and divide by 2 for safety margin
    watchdog.ready()
    watchdog.ping()
    while True:
        await asyncio.sleep(interval)
        watchdog.ping()


def _parse_namespace_spec(spec):
    """
    Parse one -N/--namespace command-line value.

    SPEC is a comma-separated ``key=value`` list describing one namespace's
    Transport and Router configuration, e.g.::

        name=orders,mode=server,transport=amqp,url=amqp://host/,hostname=worker-1,
        max-trace-depth=500,trace-filter-modules=atasks:backoff,collect-await-frames=false

    Recognized keys:

    - ``name``  - namespace name (default: ``default``)
    - ``mode`` - ``client``, ``server``, or ``loopback``, default: ``loopback``.
        A ``server`` namespace has its Router activated against its Transport and waits for incoming requests/events.
        A ``client`` namespace only connects its Transport to send requests/events/broadcasts.
        A ``loopback`` namespace acts as both a client and a server within the same process,
        but doesn't wait for incoming requests/events from external clients.
    - ``url`` - URL passed to the transport, e.g. the AMQP broker URL when
      ``transport=amqp``
    - ``hostname`` - default: auto-detected via ``socket.gethostname()``
    - ``max-trace-depth`` - integer, default: ``1000``
    - ``trace-filter-modules`` - ``:``-separated dotted module name prefixes,
      default: none filtered
    - ``collect-await-frames`` - ``true``/``false``, default: ``true``

    See the Router constructor (``atasks.router.Router``) for what each of the
    last four actually control.

    :param spec: the raw -N/--namespace argument value
    :type spec: str
    :returns: parsed namespace options, every key present (defaulted where omitted)
    :rtype: dict
    :raises argparse.ArgumentTypeError: for an unknown key, a missing ``name``,
                                         a key given twice, or an invalid value
    """
    raw = {}
    for item in spec.split(','):
        item = item.strip()
        if not item:
            continue
        if '=' not in item:
            raise argparse.ArgumentTypeError("expected key=value, got %r in %r" % (item, spec))
        key, value = item.split('=', 1)
        key = key.strip()
        if key not in _NAMESPACE_SPEC_KEYS:
            raise argparse.ArgumentTypeError(
                "unknown key %r in %r, expected one of: %s" % (key, spec, ', '.join(sorted(_NAMESPACE_SPEC_KEYS)))
            )
        if key in raw:
            raise argparse.ArgumentTypeError("key %r given twice in %r" % (key, spec))
        raw[key] = value.strip()

    name = raw.get('name', 'default')

    mode = raw.get('mode', 'loopback')
    if mode not in ('client', 'server', 'loopback'):
        raise argparse.ArgumentTypeError("mode must be 'client', 'server', or 'loopback', got %r in %r" % (mode, spec))

    transport = raw.get('transport', 'loopback')
    if transport not in ('loopback', 'amqp'):
        raise argparse.ArgumentTypeError("transport must be 'loopback' or 'amqp', got %r in %r" % (transport, spec))

    max_trace_depth = raw.get('max-trace-depth')
    if max_trace_depth is not None:
        try:
            max_trace_depth = int(max_trace_depth)
        except ValueError:
            raise argparse.ArgumentTypeError(
                "max-trace-depth must be an integer, got %r in %r" % (max_trace_depth, spec)
            )

    trace_filter_modules = raw.get('trace-filter-modules')
    if trace_filter_modules is not None:
        trace_filter_modules = [module for module in trace_filter_modules.split(':') if module]

    collect_await_frames = raw.get('collect-await-frames')
    if collect_await_frames is not None:
        lowered = collect_await_frames.lower()
        if lowered in ('true', 'yes', '1'):
            collect_await_frames = True
        elif lowered in ('false', 'no', '0'):
            collect_await_frames = False
        else:
            raise argparse.ArgumentTypeError(
                "collect-await-frames must be true/false, got %r in %r" % (collect_await_frames, spec)
            )

    return {
        'name': name,
        'mode': mode,
        'transport': transport,
        'url': raw.get('url'),
        'hostname': raw.get('hostname'),
        'max_trace_depth': max_trace_depth,
        'trace_filter_modules': trace_filter_modules,
        'collect_await_frames': collect_await_frames,
    }


async def _disconnect_connected_transports(namespace_specs):
    """
    Disconnect every namespace's transport that is currently connected.

    :param namespace_specs: parsed -N/--namespace specs (only ``name`` is used)
    :type namespace_specs: list[dict]
    """
    from atasks.transport.base import get_transport

    for ns in namespace_specs:
        transport = get_transport(ns['name'])
        if transport is not None and transport.is_connected():
            await transport.disconnect()


async def aiomain(**options):
    """The non-task main function calls tasks from atasks worker, not self process"""
    from atasks.codecs import PickleCodec
    from atasks.router import Router, get_router
    from atasks.transport.backends.amqp import AMQPTransport
    from atasks.transport.base import LoopbackTransport

    transports = {}
    server_namespaces = []
    for ns in options['namespaces']:
        name = ns['name']
        PickleCodec(namespace=name)

        kw = {'namespace': name}
        if ns['transport'] == 'amqp':
            kw['url'] = ns['url']
        transport = {
            'loopback': LoopbackTransport,
            'amqp': AMQPTransport,
        }[ns['transport']](**kw)
        try:
            await transport.connect()
            transports[name] = transport
        except Exception as e:
            logger.error('Failed to connect transport for namespace %s: %s', name, e, exc_info=True)
            await _disconnect_connected_transports(options['namespaces'])
            return False

        router_kwargs = {}
        if ns['hostname'] is not None:
            router_kwargs['hostname'] = ns['hostname']
        if ns['max_trace_depth'] is not None:
            router_kwargs['max_trace_depth'] = ns['max_trace_depth']
        if ns['trace_filter_modules'] is not None:
            router_kwargs['trace_filter_modules'] = ns['trace_filter_modules']
        if ns['collect_await_frames'] is not None:
            router_kwargs['collect_await_frames'] = ns['collect_await_frames']
        if router_kwargs:
            Router(namespace=name, **router_kwargs)

        if ns['mode'] in ('server', 'loopback'):
            server_namespaces.append(name)

    health_runner = None
    self_watchdog = None
    systemd_watchdog_task = None
    health_address = options.get('health_http')
    try:
        if health_address is not None:
            health_runner = await _start_health_http(health_address)

        if options.get('self_watchdog'):
            self_watchdog = _SelfWatchdog(
                asyncio.get_running_loop(),
                options.get('self_watchdog_period', 10),
                options.get('self_watchdog_timeout', 30),
                options.get('self_watchdog_kill_after', 5),
            )
            self_watchdog.start()

        routers = {name: get_router(namespace=name) for name in transports}
        futures = []
        for filename in options['scenario']:
            if os.path.exists(filename) and os.path.isfile(filename):
                name = os.path.basename(filename).rsplit('.', 1)[0]
                spec = importlib.util.spec_from_file_location(name, filename)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            else:
                module = importlib.import_module(filename)

            if hasattr(module, 'aiomain'):
                futures.append(module.aiomain(**options))

        for name in server_namespaces:
            await routers[name].activate(transports[name])
        try:
            if options.get('systemd_watchdog'):
                systemd_watchdog_task = asyncio.create_task(_systemd_watchdog_loop(_start_systemd_watchdog()))

            if futures:
                logger.info("Running scenario modules")
                await asyncio.gather(*futures)

            should_listen = any(ns['mode'] == 'server' for ns in options['namespaces'])
            should_listen = should_listen or health_address is not None
            should_listen = should_listen or options.get('self_watchdog') or options.get('systemd_watchdog')
            if should_listen:
                for s in set([
                    signal.SIGINT,
                    signal.SIGQUIT,
                    signal.SIGTERM,
                ]):
                    signal.signal(s, sig_handler)
                logger.info("Listening for requests")

                while not exit_run:
                    await asyncio.sleep(1)

                logger.info("Execution stopped")
        finally:
            if systemd_watchdog_task is not None:
                systemd_watchdog_task.cancel()
                try:
                    await systemd_watchdog_task
                except asyncio.CancelledError:
                    pass
            for name in server_namespaces:
                await routers[name].deactivate()
    finally:
        if self_watchdog is not None:
            self_watchdog.stop()
        if health_runner is not None:
            await health_runner.cleanup()
        await _disconnect_connected_transports(options['namespaces'])


def sig_handler(sig_num, stack_frame):
    """Signal handler"""
    global exit_run
    signal_names = dict((s.value, s.name) for s in signal.Signals)
    logger.info("Signal %s[%s] cought, exiting...", signal_names.get(sig_num, 'UNKNOWN'), sig_num)
    exit_run = True


def main(argv=None):
    """Module main"""
    if argv is None:
        argv = sys.argv

    parser = argparse.ArgumentParser(
        prog=argv[0],
        formatter_class=argparse.RawTextHelpFormatter,
        description="Run task service scenario modules and serve their declared tasks"
    )
    parser.add_argument(
        'scenario',
        nargs='*',
        help='File or module name(s) to be loaded, may be multiple',
    )

    parser.add_argument(
        '-o', '--option',
        nargs='*',
        dest='opt',
        help='Additional options available to analize in the module aiomain function/future',
    )
    parser.add_argument(
        '--health-http',
        metavar='HOST[:PORT][/PATH]',
        type=_parse_health_http,
        help='Serve an asyncio-loop liveness endpoint; default port/path are 80 and /',
    )
    parser.add_argument(
        '--self-watchdog',
        action='store_true',
        help='Terminate this process when its main asyncio loop stops responding',
    )
    parser.add_argument(
        '--self-watchdog-period',
        type=_positive_seconds,
        default=10,
        help='Seconds between self-watchdog probes; default: %(default)s',
    )
    parser.add_argument(
        '--self-watchdog-timeout',
        type=_positive_seconds,
        default=30,
        help='Seconds a self-watchdog probe may wait; default: %(default)s',
    )
    parser.add_argument(
        '--self-watchdog-kill-after',
        type=_positive_seconds,
        default=5,
        help='Seconds after SIGABRT before the self-watchdog sends SIGKILL; default: %(default)s',
    )
    parser.add_argument(
        '--systemd-watchdog',
        action='store_true',
        help='Send watchdog heartbeats through the optional systemd-watchdog package',
    )

    parser.add_argument(
        '-N', '--namespace',
        dest='namespaces',
        metavar='SPEC',
        action='append',
        type=_parse_namespace_spec,
        default=None,
        help=("""
Configure one namespace to run - may be given multiple times, once per
namespace, each with its own Transport/Router setup and mode. SPEC is a
comma-separated key=value list:
    name                      default: default
                                Namespace name
    mode=client|server        default: client
                                A 'server' namespace has its Router activated.
                                If any namespace is 'server', the process then
                                listens for requests until a signal arrives
    transport=loopback|amqp   default: loopback
                                The 'loopback' transport should be used for
                                local testing only. The 'amqp' transport
                                may be used for real deployments.
    url                       default: <empty>
                                Only for AMQP transport, <empty> means
                                amqp://guest:guest@localhost/
    hostname                  default: <auto-detected>
                                Is used to determine the hostname for the
                                namespace during atask tracing
    max-trace-depth           default: 1000
                                Maximum depth for tracing atask stack
    collect-await-frames=true|false       default: true
                                Whether to collect await frames during
                                atask tracing
    trace-filter-modules=MOD1:MOD2:...    default: <none filtered>
                                Modules to be filtered from await
                                frames during atask tracing

Default when omitted entirely: a single name=default namespace
with loopback transport. See README.md (Commands section)
for the full reference and examples.
        """),
    )

    parser.add_argument(
        '-v', '--verbosity',
        dest='verbosity',
        type=int,
        default=1,
        choices=(0, 1, 2, 3, 4),
        help='Verbosity level, default: %(default)s',
    )
    parser.add_argument(
        '-L', '--loggers',
        nargs='*',
        dest='loggers',
        help='Logger(s) to be activated to the verbosity level, may be several, default is atasks',
    )

    args = parser.parse_args(argv[1:])
    options = vars(args)

    if not options['self_watchdog']:
        watchdog_options = (
            '--self-watchdog-period',
            '--self-watchdog-timeout',
            '--self-watchdog-kill-after',
        )
        if any(arg == option or arg.startswith(option + '=') for arg in argv[1:] for option in watchdog_options):
            parser.error('self-watchdog timing options require --self-watchdog')

    # No -N/--namespace at all: fall back to a single default-configured
    # 'default' namespace (client mode, loopback transport) - the same
    # behaviour running with zero namespace options always had.
    if options['namespaces'] is None:
        options['namespaces'] = [_parse_namespace_spec('name=default')]

    seen = set()
    for ns in options['namespaces']:
        if ns['name'] in seen:
            parser.error("namespace %r given more than once via -N/--namespace" % ns['name'])
        seen.add(ns['name'])

    LOGGING = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'verbose': {
                'format': '[%(asctime)s] %(levelname)s(%(module)s) <%(process)d/%(thread)d> %(message)s'
            },
        },
        'handlers': {
            'console': {
                'level': 'DEBUG',
                'class': 'logging.StreamHandler',
                'formatter': 'verbose',
            },
        },
    }
    LOGGING['loggers'] = LOGGING.get('loggers', {})
    for logger in options['loggers'] if options['loggers'] is not None else ['atasks']:
        conf = LOGGING['loggers'].get(logger, {})
        if not conf:
            conf = {
                'handlers': ['console'],
                'propagate': False,
            }
        conf['level'] = [
            0,
            'ERROR',
            'WARNING',
            'INFO',
            'DEBUG'
        ][options['verbosity']]
        LOGGING['loggers'][logger] = conf

    logging.config.dictConfig(LOGGING)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(aiomain(**options))
    finally:
        # A loop left open here is only closed implicitly, whenever the
        # garbage collector gets around to it - by which point interpreter
        # shutdown may already have torn down other state, turning any
        # still-pending task's teardown into "Event loop is closed" /
        # "no running event loop" errors instead of a clean cancellation.
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == '__main__':
    sys.path.insert(0, '..')
    main(sys.argv)
