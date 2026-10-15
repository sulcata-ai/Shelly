''' what a capability gives Claude: tools, prompt context, pollers, and the registry that collects them '''

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from shelly.core.context import current_chat


log = logging.getLogger(__name__)

# Appended to the description of a tool that asks first, so Claude calls it
# rather than asking for permission in chat and then being asked again by the button.
ASKS_FIRST = (' (Before this runs, the operator is shown what it will do and taps approve or '
              'decline. Call it directly; do not ask for permission in chat first.)')

NOT_DONE = ('Not done: {why}. Nothing was changed. Tell the operator it was not done, and do '
            'not try it again unless they ask.')


@dataclass(frozen=True)
class Tool:
    ''' one tool Claude can call

    handler is async, takes the tool's input as keyword arguments and returns
    the text Claude reads back. Raising is fine: the registry turns it into an
    error result Claude can see and work around, rather than a failed turn.

    confirm, for a tool with side effects outside Shelly (sending, spending,
    deleting), takes the input as a dict and returns one line saying what the
    call will do, in the operator's terms; the operator must approve that line
    before the handler runs. It may be async, to look up what the input names
    (an event's title, not its id). Tools that only touch Shelly's own memory leave it
    None and run unasked.
    '''

    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Awaitable[str]]
    confirm: Callable[[dict], str | Awaitable[str]] | None = None

    def param(self):
        ''' the tool as the API takes it

        strict, so the input Claude sends always validates against the schema
        and handlers need not re-check shapes. That needs every property listed
        in "required" and additionalProperties false in the schema.
        '''

        return {
            'name': self.name,
            'description': self.description + (ASKS_FIRST if self.confirm else ''),
            'input_schema': self.input_schema,
            'strict': True,
        }


class Registry:
    ''' every tool the capabilities registered, in registration order

    The order is kept rather than sorted so the tools block is byte-identical
    from request to request; it sits at the very front of the prompt, and any
    change to it would invalidate a cache built on top.
    '''

    def __init__(self):
        self._tools = {}
        self._contexts = []
        self._pollers = []
        # async (chat_id, action) -> (approved, why), set by the channel, which
        # is the one that can put a question in front of the operator. Until it
        # is set, a tool that asks first does not run.
        self.approver = None

    def add(self, *tools):
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f'two tools are named {tool.name!r}')

            self._tools[tool.name] = tool

    def add_context(self, provider):
        ''' an async callable returning text for the system prompt each turn, or None

        For what Claude should always know rather than have to ask for -- saved
        notes, say. Called once per turn, in registration order.
        '''

        self._contexts.append(provider)

    async def context(self):
        ''' every provider's text for this turn; a failing provider is left out, not fatal

        A turn without the notes is worse but still useful; a turn that fails
        because the notes could not be read is neither.
        '''

        texts = []

        for provider in self._contexts:
            try:
                text = await provider()
            except Exception:
                log.exception('context provider %s failed; leaving it out this turn',
                              getattr(provider, '__qualname__', provider))
                continue

            if text:
                texts.append(text)

        return texts

    def add_poller(self, poller):
        ''' an async callable(now) returning [(chat_id, text)] for Shelly to send unprompted

        For what a capability has to say when nobody asked -- a reminder coming
        due. The channel calls every poller on a timer. A poller marks what it
        returns as handled: it is sent once, not again on the next tick.
        '''

        self._pollers.append(poller)

    async def poll(self, now):
        ''' every poller's messages now; a failing poller is logged and skipped this tick '''

        messages = []

        for poller in self._pollers:
            try:
                messages.extend(await poller(now))
            except Exception:
                log.exception('poller %s failed', getattr(poller, '__qualname__', poller))

        return messages

    def __len__(self):
        return len(self._tools)

    def params(self):
        return [tool.param() for tool in self._tools.values()]

    async def run(self, name, tool_input):
        ''' run one call; returns (text, is_error) and never raises '''

        tool = self._tools.get(name)

        if tool is None:
            return f'There is no tool named {name!r}.', True

        if tool.confirm is not None:
            why = await self._refusal(tool, tool_input)

            if why is not None:
                return NOT_DONE.format(why=why), True

        try:
            result = await tool.handler(**tool_input)
        except Exception as error:  # a broken tool must not take the turn down with it
            log.exception('tool %s failed', name)
            return f'{name} failed: {type(error).__name__}: {error}', True

        if not isinstance(result, str):
            result = json.dumps(result, default=str)

        return result, False

    async def _refusal(self, tool, tool_input):
        ''' None if the operator approved this call, else why it must not run

        Fails closed: no way to ask, or asking failing, is a no.
        '''

        try:
            action = tool.confirm(tool_input)

            if inspect.isawaitable(action):
                action = await action
        except Exception as error:
            log.exception('could not describe a %s call for approval', tool.name)
            return f'could not describe it for approval ({type(error).__name__}: {error})'

        if self.approver is None:
            log.warning('%s needs approval and there is no one to ask', tool.name)
            return 'there is no way to ask the operator for approval here'

        try:
            approved, why = await self.approver(current_chat.get(), action)
        except Exception:
            log.exception('asking for approval of %s failed', tool.name)
            return 'asking the operator for approval failed'

        return None if approved else why
