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


logger = logging.getLogger(__name__)

exit_run = False


async def aiomain(**options):
    """The non-task main function calls tasks from atasks worker, not self process"""
    from atasks.codecs import PickleCodec
    from atasks.router import get_router
    from atasks.transport.backends.amqp import AMQPTransport
    from atasks.transport.base import LoopbackTransport

    PickleCodec()
    kw = {}
    if options['transport'] in ('amqp',):
        kw = {
            'url': options['url']
        }
    transport = {
        'loopback': LoopbackTransport,
        'amqp': AMQPTransport
    }[options['transport']](**kw)
    await transport.connect()
    try:
        router = get_router()
        if options['mode'] in ('server', 'loopback'):
            await router.activate(transport)
        try:
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

            if futures:
                await asyncio.gather(*futures)

            if options['mode'] == 'server':
                logger.info("Listening for requests")
                for s in set([
                    signal.SIGINT,
                    signal.SIGQUIT,
                    signal.SIGTERM,
                ]):
                    signal.signal(s, sig_handler)

                while not exit_run:
                    await asyncio.sleep(1)

                logger.info("Execution stopped")
        finally:
            # Mirrors transport.connect() above: whatever happens in the body
            # (exception, signal-triggered exit, or plain fallthrough for the
            # no-scenario client case), the server callback registered via
            # router.activate() above must be torn down before the transport
            # itself is disconnected below.
            if options['mode'] in ('server', 'loopback'):
                await router.deactivate()
    finally:
        # Without this, e.g. AMQPTransport leaves its underlying connection
        # (and the reader/writer/heartbeat/reconnect tasks aio_pika runs on
        # top of it) open when this coroutine returns. Those tasks are then
        # still pending when the process exits, and get torn down mid-flight
        # by the interpreter instead of shutting down cleanly - surfacing as
        # "Task was destroyed but it is pending!" / "Event loop is closed"
        # noise on every run, even a scenario-less one.
        await transport.disconnect()


def sig_handler(sig_num, stack_frame):
    """Signal handler"""
    global exit_run
    signal_names = dict((s.value, s.name) for s in signal.Signals)
    logger.info("Signal %s[%s] cought, exiting...", signal_names.get(sig_num, 'UNKNOWN'), sig_num)
    exit_run = True


def main(argv):
    """Module main"""
    parser = argparse.ArgumentParser()
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
        '-U', '--url',
        dest='url',
        help='URL for the transport',
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

    parser.add_argument(
        '-M', '--mode',
        choices=['client', 'server', 'loopback'],
        dest='mode',
        default='client',
        help='Mode to be evaluated',
    )

    parser.add_argument(
        '-T', '--transport',
        choices=['loopback', 'amqp'],
        dest='transport',
        default='loopback',
        help='Transport to be used',
    )
    args = parser.parse_args(argv[1:])
    options = vars(args)

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
