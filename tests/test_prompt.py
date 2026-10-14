''' the system prompt: the right parts in the right order, and the clock in the right zone '''

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shelly.core.prompt import GUIDE, PROFILE_HEADER, Prompt, load_profile

CHICAGO = ZoneInfo('America/Chicago')


class Build(unittest.TestCase):

    def test_guide_then_profile_then_clock(self):
        blocks = Prompt('Call me M.', CHICAGO).build(now=datetime(2026, 10, 14, 15, 40, tzinfo=CHICAGO))

        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0]['text'].startswith(GUIDE))
        self.assertTrue(blocks[0]['text'].endswith(f'{PROFILE_HEADER}\nCall me M.'))
        self.assertEqual(blocks[1]['text'], 'Current date and time for the operator: '
                                            'Wednesday 14 October 2026, 15:40 (CDT, America/Chicago).')

    def test_capability_context_sits_between_the_profile_and_the_clock(self):
        blocks = Prompt('p', CHICAGO).build(['notes here'])

        self.assertEqual([b['text'] for b in blocks][1], 'notes here')
        self.assertTrue(blocks[2]['text'].startswith('Current date and time'))

    def test_no_profile_means_no_header(self):
        self.assertNotIn(PROFILE_HEADER, Prompt(None, CHICAGO).build()[0]['text'])

    def test_the_clock_is_in_the_operators_zone(self):
        text = Prompt(None, CHICAGO).build()[1]['text']
        self.assertTrue(text.endswith('America/Chicago).'))

    def test_the_stable_part_does_not_change_between_turns(self):
        prompt = Prompt('p', CHICAGO)
        first = prompt.build(now=datetime(2026, 1, 1, 9, tzinfo=CHICAGO))
        later = prompt.build(now=datetime(2026, 7, 1, 21, tzinfo=CHICAGO))

        self.assertEqual(first[0], later[0])
        self.assertNotEqual(first[1], later[1])

    def test_without_a_zone_the_clock_is_utc_not_naive(self):
        self.assertIn('UTC', Prompt().build()[1]['text'])


class LoadProfile(unittest.TestCase):

    def test_a_profile_file_is_read_and_trimmed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'profile.md')
            path.write_text('\n  I am M.  \n', encoding='utf-8')
            self.assertEqual(load_profile(path), 'I am M.')

    def test_a_missing_or_empty_profile_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertLogs('shelly.core.prompt', level='WARNING'):
                self.assertIsNone(load_profile(Path(d, 'nope.md')))

            empty = Path(d, 'empty.md')
            empty.write_text('  \n', encoding='utf-8')
            self.assertIsNone(load_profile(empty))


if __name__ == '__main__':
    unittest.main()
