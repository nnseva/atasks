"""
Router tests
"""
from unittest import TestCase

from atasks.run import main


class ModuleTest(TestCase):
    """Module tests"""
    def test_run_atask(self):
        """Test scenarios"""
        main(['run.py', 'dev.tests.scenarios', '--verbosity=3', '--mode=loopback'])
