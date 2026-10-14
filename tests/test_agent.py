''' the core loop: what goes to Claude, what comes back, and what is remembered

The traps: tool results split across messages, thinking blocks dropped between
tool rounds, an error reply stored as if Claude had said it, a loop that never
ends, and one tool's failure taking the whole turn down.
'''

import asyncio
import unittest

import anthropic
import httpx2

from shelly.core import agent as agent_module
from shelly.core.agent import Agent
from shelly.core.tools import Registry, Tool
from tests.fakes import FakeClaude, FakeHistory, response, text, thinking, tool_use


def echo_tool(name='echo', fail=False):
    async def handler(value):
        if fail:
            raise RuntimeError('boom')
        return f'echo:{value}'

    schema = {'type': 'object', 'properties': {'value': {'type': 'string'}},
              'required': ['value'], 'additionalProperties': False}

    return Tool(name=name, description='echo the value back', input_schema=schema, handler=handler)


def run(coroutine):
    return asyncio.run(coroutine)


class AgentCase(unittest.TestCase):

    def make(self, *responses, tools=(), error=None):
        self.claude = FakeClaude(*responses, error=error)
        self.history = FakeHistory()
        registry = Registry()
        registry.add(*tools)

        return Agent(self.claude, self.history, registry, model='claude-opus-5-5', effort='medium')

    def stored(self):
        return [(d['role'], d['text']) for d in self.history.docs]


class PlainChat(AgentCase):

    def test_a_reply_is_returned_and_both_sides_are_stored(self):
        agent = self.make(response(thinking(), text('Hello!')))

        self.assertEqual(run(agent.reply(1, 'hi')), 'Hello!')
        self.assertEqual(self.stored(), [('user', 'hi'), ('assistant', 'Hello!')])

    def test_the_request_carries_model_effort_fallback_and_no_empty_tools(self):
        agent = self.make(response(text('ok')))
        run(agent.reply(1, 'hi'))
        request = self.claude.requests[0]

        self.assertEqual(request['model'], 'claude-opus-5-5')
        self.assertEqual(request['output_config'], {'effort': 'medium'})
        self.assertEqual(request['fallbacks'], 'default')
        self.assertIn(agent_module.FALLBACK_BETA, request['betas'])
        self.assertNotIn('tools', request)
        self.assertNotIn('thinking', request, 'thinking cannot be configured off on this model; leave it out')
        self.assertTrue(request['system'][1]['text'].startswith('Current date and time'))

    def test_the_clock_is_read_once_per_turn(self):
        agent = self.make(
            response(tool_use('echo', {'value': 'x'}), stop='tool_use'),
            response(text('done')),
            tools=[echo_tool()],
        )
        run(agent.reply(1, 'go'))

        self.assertIs(self.claude.requests[0]['system'], self.claude.requests[1]['system'])

    def test_earlier_turns_are_in_the_window(self):
        agent = self.make(response(text('first')), response(text('second')))
        run(agent.reply(1, 'one'))
        run(agent.reply(1, 'two'))

        self.assertEqual(self.claude.requests[1]['messages'], [
            {'role': 'user', 'content': 'one'},
            {'role': 'assistant', 'content': 'first'},
            {'role': 'user', 'content': 'two'},
        ])


class Failures(AgentCase):

    def test_an_api_error_is_answered_but_not_remembered(self):
        error = anthropic.APIConnectionError(request=httpx2.Request('POST', 'https://api.anthropic.com'))
        agent = self.make(error=error)

        self.assertEqual(run(agent.reply(1, 'hi')), agent_module.ERROR_REPLY)
        self.assertEqual(self.stored(), [('user', 'hi')])

    def test_a_refusal_is_answered_but_not_remembered(self):
        agent = self.make(response(stop='refusal', category='cyber'))

        self.assertEqual(run(agent.reply(1, 'x')), agent_module.REFUSAL_REPLY)
        self.assertEqual(self.stored(), [('user', 'x')])

    def test_a_cut_off_reply_says_so_and_is_kept(self):
        agent = self.make(response(text('long...'), stop='max_tokens'))
        answer = run(agent.reply(1, 'essay please'))

        self.assertTrue(answer.startswith('long...'))
        self.assertIn('cut off', answer)
        self.assertEqual(self.stored()[-1], ('assistant', answer))

    def test_an_empty_reply_is_not_remembered(self):
        agent = self.make(response(thinking()))

        self.assertEqual(run(agent.reply(1, 'hi')), agent_module.ERROR_REPLY)
        self.assertEqual(self.stored(), [('user', 'hi')])

    def test_after_a_failure_the_next_window_joins_the_unanswered_message(self):
        agent = self.make(response(stop='refusal'), response(text('ok')))
        run(agent.reply(1, 'first'))
        run(agent.reply(1, 'second'))

        self.assertEqual(self.claude.requests[1]['messages'],
                         [{'role': 'user', 'content': 'first\n\nsecond'}])


