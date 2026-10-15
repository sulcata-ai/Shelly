''' what a capability gives Claude: tools, prompt context, pollers, and the registry that collects them '''

import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    ''' one tool Claude can call

    handler is async, takes the tool's input as keyword arguments and returns
    the text Claude reads back. Raising is fine: the registry turns it into an
    error result Claude can see and work around, rather than a failed turn.
    '''

    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Awaitable[str]]

    def param(self):
        ''' the tool as the API takes it

        strict, so the input Claude sends always validates against the schema
        and handlers need not re-check shapes. That needs every property listed
        in "required" and additionalProperties false in the schema.
        '''

        return {
            'name': self.name,
            'description': self.description,
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

        try:
            result = await tool.handler(**tool_input)
        except Exception as error:  # a broken tool must not take the turn down with it
            log.exception('tool %s failed', name)
            return f'{name} failed: {type(error).__name__}: {error}', True

        if not isinstance(result, str):
            result = json.dumps(result, default=str)

        return result, False
