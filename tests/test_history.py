''' the context window: what Claude sees of the stored conversation

The store keeps what happened; the window has to be what the API accepts. The
traps are a window cut on a reply, a failed turn leaving two user messages in a
row, ties in time, and one chat's messages leaking into another's.
'''

import asyncio
import unittest

from shelly.memory.history import History, build_window


def doc(role, text):
    return {'role': role, 'text': text}


class BuildWindow(unittest.TestCase):

    def test_an_ordinary_exchange_passes_through(self):
        docs = [doc('user', 'hi'), doc('assistant', 'hello'), doc('user', 'how are you?')]

        self.assertEqual(build_window(docs), [
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'hello'},
            {'role': 'user', 'content': 'how are you?'},
        ])

    def test_a_window_cut_on_a_reply_opens_on_the_next_user_message(self):
        docs = [doc('assistant', 'older reply'), doc('user', 'question'), doc('assistant', 'answer')]

        self.assertEqual(build_window(docs)[0], {'role': 'user', 'content': 'question'})

    def test_a_failed_turn_is_joined_not_dropped(self):
        docs = [doc('user', 'first try'), doc('user', 'second try'), doc('assistant', 'answer')]

        self.assertEqual(build_window(docs), [
            {'role': 'user', 'content': 'first try\n\nsecond try'},
            {'role': 'assistant', 'content': 'answer'},
        ])

    def test_empty_and_unknown_messages_are_skipped(self):
        docs = [doc('user', ''), doc('system', 'x'), doc('user', 'hi')]

        self.assertEqual(build_window(docs), [{'role': 'user', 'content': 'hi'}])

    def test_a_reset_empties_the_window_from_that_point(self):
        docs = [doc('user', 'old'), doc('assistant', 'old reply'), doc('reset', ''),
                doc('user', 'new')]

        self.assertEqual(build_window(docs), [{'role': 'user', 'content': 'new'}])

    def test_only_the_newest_reset_counts(self):
        docs = [doc('reset', ''), doc('user', 'a'), doc('reset', ''), doc('user', 'b'), doc('assistant', 'c')]

        self.assertEqual([m['content'] for m in build_window(docs)], ['b', 'c'])

    def test_nothing_stored_is_an_empty_window(self):
        self.assertEqual(build_window([]), [])
        self.assertEqual(build_window([doc('assistant', 'orphan')]), [])


class FakeCursor:

    def __init__(self, docs):
        self.docs = docs

    def sort(self, keys):
        for key, direction in reversed(keys):
            self.docs.sort(key=lambda d: d[key], reverse=direction < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self):
        return self.docs


class FakeCollection:
    ''' just enough of an async pymongo collection for History '''

    def __init__(self):
        self.docs = []
        self.indexes = []

    async def create_index(self, keys):
        self.indexes.append(keys)

    async def insert_one(self, document):
        document = dict(document, _id=len(self.docs))
        self.docs.append(document)

    def find(self, query, projection=None):
        return FakeCursor([d for d in self.docs if d['chat_id'] == query['chat_id']])


def run(coroutine):
    return asyncio.run(coroutine)


class Store(unittest.TestCase):

    def setUp(self):
        self.collection = FakeCollection()
        self.history = History(self.collection, limit=4)

    def test_messages_come_back_oldest_first_and_limited(self):
        async def go():
            for i in range(3):
                await self.history.add(1, 'user', f'q{i}')
                await self.history.add(1, 'assistant', f'a{i}')
            return await self.history.window(1)

        self.assertEqual([m['content'] for m in run(go())], ['q1', 'a1', 'q2', 'a2'])

    def test_ties_in_time_keep_insertion_order(self):
        async def go():
            await self.history.add(1, 'user', 'q')
            await self.history.add(1, 'assistant', 'a')
            for d in self.collection.docs:
                d['created_at'] = self.collection.docs[0]['created_at']
            return await self.history.window(1)

        self.assertEqual([m['role'] for m in run(go())], ['user', 'assistant'])

    def test_chats_do_not_leak_into_each_other(self):
        async def go():
            await self.history.add(1, 'user', 'mine')
            await self.history.add(2, 'user', 'theirs')
            return await self.history.window(1)

        self.assertEqual(run(go()), [{'role': 'user', 'content': 'mine'}])

    def test_everything_is_kept_beyond_the_window(self):
        async def go():
            for i in range(10):
                await self.history.add(1, 'user', f'q{i}')

        run(go())

        self.assertEqual(len(self.collection.docs), 10)

    def test_a_reset_hides_but_keeps_everything(self):
        async def go():
            await self.history.add(1, 'user', 'before')
            await self.history.mark_reset(1)
            after_reset = await self.history.window(1)
            await self.history.add(1, 'user', 'after')
            return after_reset, await self.history.window(1)

        after_reset, later = run(go())

        self.assertEqual(after_reset, [])
        self.assertEqual(later, [{'role': 'user', 'content': 'after'}])
        self.assertEqual([d['text'] for d in self.collection.docs if d['role'] == 'user'], ['before', 'after'])

    def test_a_reset_in_one_chat_leaves_others_alone(self):
        async def go():
            await self.history.add(2, 'user', 'theirs')
            await self.history.mark_reset(1)
            return await self.history.window(2)

        self.assertEqual(run(go()), [{'role': 'user', 'content': 'theirs'}])

    def test_a_bad_role_is_refused(self):
        with self.assertRaises(ValueError):
            run(self.history.add(1, 'system', 'x'))


if __name__ == '__main__':
    unittest.main()
