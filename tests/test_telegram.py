''' the Telegram channel: who gets answered, and that a reply always arrives

No network: the bot and the agent are fakes. The traps are a stranger getting any
response at all, an HTML rejection losing the reply, and a store failure leaving
the operator with silence.
'''

import asyncio
import unittest
from types import SimpleNamespace

from telegram.error import BadRequest

from shelly.channels import telegram as channel_module
from shelly.channels.telegram import Channel, send

OPERATOR = 1111
STRANGER = 9999


class FakeBot:

    def __init__(self, reject_html=False):
        self.sent = []
        self.actions = []
        self.reject_html = reject_html

    async def send_message(self, chat_id, text, parse_mode=None):
        if parse_mode and self.reject_html:
            raise BadRequest("Can't parse entities")
        self.sent.append((chat_id, text, parse_mode))

    async def send_chat_action(self, chat_id, action):
        self.actions.append(chat_id)


class FakeAgent:

    def __init__(self, answer='hello', error=None):
        self.answer, self.error = answer, error
        self.seen = []

    async def reply(self, chat_id, text):
        self.seen.append((chat_id, text))
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return self.answer


def update(chat_id, text='hi'):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(text=text),
    )


def run(coroutine):
    return asyncio.run(coroutine)


class Allowlist(unittest.TestCase):

    def setUp(self):
        self.agent, self.bot = FakeAgent(), FakeBot()
        self.channel = Channel(self.agent, frozenset({OPERATOR}))
        self.context = SimpleNamespace(bot=self.bot)

    def test_a_stranger_gets_nothing_at_all(self):
        with self.assertLogs(channel_module.log, level='WARNING'):
            run(self.channel.on_text(update(STRANGER), self.context))
            run(self.channel.on_start(update(STRANGER), self.context))
            run(self.channel.on_other(update(STRANGER), self.context))

        self.assertEqual(self.agent.seen, [])
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.bot.actions, [], 'not even "typing..."')

    def test_the_operator_is_answered(self):
        run(self.channel.on_text(update(OPERATOR, 'hey'), self.context))

        self.assertEqual(self.agent.seen, [(OPERATOR, 'hey')])
        self.assertEqual(self.bot.sent, [(OPERATOR, 'hello', 'HTML')])
        self.assertEqual(self.bot.actions, [OPERATOR])

    def test_non_text_gets_a_polite_no(self):
        run(self.channel.on_other(update(OPERATOR), self.context))

        self.assertEqual(self.bot.sent[0][1], channel_module.TEXT_ONLY)
        self.assertEqual(self.agent.seen, [])

    def test_a_failed_turn_still_answers(self):
        self.channel.agent = FakeAgent(error=RuntimeError('mongo down'))

        with self.assertLogs(channel_module.log, level='ERROR'):
            run(self.channel.on_text(update(OPERATOR), self.context))

        self.assertEqual(self.bot.sent[0][1], channel_module.BROKEN)


class Send(unittest.TestCase):

    def test_rejected_html_falls_back_to_plain_text(self):
        bot = FakeBot(reject_html=True)

        with self.assertLogs(channel_module.log, level='WARNING'):
            run(send(bot, OPERATOR, '**hi** there'))

        self.assertEqual(bot.sent, [(OPERATOR, '**hi** there', None)])

    def test_a_long_reply_arrives_in_pieces_in_order(self):
        bot = FakeBot()
        text = '\n\n'.join(f'part {i} ' + 'x' * 1000 for i in range(8))
        run(send(bot, OPERATOR, text))

        self.assertGreater(len(bot.sent), 1)
        self.assertTrue(all(len(t) <= 4096 for _, t, _ in bot.sent))
        self.assertTrue(bot.sent[0][1].startswith('part 0'))
        self.assertIn('part 7', bot.sent[-1][1])

    def test_html_that_grows_past_the_limit_goes_plain(self):
        bot = FakeBot()
        run(send(bot, OPERATOR, '<' * 3000))  # each < becomes &lt;: 12000 characters of HTML

        self.assertEqual(bot.sent, [(OPERATOR, '<' * 3000, None)])


if __name__ == '__main__':
    unittest.main()
