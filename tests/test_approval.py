''' approval before side effects: the gate in the registry and the prompt in Telegram

The traps: a tool with side effects running without a yes (no channel to ask,
the ask failing, a tap from the wrong chat), a waiting prompt blocking the chat
when the operator moves on, and a turn that waits forever.
'''

import asyncio
import unittest
from types import SimpleNamespace

from shelly.channels import telegram as channel_module
from shelly.channels.telegram import Approvals, Channel
from shelly.core import tools as tools_module
from shelly.core.agent import Agent
from shelly.core.tools import Registry, Tool
from tests.fakes import FakeClaude, FakeHistory, response, text, tool_use

OPERATOR = 1111
STRANGER = 9999

SCHEMA = {'type': 'object', 'properties': {'to': {'type': 'string'}},
          'required': ['to'], 'additionalProperties': False}


def send_tool(ran, confirm=lambda i: f'Send a postcard to {i["to"]}'):
    async def handler(to):
        ran.append(to)
        return f'sent to {to}'

    return Tool(name='send', description='Send a postcard.', input_schema=SCHEMA,
                handler=handler, confirm=confirm)


def run(coroutine):
    return asyncio.run(coroutine)


class Gate(unittest.TestCase):

    def setUp(self):
        self.ran, self.asked = [], []
        self.registry = Registry()
        self.registry.add(send_tool(self.ran))

    def approver(self, answer):
        async def ask(chat_id, action):
            self.asked.append((chat_id, action))
            return answer

        return ask

    def test_approved_runs_and_the_operator_saw_what_it_would_do(self):
        self.registry.approver = self.approver((True, None))
        tools_module.current_chat.set(OPERATOR)

        self.assertEqual(run(self.registry.run('send', {'to': 'Ana'})), ('sent to Ana', False))
        self.assertEqual(self.asked, [(OPERATOR, 'Send a postcard to Ana')])

    def test_declined_does_not_run_and_claude_is_told_why(self):
        self.registry.approver = self.approver((False, 'the operator declined'))
        result, is_error = run(self.registry.run('send', {'to': 'Ana'}))

        self.assertEqual(self.ran, [])
        self.assertTrue(is_error)
        self.assertIn('the operator declined', result)
        self.assertIn('do not try it again', result)

    def test_no_one_to_ask_means_no(self):
        with self.assertLogs(tools_module.log, level='WARNING'):
            result, is_error = run(self.registry.run('send', {'to': 'Ana'}))

        self.assertEqual((self.ran, is_error), ([], True))

    def test_asking_failing_means_no(self):
        async def broken(chat_id, action):
            raise ConnectionError('telegram down')

        self.registry.approver = broken

        with self.assertLogs(tools_module.log, level='ERROR'):
            _, is_error = run(self.registry.run('send', {'to': 'Ana'}))

        self.assertEqual((self.ran, is_error), ([], True))

    def test_a_confirm_that_raises_means_no(self):
        registry = Registry()
        registry.add(send_tool(self.ran, confirm=lambda i: i['missing']))
        registry.approver = self.approver((True, None))

        with self.assertLogs(tools_module.log, level='ERROR'):
            _, is_error = run(registry.run('send', {'to': 'Ana'}))

        self.assertEqual((self.ran, self.asked, is_error), ([], [], True))

    def test_a_tool_without_confirm_is_never_asked_about(self):
        registry = Registry()
        registry.add(send_tool(self.ran, confirm=None))
        registry.approver = self.approver((False, 'no'))

        self.assertEqual(run(registry.run('send', {'to': 'Ana'})), ('sent to Ana', False))
        self.assertEqual(self.asked, [])

    def test_the_description_says_it_asks_first(self):
        asks = send_tool(self.ran).param()['description']
        plain = send_tool(self.ran, confirm=None).param()['description']

        self.assertEqual(plain, 'Send a postcard.')
        self.assertEqual(asks, 'Send a postcard.' + tools_module.ASKS_FIRST)

    def test_the_agent_asks_for_the_chat_whose_turn_it_is(self):
        claude = FakeClaude(response(tool_use('send', {'to': 'Ana'}), stop='tool_use'),
                            response(text('Done.')))
        self.registry.approver = self.approver((True, None))
        agent = Agent(claude, FakeHistory(), self.registry, model='claude-opus-5-5', effort='medium')

        self.assertEqual(run(agent.reply(42, 'send Ana a postcard')), 'Done.')
        self.assertEqual(self.asked, [(42, 'Send a postcard to Ana')])
        self.assertEqual(self.ran, ['Ana'])


class FakeBot:

    def __init__(self):
        self.sent, self.edits, self.actions = [], [], []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, text, chat_id, message_id):
        self.edits.append((chat_id, message_id, text))

    async def send_chat_action(self, chat_id, action):
        self.actions.append(chat_id)


def buttons(bot):
    ''' the callback data of the last prompt's buttons '''

    markup = bot.sent[-1][2]
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def tap(approvals, bot, chat_id, which):
    ''' wait for the prompt to go out, then answer it '''

    while not bot.sent:
        await asyncio.sleep(0)

    _, key, _ = buttons(bot)[0].split(':')
    return approvals.answer(key, chat_id, which)


