"""
Script to start scenarios file in a client mode
"""

import asyncio
import logging

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """
    Start scenarios file in a client mode
    """

    help = __doc__

    def add_arguments(self, parser):
        """Adding arguments for a parser."""
        parser.add_argument(
            '-U', '--url',
            dest='url',
            help='URL for the transport',
        )

        parser.add_argument(
            '--loggers', '-L',
            nargs='*',
            dest='loggers',
            help='Logger(s) to be activated to the verbosity level, may be several, default is atasks',
        )

        parser.add_argument(
            '--mode', '-M',
            choices=['client', 'server', 'loopback'],
            dest='mode',
            default='client',
            help='Mode to be evaluated',
        )

        parser.add_argument(
            '--transport', '-T',
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
        for logger in options['loggers'] if options['loggers'] is not None else ['atasks']:
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

        from tests import scenarios

        loop = asyncio.get_event_loop()
        loop.run_until_complete(scenarios.aiomain(**options))
