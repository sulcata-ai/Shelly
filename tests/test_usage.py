''' what a turn cost: the arithmetic, the fallback case, and that every turn is recorded

The traps: the fallback's top-level usage covering only the last attempt, a model
with no price passing as free, and a failed turn going unrecorded even though it
paid for the requests before it failed.
'''

import asyncio
import unittest

import anthropic
import httpx2

from shelly.core.agent import Agent
from shelly.core.tools import Registry
from shelly.core.usage import TurnUsage
from tests.fakes import FakeClaude, FakeHistory, response, text, tool_use, usage
from tests.test_agent import echo_tool


class Pricing(unittest.TestCase):

    def test_opus_5_5_rates(self):
        turn = TurnUsage(1)
        turn.add(response(use=usage(input_tokens=1_000_000, output_tokens=100_000,
                                    cache_read=1_000_000, cache_write=1_000_000)))

        # 4.00 + 2.00 + 0.20 + 5.00
        self.assertAlmostEqual(turn.document()['cost_usd'], 11.20)

    def test_fallback_attempts_are_priced_at_their_own_models(self):
        attempts = [
            SimpleNS(model='claude-opus-5-5', input_tokens=1_000_000, output_tokens=0),
            SimpleNS(model='claude-opus-5', input_tokens=1_000_000, output_tokens=100_000),
        ]
        turn = TurnUsage(1)
        # the top-level usage covers the last attempt only; it must not be added again
        turn.add(response(model='claude-opus-5', use=usage(input_tokens=1_000_000, output_tokens=100_000,
                                                           iterations=attempts)))
        doc = turn.document()

        self.assertAlmostEqual(doc['cost_usd'], 4.00 + 5.00 + 2.50)
        self.assertEqual(doc['input_tokens'], 2_000_000)
        self.assertEqual(doc['models'], ['claude-opus-5-5', 'claude-opus-5'])

    def test_an_unknown_model_is_unknown_cost_not_free(self):
        turn = TurnUsage(1)
        turn.add(response(model='claude-someday-9'))

        with self.assertLogs('shelly.core.usage', level='WARNING'):
            self.assertIsNone(turn.document()['cost_usd'])


def SimpleNS(**fields):
    from types import SimpleNamespace
    fields.setdefault('cache_read_input_tokens', 0)
    fields.setdefault('cache_creation_input_tokens', 0)
    return SimpleNamespace(type='message', **fields)


class FakeLedger:

    def __init__(self, fail=False):
        self.docs, self.fail = [], fail

    async def record(self, doc):
        if self.fail:
            raise ConnectionError('atlas down')
        self.docs.append(doc)


def run(coroutine):
    return asyncio.run(coroutine)


class Recording(unittest.TestCase):

    def make(self, *responses, tools=(), error=None, ledger=None):
        registry = Registry()
        registry.add(*tools)
        self.ledger = ledger or FakeLedger()
        return Agent(FakeClaude(*responses, error=error), FakeHistory(), registry,
                     'claude-opus-5-5', 'medium', ledger=self.ledger)

    def test_a_turn_sums_its_requests_and_names_its_tools(self):
        agent = self.make(
            response(tool_use('echo', {'value': 'x'}), stop='tool_use', use=usage(1000, 50)),
            response(text('done'), use=usage(1200, 30)),
            tools=[echo_tool()],
        )

        with self.assertLogs('shelly.core.agent', level='INFO'):
            run(agent.reply(7, 'go'))

        [doc] = self.ledger.docs
        self.assertEqual((doc['chat_id'], doc['outcome'], doc['requests']), (7, 'reply', 2))
        self.assertEqual((doc['input_tokens'], doc['output_tokens']), (2200, 80))
        self.assertEqual(doc['tools'], ['echo'])
        self.assertAlmostEqual(doc['cost_usd'], (2200 * 4 + 80 * 20) / 1_000_000)

    def test_failed_turns_are_recorded_too(self):
        error = anthropic.APIConnectionError(request=httpx2.Request('POST', 'https://api.anthropic.com'))

        cases = ((lambda: self.make(response(stop='refusal')), 'refusal'),
                 (lambda: self.make(error=error), 'error'))

        for make, outcome in cases:
            with self.subTest(outcome=outcome):
                agent = make()
                with self.assertLogs('shelly.core.agent'):
                    run(agent.reply(1, 'x'))
                self.assertEqual(self.ledger.docs[-1]['outcome'], outcome)

    def test_a_broken_ledger_does_not_cost_the_reply(self):
        agent = self.make(response(text('hi')), ledger=FakeLedger(fail=True))

        with self.assertLogs('shelly.core.agent', level='ERROR'):
            self.assertEqual(run(agent.reply(1, 'hello')), 'hi')


if __name__ == '__main__':
    unittest.main()
