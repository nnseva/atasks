"""
Script to start atasks file in server mode
"""
import argparse
import asyncio
import importlib
import logging
import logging.config
import os
import signal
import sys


# Not logging.getLogger(__name__): this module is meant to be run as
# `python -m atasks.run`, which makes __name__ '__main__' rather than
# 'atasks.run' - decoupled from the 'atasks' logger hierarchy that -L/
# --loggers (and its 'atasks' default) configures below, so every log call in
# this file would otherwise be silently dropped by logging's lastResort
# handler (WARNING+ only, straight to stderr) whenever actually run that way.
logger = logging.getLogger('atasks.run')

exit_run = False

# Keys accepted inside one -N/--namespace SPEC (see _parse_namespace_spec).
_NAMESPACE_SPEC_KEYS = frozenset((
    'name', 'mode', 'transport', 'url', 'hostname',
    'max-trace-depth', 'trace-filter-modules', 'collect-await-frames',
))


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

    try:
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
            if futures:
                logger.info("Running scenario modules")
                await asyncio.gather(*futures)

            if any(ns['mode'] == 'server' for ns in options['namespaces']):
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
            for name in server_namespaces:
                await routers[name].deactivate()
    finally:
        await _disconnect_connected_transports(options['namespaces'])


def sig_handler(sig_num, stack_frame):
    """Signal handler"""
    global exit_run
    signal_names = dict((s.value, s.name) for s in signal.Signals)
    logger.info("Signal %s[%s] cought, exiting...", signal_names.get(sig_num, 'UNKNOWN'), sig_num)
    exit_run = True


def main(argv):
    """Module main"""
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
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
    sys.path.insert(0, '.')
    main(sys.argv)
