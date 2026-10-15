''' what a tool may need to know about the turn it runs in

Set by the agent for the length of a turn. A context variable rather than an
argument to every handler: most tools do not care, and asyncio copies it into
the tasks that run a turn's tool calls concurrently.
'''

from contextvars import ContextVar

# The chat the current turn belongs to -- where a tool's later follow-ups (a
# reminder, say) should be sent. None outside a turn.
current_chat = ContextVar('current_chat', default=None)
