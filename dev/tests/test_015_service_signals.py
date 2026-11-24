"""
``atasks.commands.service.sig_handler()`` tests
"""
import signal
from unittest import TestCase
from unittest.mock import patch

import atasks.commands.service as run_module


class SigHandlerTest(TestCase):
    """The first signal requests a graceful exit, a repeated one forces it"""

    def setUp(self):
        run_module.exit_run = False

    def tearDown(self):
        run_module.exit_run = False

    def test_first_signal_requests_graceful_exit(self):
        """The first signal only sets exit_run and does not touch the process"""
        with patch('atasks.commands.service.os.kill') as kill, patch('atasks.commands.service.signal.signal') as set_handler:
            run_module.sig_handler(signal.SIGTERM, None)

        self.assertTrue(run_module.exit_run)
        kill.assert_not_called()
        set_handler.assert_not_called()

    def test_second_signal_forces_exit(self):
        """A repeated signal restores the default handler and re-sends the signal to the process"""
        run_module.exit_run = True
        with patch('atasks.commands.service.os.kill') as kill, patch('atasks.commands.service.signal.signal') as set_handler:
            run_module.sig_handler(signal.SIGTERM, None)

        set_handler.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        kill.assert_called_once_with(kill.call_args.args[0], signal.SIGTERM)
