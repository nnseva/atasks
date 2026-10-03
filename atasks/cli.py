"""Top-level command-line interface for ATasks."""
import argparse
import os
import sys


def main(argv=None):
    """Run an ATasks subcommand."""
    if argv is None:
        argv = sys.argv

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description='Asynchronous task management framework command-line interface',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser(
        'service',
        help='Run task service scenario modules and serve their declared tasks',
    )
    subparsers.add_parser(
        'refs',
        help='Generate a _refs.py module with lightweight references to declared tasks',
    )
    subparsers.add_parser(
        'help',
        help='Show this help message',
    )
    options = parser.parse_args(argv[1:2])

    if options.command == 'service':
        from atasks.commands.service import main as service_main
        service_main([os.path.basename(argv[0]) + ' service'] + argv[2:])
    elif options.command == 'refs':
        from atasks.commands.refs import main as refs_main
        refs_main([os.path.basename(argv[0]) + ' refs'] + argv[2:])
    elif options.command == 'help':
        parser.print_help()
