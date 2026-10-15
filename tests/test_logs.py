''' log rotation: the file rolls over at its size limit and keeps a bounded history '''

import logging
import sys
import tempfile
import unittest
from pathlib import Path

from shelly import logs


class Rotation(unittest.TestCase):

    def setUp(self):
        root = logging.getLogger()
        saved = root.handlers[:], root.level, sys.excepthook
        self.addCleanup(self.restore, saved)

    def restore(self, saved):
        root = logging.getLogger()
        for handler in root.handlers:
            handler.close()
        root.handlers[:], level, sys.excepthook = saved
        root.setLevel(level)

    def test_the_file_rolls_over_and_keeps_only_so_many(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'nested', 'shelly.log')
            logs.configure(path, max_bytes=2000, backups=3)

            for i in range(200):
                logging.getLogger('shelly').info('line %d %s', i, 'x' * 40)

            names = sorted(p.name for p in path.parent.iterdir())

            self.assertEqual(names, ['shelly.log', 'shelly.log.1', 'shelly.log.2', 'shelly.log.3'])
            self.assertTrue(all(p.stat().st_size <= 2000 for p in path.parent.iterdir()))
            self.assertIn('line 199', path.read_text())

    def test_per_request_http_lines_are_quiet_but_warnings_get_through(self):
        logs.configure(None)

        for name in ('httpx', 'httpx2'):
            self.assertFalse(logging.getLogger(name).isEnabledFor(logging.INFO), name)
            self.assertTrue(logging.getLogger(name).isEnabledFor(logging.WARNING), name)

    def test_without_a_path_logs_go_to_stderr(self):
        handler = logs.configure(None)
        self.assertIsInstance(handler, logging.StreamHandler)
        self.assertNotIsInstance(handler, logging.FileHandler)

    def test_an_uncaught_exception_is_logged(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'shelly.log')
            logs.configure(path)

            try:
                raise RuntimeError('kaboom')
            except RuntimeError:
                sys.excepthook(*sys.exc_info())

            self.assertIn('kaboom', path.read_text())


if __name__ == '__main__':
    unittest.main()