class Tools(AgentCase):

    def test_a_tool_round_trip(self):
        agent = self.make(
            response(thinking(), tool_use('echo', {'value': 'x'}), stop='tool_use'),
            response(text('done')),
            tools=[echo_tool()],
        )

        self.assertEqual(run(agent.reply(1, 'use the tool')), 'done')

        second = self.claude.requests[1]['messages']
        assistant, results = second[-2], second[-1]

        self.assertEqual([b.type for b in assistant['content']], ['thinking', 'tool_use'],
                         'the assistant turn must go back whole, thinking included')
        self.assertEqual(results['content'], [
            {'type': 'tool_result', 'tool_use_id': 'tu_1', 'content': 'echo:x', 'is_error': False}])
        self.assertEqual(self.claude.requests[0]['tools'][0]['strict'], True)

    def test_only_the_final_text_is_stored(self):
        agent = self.make(
            response(tool_use('echo', {'value': 'x'}), stop='tool_use'),
            response(text('done')),
            tools=[echo_tool()],
        )
        run(agent.reply(1, 'go'))

        self.assertEqual(self.stored(), [('user', 'go'), ('assistant', 'done')])

    def test_parallel_calls_return_in_one_message(self):
        agent = self.make(
            response(tool_use('echo', {'value': 'a'}, id='t1'),
                     tool_use('echo', {'value': 'b'}, id='t2'), stop='tool_use'),
            response(text('both')),
            tools=[echo_tool()],
        )
        run(agent.reply(1, 'two at once'))
        results = self.claude.requests[1]['messages'][-1]['content']

        self.assertEqual([(r['tool_use_id'], r['content']) for r in results],
                         [('t1', 'echo:a'), ('t2', 'echo:b')])

    def test_a_failing_tool_is_an_error_result_not_a_failed_turn(self):
        agent = self.make(
            response(tool_use('echo', {'value': 'x'}), stop='tool_use'),
            response(text('it broke, sorry')),
            tools=[echo_tool(fail=True)],
        )

        with self.assertLogs('shelly.core.tools', level='ERROR'):
            self.assertEqual(run(agent.reply(1, 'go')), 'it broke, sorry')

        result = self.claude.requests[1]['messages'][-1]['content'][0]
        self.assertTrue(result['is_error'])
        self.assertIn('boom', result['content'])

    def test_an_unknown_tool_is_an_error_result(self):
        agent = self.make(
            response(tool_use('nope', {}), stop='tool_use'),
            response(text('ok')),
            tools=[echo_tool()],
        )
        run(agent.reply(1, 'go'))

        self.assertTrue(self.claude.requests[1]['messages'][-1]['content'][0]['is_error'])

    def test_a_tool_loop_is_cut_off(self):
        endless = [response(tool_use('echo', {'value': 'x'}), stop='tool_use')
                   for _ in range(agent_module.MAX_STEPS)]
        agent = self.make(*endless, tools=[echo_tool()])

        self.assertEqual(run(agent.reply(1, 'go')), agent_module.STEPS_REPLY)
        self.assertEqual(len(self.claude.requests), agent_module.MAX_STEPS)
        self.assertEqual(self.stored(), [('user', 'go')])


class Reset(AgentCase):

    def test_after_reset_claude_sees_only_new_messages(self):
        agent = self.make(response(text('first')), response(text('second')))

        async def go():
            await agent.reply(1, 'old topic')
            await agent.reset(1)
            await agent.reply(1, 'new topic')

        run(go())

        self.assertEqual(self.claude.requests[1]['messages'], [{'role': 'user', 'content': 'new topic'}])

    def test_a_reset_during_a_turn_lands_after_it(self):
        agent = self.make(response(text('answer')))

        async def go():
            await asyncio.gather(agent.reply(1, 'q'), agent.reset(1))

        run(go())

        self.assertEqual([d['role'] for d in self.history.docs], ['user', 'assistant', 'reset'])


class RegistryRules(unittest.TestCase):

    def test_duplicate_names_are_refused(self):
        registry = Registry()
        registry.add(echo_tool())

        with self.assertRaises(ValueError):
            registry.add(echo_tool())

    def test_order_is_registration_order(self):
        registry = Registry()
        registry.add(echo_tool('zeta'), echo_tool('alpha'))

        self.assertEqual([p['name'] for p in registry.params()], ['zeta', 'alpha'])


class OneTurnAtATime(AgentCase):

    def test_turns_for_one_chat_do_not_interleave(self):
        agent = self.make(response(text('a1')), response(text('a2')))

        async def both():
            await asyncio.gather(agent.reply(1, 'q1'), agent.reply(1, 'q2'))

        run(both())

        self.assertEqual([r for r, _ in self.stored()], ['user', 'assistant', 'user', 'assistant'])


if __name__ == '__main__':
    unittest.main()
