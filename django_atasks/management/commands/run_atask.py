"""
Script to start atasks file in server mode
"""
import asyncio
import importlib
import logging
import os
import signal
import sys

from django.conf import settings
from django.core.management.base import BaseCommand


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Start scenarios file in a client mode
    """

    help = __doc__

    exit_run = False

    def add_arguments(self, parser):
        """Adding arguments for a parser."""
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

    def handle(self, *args, **options):
        """Command handler."""

        LOGGING = {}
        LOGGING.update(settings.LOGGING)
        LOGGING['loggers'] = LOGGING.get('loggers', {})
        for logger in options['loggers'] if options['loggers'] is not None else ['atasks', 'django_atasks']:
            conf = LOGGING['loggers'].get(logger, {})
            if not conf:
                conf = {
                    'handlers': ['console'],
                    'level': 'ERROR',
                    'propagate': False,
                }
            conf['level'] = [
                'ERROR',
                'WARNING',
                'INFO',
                'DEBUG'
            ][options['verbosity']]
            LOGGING['loggers'][logger] = conf

        logging.config.dictConfig(LOGGING)

        loop = asyncio.get_event_loop()
        loop.run_until_complete(aiomain(**options))


async def aiomain(**options):
    """The non-task main function calls tasks from atasks worker, not self process"""
    from atasks.transport.base import LoopbackTransport
    from atasks.transport.backends.amqp import AMQPTransport
    from atasks.router import get_router
    from atasks.codecs import PickleCodec

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
    router = get_router()
    if options['mode'] in ('server', 'loopback'):
        await router.activate(transport)

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

        while not Command.exit_run:
            await asyncio.sleep(1)

        logger.info("Execution stopped")


def sig_handler(sig_num, stack_frame):
    """Signal handler"""
    signal_names = dict((s.value, s.name) for s in signal.Signals)
    logger.info("Signal %s[%s] cought, exiting...", signal_names.get(sig_num, 'UNKNOWN'), sig_num)
    Command.exit_run = True
