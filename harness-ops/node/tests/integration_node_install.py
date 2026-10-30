#!/usr/bin/env python3
"""A new private installation upgrades while an actual PF owner keeps its fd."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

from test_node_install import NodeInstallTests, PROGRAM

PF = str(Path(sys.argv.pop(1)).resolve())


class InstalledPhoneLock(NodeInstallTests):
    program = '''
import os,sys
if sys.argv[1:2] == ['fixture-hold'] and sys.argv[-1] != 'pf-child':
    os.execv(PF, [PF, 'lease', '--serial', 'fixture-phone', '--', sys.executable,
                 __file__, *sys.argv[1:], 'pf-child'])
'''.replace('PF', repr(PF)) + PROGRAM

    def check_phone_lock(self, held):
        for scope in (['--serial', 'fixture-phone'], []):
            result = subprocess.run([PF, 'lease', *scope, '--timeout-ms', '0', '--', '/usr/bin/true'],
                env={**os.environ, 'DEVLOCK_DIR': str(self.root / 'locks'), 'HG_CONFIG_DIR': str(self.root / 'config')},
                capture_output=True, timeout=10)
            self.assertEqual(result.returncode == 0, not held, result.stderr.decode(errors='replace'))


if __name__ == '__main__':
    unittest.main(defaultTest='InstalledPhoneLock.test_join_restart_and_upgrade_preserve_identity_and_a_live_lock_fd')