class Prompt(unittest.TestCase):

    def setUp(self):
        self.bot = FakeBot()
        self.approvals = Approvals(self.bot, timeout=5)

    def ask_and(self, then):
        async def go():
            return await asyncio.gather(self.approvals.ask(OPERATOR, 'Send a postcard to Ana'), then())

        return run(go())

    def test_yes(self):
        decision, settled = self.ask_and(lambda: tap(self.approvals, self.bot, OPERATOR, 'yes'))

        self.assertEqual(decision, (True, None))
        self.assertTrue(settled)
        self.assertIn('Send a postcard to Ana', self.bot.sent[0][1])
        self.assertEqual(self.bot.edits, [(OPERATOR, 1, '✅ Approved: Send a postcard to Ana')])
        self.assertFalse(self.approvals.waiting(OPERATOR))

    def test_no(self):
        decision, _ = self.ask_and(lambda: tap(self.approvals, self.bot, OPERATOR, 'no'))

        self.assertEqual(decision, (False, 'the operator declined'))
        self.assertTrue(self.bot.edits[0][2].startswith('❌ Declined'))

    def test_a_tap_from_another_chat_settles_nothing(self):
        self.approvals.timeout = 0.05
        decision, settled = self.ask_and(lambda: tap(self.approvals, self.bot, STRANGER, 'yes'))

        self.assertFalse(settled)
        self.assertEqual(decision, (False, 'the operator did not answer in time'))

    def test_silence_times_out_as_no(self):
        self.approvals.timeout = 0.01
        decision = run(self.approvals.ask(OPERATOR, 'Send a postcard to Ana'))

        self.assertEqual(decision[0], False)
        self.assertTrue(self.bot.edits[0][2].startswith('⌛'))
        self.assertFalse(self.approvals.waiting(OPERATOR))

    def test_moving_on_settles_it_as_no(self):
        async def move_on():
            while not self.approvals.waiting(OPERATOR):
                await asyncio.sleep(0)
            self.approvals.supersede(OPERATOR)

        decision, _ = self.ask_and(move_on)

        self.assertEqual(decision, (False, 'the operator sent a new message instead of answering'))

    def test_an_unknown_prompt_settles_nothing(self):
        self.assertFalse(self.approvals.answer('deadbeef', OPERATOR, 'yes'))


class Query:

    def __init__(self, data):
        self.data = data
        self.answered, self.cleared = [], False

    async def answer(self, text=None):
        self.answered.append(text)

    async def edit_message_reply_markup(self, markup):
        self.cleared = True


def tap_update(chat_id, data):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id),
                           effective_user=SimpleNamespace(id=chat_id),
                           callback_query=Query(data))


class FakeAgent:

    def __init__(self):
        self.seen = []

    async def reply(self, chat_id, text):
        self.seen.append(text)
        return 'ok'


class Buttons(unittest.TestCase):

    def setUp(self):
        self.bot = FakeBot()
        self.approvals = Approvals(self.bot, timeout=5)
        self.channel = Channel(FakeAgent(), frozenset({OPERATOR}), approvals=self.approvals)
        self.context = SimpleNamespace(bot=self.bot)

    def test_a_tap_approves(self):
        async def go():
            asking = asyncio.create_task(self.approvals.ask(OPERATOR, 'Send it'))
            while not self.bot.sent:
                await asyncio.sleep(0)
            update = tap_update(OPERATOR, buttons(self.bot)[0])
            await self.channel.on_button(update, self.context)
            return await asking, update.callback_query

        decision, query = run(go())

        self.assertEqual(decision, (True, None))
        self.assertEqual(query.answered, [None])

    def test_a_stale_button_says_expired_and_loses_its_buttons(self):
        update = tap_update(OPERATOR, 'approve:deadbeef:yes')
        run(self.channel.on_button(update, self.context))

        self.assertEqual(update.callback_query.answered, [channel_module.EXPIRED])
        self.assertTrue(update.callback_query.cleared)

    def test_a_stranger_tapping_gets_nothing(self):
        update = tap_update(STRANGER, 'approve:deadbeef:yes')

        with self.assertLogs(channel_module.log, level='WARNING'):
            run(self.channel.on_button(update, self.context))

        self.assertEqual(update.callback_query.answered, [])

    def test_a_new_message_declines_the_waiting_prompt_and_is_answered(self):
        async def go():
            asking = asyncio.create_task(self.approvals.ask(OPERATOR, 'Send it'))
            while not self.approvals.waiting(OPERATOR):
                await asyncio.sleep(0)
            update = SimpleNamespace(effective_chat=SimpleNamespace(id=OPERATOR),
                                     effective_user=SimpleNamespace(id=OPERATOR),
                                     message=SimpleNamespace(text='actually, wait'))
            await self.channel.on_text(update, self.context)
            return await asking

        self.assertEqual(run(go())[0], False)
        self.assertEqual(self.channel.agent.seen, ['actually, wait'])


if __name__ == '__main__':
    unittest.main()
