''' long-term memory: the notes store, the tools Claude uses on it, and the prompt block

The traps: two notes saved in one turn getting the same number, a note that is
really an essay, a note number that does not exist, and a broken store taking the
whole turn down instead of just leaving the notes out.
'''

import asyncio
import unittest
from datetime import datetime, timezone

from shelly.capabilities import memory
from shelly.core.tools import Registry
from shelly.memory.notes import MAX_NOTE, NoteError, Notes
from tests.fakes import FakeDb


def run(coroutine):
    return asyncio.run(coroutine)


class Store(unittest.TestCase):

    def setUp(self):
        self.notes = Notes(FakeDb())

    def test_notes_are_numbered_in_order_and_listed_oldest_first(self):
        async def go():
            first = await self.notes.add('Likes coffee black')
            second = await self.notes.add('Runs on Sundays')
            return first, second, await self.notes.all()

        first, second, saved = run(go())

        self.assertEqual((first, second), (1, 2))
        self.assertEqual([n['text'] for n in saved], ['Likes coffee black', 'Runs on Sundays'])

    def test_two_notes_saved_at_once_get_different_numbers(self):
        async def go():
            return await asyncio.gather(*(self.notes.add(f'fact {i}') for i in range(5)))

        self.assertEqual(sorted(run(go())), [1, 2, 3, 4, 5])

    def test_numbers_are_not_reused_after_a_delete(self):
        async def go():
            await self.notes.add('a')
            two = await self.notes.add('b')
            await self.notes.remove(two)
            return await self.notes.add('c')

        self.assertEqual(run(go()), 3)

    def test_update_and_remove_report_a_missing_note(self):
        self.assertFalse(run(self.notes.update(9, 'x')))
        self.assertFalse(run(self.notes.remove(9)))

    def test_whitespace_is_tidied_and_limits_hold(self):
        async def go():
            await self.notes.add('  two\n\nlines  ')
            return await self.notes.all()

        self.assertEqual(run(go())[0]['text'], 'two lines')

        with self.assertRaises(NoteError):
            run(self.notes.add('   '))

        with self.assertRaises(NoteError):
            run(self.notes.add('x' * (MAX_NOTE + 1)))


class Capability(unittest.TestCase):

    def setUp(self):
        self.notes = Notes(FakeDb())
        self.registry = Registry()
        memory.register(self.registry, self.notes)

    def call(self, name, **tool_input):
        text, is_error = run(self.registry.run(name, tool_input))
        self.assertFalse(is_error)
        return text

    def test_the_tools_are_strict_and_fully_required(self):
        params = {p['name']: p for p in self.registry.params()}

        self.assertEqual(set(params), {'remember', 'update_memory', 'forget'})

        for p in params.values():
            schema = p['input_schema']
            self.assertTrue(p['strict'])
            self.assertFalse(schema['additionalProperties'])
            self.assertEqual(set(schema['required']), set(schema['properties']))

    def test_remember_update_forget_round_trip(self):
        self.assertEqual(self.call('remember', text='Prefers mornings for workouts'), 'Saved as note 1.')
        self.assertEqual(self.call('update_memory', n=1, text='Prefers evenings for workouts'), 'Note 1 updated.')
        self.assertEqual(run(self.notes.all())[0]['text'], 'Prefers evenings for workouts')
        self.assertEqual(self.call('forget', n=1), 'Note 1 deleted.')
        self.assertEqual(run(self.notes.all()), [])

    def test_mistakes_come_back_as_text_claude_can_act_on(self):
        self.assertEqual(self.call('forget', n=7), 'There is no note 7.')
        self.assertEqual(self.call('update_memory', n=7, text='x'), 'There is no note 7.')
        self.assertIn('Split it', self.call('remember', text='x' * (MAX_NOTE + 1)))

    def test_no_notes_means_no_prompt_block(self):
        self.assertEqual(run(self.registry.context()), [])

    def test_notes_are_listed_with_number_and_date(self):
        self.call('remember', text='Goal: emergency fund of six months')
        self.notes.notes.docs[0]['updated_at'] = datetime(2026, 10, 14, tzinfo=timezone.utc)

        [block] = run(self.registry.context())

        self.assertTrue(block.startswith(memory.HEADER))
        self.assertTrue(block.endswith('[1] (2026-10-14) Goal: emergency fund of six months'))


class BrokenContext(unittest.TestCase):

    def test_a_failing_provider_is_left_out_not_fatal(self):
        registry = Registry()

        async def broken():
            raise ConnectionError('atlas unreachable')

        async def fine():
            return 'still here'

        registry.add_context(broken)
        registry.add_context(fine)

        with self.assertLogs('shelly.core.tools', level='ERROR'):
            self.assertEqual(run(registry.context()), ['still here'])


if __name__ == '__main__':
    unittest.main()
