''' the system prompt: the right parts in the right order, and nothing in it that drifts per turn '''

import tempfile
import unittest
from pathlib import Path

from shelly.core.prompt import GUIDE, PROFILE_HEADER, Prompt, load_profile


class Build(unittest.TestCase):

    def test_guide_and_profile_then_capability_context(self):
        blocks = Prompt('Call me M.').build(['notes here'])

        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0]['text'].startswith(GUIDE))
        self.assertTrue(blocks[0]['text'].endswith(f'{PROFILE_HEADER}\nCall me M.'))
        self.assertEqual(blocks[1]['text'], 'notes here')

    def test_no_profile_means_no_header(self):
        self.assertNotIn(PROFILE_HEADER, Prompt(None).build()[0]['text'])

    def test_nothing_changes_between_turns_on_its_own(self):
        # no clock: the time lives on the operator's messages, so the system
        # prompt is a stable cache prefix
        prompt = Prompt('p')
        self.assertEqual(prompt.build(['x']), prompt.build(['x']))
        self.assertNotIn('Current date', str(prompt.build()))

    def test_only_the_stable_block_is_a_cache_point(self):
        blocks = Prompt('p').build(['notes here'])

        self.assertEqual(blocks[0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual([b for b in blocks[1:] if 'cache_control' in b], [])


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
