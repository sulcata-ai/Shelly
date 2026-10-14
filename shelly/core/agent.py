''' one turn of the conversation: history in, Claude and its tools, reply out

A manual loop rather than the SDK's tool runner: the registry already holds the
tools as plain handlers, and the loop is where per-turn limits, refusals and
tool errors are decided -- all of which are clearer written out than hooked in.
'''

import asyncio
import logging
from collections import defaultdict

import anthropic

from shelly.core.prompt import Prompt
from shelly.core.usage import TurnUsage


log = logging.getLogger(__name__)

# Generous for chat; a reply that hits it is cut off, and says so.
MAX_TOKENS = 16000

# Rounds of tool calls in one turn before giving up. A turn that needs more is
# almost always a loop, and each round is a paid request.
MAX_STEPS = 10

# On a safety decline, re-run the request server-side on the model Anthropic
# recommends for that category, instead of handing back a dead message.
FALLBACK_BETA = 'server-side-fallback-2026-07-01'

ERROR_REPLY = 'Something went wrong on my side and I could not answer. Try again in a moment.'
REFUSAL_REPLY = "I can't help with that one."
STEPS_REPLY = 'That took more steps than I allow myself in one go, so I stopped. Try narrowing it down?'
CUT_OFF_NOTE = '\n\n(cut off -- that reply ran past my length limit)'


class Agent:
    ''' the core: knows Claude, the history and the tools, and nothing about channels '''

    def __init__(self, client, history, registry, model, effort, prompt=None, ledger=None):
        self.client = client
        self.history = history
        self.registry = registry
        self.model = model
        self.effort = effort
        self.prompt = prompt or Prompt()
        self.ledger = ledger
        self._locks = defaultdict(asyncio.Lock)

    async def reply(self, chat_id, text):
        ''' answer one message, recording both sides; always returns text to send

        Turns for a chat run one at a time, so the history is in the order things
        happened. The user's message is recorded before Claude is asked; the
        reply only when it is a real one. A failure, refusal or give-up is sent
        but not stored, so the next turn's window joins the unanswered message
        with the next one instead of teaching Claude its own error messages.

        Every turn that reached Claude is costed and recorded, failures included:
        a failed turn can still have paid for the requests before it failed.
        '''

        async with self._locks[chat_id]:
            await self.history.add(chat_id, 'user', text)
            messages = await self.history.window(chat_id)
            turn = TurnUsage(chat_id)

            try:
                answer, keep = await self._run(messages, turn)
            except anthropic.APIError as error:
                # The SDK has already retried what is worth retrying.
                log.error('claude request failed: %s', error)
                turn.outcome = 'error'
                answer, keep = ERROR_REPLY, False

            if keep:
                await self.history.add(chat_id, 'assistant', answer)

            await self._record(turn)

            return answer

    async def _record(self, turn):
        ''' log the turn's cost and keep it; a ledger failure never costs the reply '''

        doc = turn.document()
        cost = 'unknown' if doc['cost_usd'] is None else f'${doc["cost_usd"]:.4f}'
        log.info('turn %s: %s, %d request(s), in=%d out=%d cache_read=%d, %s, %.1fs',
                 doc['chat_id'], doc['outcome'], doc['requests'], doc['input_tokens'],
                 doc['output_tokens'], doc['cache_read_input_tokens'], cost, doc['seconds'])

        if self.ledger is None:
            return

        try:
            await self.ledger.record(doc)
        except Exception:
            log.exception('could not record turn usage')

    async def _run(self, messages, turn):
        ''' the request/tool loop; returns (reply text, worth storing) and fills in turn '''

        messages = list(messages)
        request = {
            'model': self.model,
            'max_tokens': MAX_TOKENS,
            'system': self.prompt.build(await self.registry.context()),
            'output_config': {'effort': self.effort},
            'betas': [FALLBACK_BETA],
            'fallbacks': 'default',
        }

        if len(self.registry):
            request['tools'] = self.registry.params()

        for _ in range(MAX_STEPS):
            response = await self.client.beta.messages.create(messages=messages, **request)
            turn.add(response)
            stop = response.stop_reason

            if stop == 'refusal':
                # Only reached when the fallback model declined too.
                details = getattr(response, 'stop_details', None)
                log.warning('refused: %s', getattr(details, 'category', None))
                turn.outcome = 'refusal'
                return REFUSAL_REPLY, False

            if stop in ('tool_use', 'pause_turn'):
                # The assistant turn goes back exactly as it came, thinking blocks
                # and all: within a turn the model needs to see its own reasoning.
                messages.append({'role': 'assistant', 'content': response.content})

                if stop == 'tool_use':
                    turn.called([b.name for b in response.content if b.type == 'tool_use'])
                    messages.append({'role': 'user', 'content': await self._call_tools(response.content)})

                continue

            text = '\n\n'.join(b.text for b in response.content if b.type == 'text' and b.text).strip()

            if not text:
                log.warning('empty reply, stop_reason=%s', stop)
                turn.outcome = 'empty'
                return ERROR_REPLY, False

            if stop == 'max_tokens':
                turn.outcome = 'cut_off'
                return text + CUT_OFF_NOTE, True

            turn.outcome = 'reply'
            return text, True

        log.warning('gave up after %d tool rounds', MAX_STEPS)
        turn.outcome = 'steps'
        return STEPS_REPLY, False

    async def _call_tools(self, content):
        ''' run every tool call in the response at once; all results go back in one message '''

        calls = [b for b in content if b.type == 'tool_use']
        outcomes = await asyncio.gather(*(self.registry.run(c.name, c.input) for c in calls))

        return [
            {'type': 'tool_result', 'tool_use_id': call.id, 'content': text, 'is_error': is_error}
            for call, (text, is_error) in zip(calls, outcomes)
        ]


def client(cfg):
    ''' the async Anthropic client for this configuration '''

    return anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)
