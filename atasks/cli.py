"""Top-level command-line interface for ATasks."""
import argparse
import sys


def main(argv=None):
    """Run an ATasks subcommand."""
    if argv is None:
        argv = sys.argv

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser(
        'service',
        help='Run task service scenario modules',
    )
    subparsers.add_parser(
        'help',
        help='Show this help message',
    )
    options = parser.parse_args(argv[1:2])

    if options.command == 'service':
        from atasks.service import main as service_main
        service_main([argv[0] + ' service'] + argv[2:])
    elif options.command == 'help':
        parser.print_help()
