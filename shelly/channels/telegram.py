''' the Telegram channel: long polling, the allowlist, and getting replies onto a phone

The channel knows nothing about Claude. It hands the agent (chat_id, text) and
sends back whatever text it returns. The agent is built inside the running event
loop by a start() coroutine -- the async Mongo and Anthropic clients bind to the
loop they are first used in, and run_polling() owns that loop.
'''

import asyncio
import logging
import secrets
from datetime import datetime, timezone

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, MessageLimit, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, MessageHandler,
                          filters)

from shelly.channels.telegram_format import split, to_html


log = logging.getLogger(__name__)

# Telegram shows "typing..." for about five seconds per action, so it is renewed
# a little more often than that while a turn runs.
TYPING_EVERY = 4

# How often the capabilities' pollers are asked for anything to send unprompted
# (reminders). A reminder can be up to this late.
TICK = 30

GREETING = "Hi, I'm Shelly. Just message me."
RESET_DONE = ('Fresh start. The recent conversation is out of view now, though still on record, '
              'and your saved notes are untouched.')
UNKNOWN_COMMAND = 'I only know /status and /reset.'

# Shown in Telegram's command menu.
COMMANDS = [
    BotCommand('status', 'Uptime, notes and spend'),
    BotCommand('reset', 'Start the conversation afresh (notes are kept)'),
]
TEXT_ONLY = 'I can only read text messages for now.'
BROKEN = 'Something went wrong on my side. Try again in a moment.'

# How long an approval prompt waits for a tap before it counts as a no. The
# chat's turn, and its lock, wait with it.
APPROVAL_TIMEOUT = 600

ASK = 'Okay to do this?\n\n{action}'
EXPIRED = 'That request has expired.'

# outcome -> (approved, why Claude is told it was not done, how the prompt is left)
OUTCOMES = {
    'yes': (True, None, '✅ Approved'),
    'no': (False, 'the operator declined', '❌ Declined'),
    'timeout': (False, 'the operator did not answer in time', '⌛ No answer, not done'),
    'superseded': (False, 'the operator sent a new message instead of answering',
                   '↩️ Skipped, you moved on'),
}


class Approvals:
    ''' approval prompts: a message with two buttons, and the turn waiting on the tap

    The registry's approver. Each prompt has a random id in its buttons'
    callback data, so a tap answers that prompt only; a tap on a prompt from
    before a restart finds nothing and is told so. Every prompt ends edited to
    say how it went, without its buttons.
    '''

    def __init__(self, bot, timeout=APPROVAL_TIMEOUT):
        self.bot = bot
        self.timeout = timeout
        self._pending = {}  # id -> (chat_id, future)

    def waiting(self, chat_id):
        return any(chat == chat_id for chat, _ in self._pending.values())

    async def ask(self, chat_id, action):
        ''' put the action to the operator; returns (approved, why not) '''

        key = secrets.token_hex(4)
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ Do it', callback_data=f'approve:{key}:yes'),
            InlineKeyboardButton("❌ Don't", callback_data=f'approve:{key}:no'),
        ]])
        decided = asyncio.get_running_loop().create_future()

        # Plain text: the action carries Claude's input, which must not be read as markup.
        message = await self.bot.send_message(chat_id, ASK.format(action=action), reply_markup=buttons)
        self._pending[key] = (chat_id, decided)
        log.info('approval %s asked in chat %s: %s', key, chat_id, action)

        try:
            outcome = await asyncio.wait_for(decided, self.timeout)
        except TimeoutError:
            outcome = 'timeout'
        finally:
            del self._pending[key]

        approved, why, label = OUTCOMES[outcome]
        log.info('approval %s: %s', key, outcome)

        try:
            await self.bot.edit_message_text(f'{label}: {action}', chat_id=chat_id,
                                             message_id=message.message_id)
        except TelegramError as error:  # cosmetic: the decision stands either way
            log.warning('could not update approval prompt %s: %s', key, error)

        return approved, why

    def answer(self, key, chat_id, outcome):
        ''' settle a prompt from a tap; false if there is no such prompt waiting in that chat '''

        chat, decided = self._pending.get(key, (None, None))

        if chat != chat_id or decided.done():
            return False

        decided.set_result(outcome)

        return True

    def supersede(self, chat_id):
        ''' the operator moved on: settle every prompt waiting in the chat as a no

        Otherwise the new message would sit behind the waiting turn's lock until
        the prompt timed out.
        '''

        for chat, decided in self._pending.values():
            if chat == chat_id and not decided.done():
                decided.set_result('superseded')


class Channel:
    ''' the handlers, bound to an agent and an allowlist '''

    def __init__(self, agent, allowed_chat_ids, status=None, approvals=None):
        self.agent = agent
        self.allowed = allowed_chat_ids
        self.status = status
        self.approvals = approvals

    def moved_on(self, chat_id):
        if self.approvals is not None:
            self.approvals.supersede(chat_id)

    def allows(self, update):
        ''' true for an allowed chat; anything else is logged and never answered

        Silent on purpose: a reply, even a refusal, confirms to a stranger that
        the bot is alive and worth probing.
        '''

        chat = update.effective_chat

        if chat is not None and chat.id in self.allowed:
            return True

        user = update.effective_user
        log.warning('ignored an update from chat %s (user %s)',
                    chat.id if chat else None, user.id if user else None)

        return False

    async def on_start(self, update, context):
        if self.allows(update):
            await send(context.bot, update.effective_chat.id, GREETING)

    async def on_reset(self, update, context):
        if self.allows(update):
            self.moved_on(update.effective_chat.id)
            await self.agent.reset(update.effective_chat.id)
            await send(context.bot, update.effective_chat.id, RESET_DONE)

    async def on_status(self, update, context):
        if self.allows(update):
            chat_id = update.effective_chat.id
            await send(context.bot, chat_id, await self.status.report(chat_id))

    async def on_unknown(self, update, context):
        if self.allows(update):
            await send(context.bot, update.effective_chat.id, UNKNOWN_COMMAND)

    async def on_text(self, update, context):
        if not self.allows(update):
            return

        chat_id = update.effective_chat.id
        self.moved_on(chat_id)
        paused = (lambda: self.approvals.waiting(chat_id)) if self.approvals else None
        typing = asyncio.create_task(keep_typing(context.bot, chat_id, paused))

        try:
            answer = await self.agent.reply(chat_id, update.message.text)
        except Exception:  # the store, most likely; the agent itself answers its own failures
            log.exception('turn failed in chat %s', chat_id)
            answer = BROKEN
        finally:
            typing.cancel()

        await send(context.bot, chat_id, answer)

    async def on_button(self, update, context):
        ''' a tap on an approval prompt's button '''

        if not self.allows(update):
            return

        query = update.callback_query
        kind, _, rest = (query.data or '').partition(':')
        key, _, outcome = rest.partition(':')

        settled = (kind == 'approve' and outcome in ('yes', 'no') and self.approvals is not None
                   and self.approvals.answer(key, update.effective_chat.id, outcome))

        try:
            if settled:
                await query.answer()
            else:
                # From before a restart, or already settled: drop the dead buttons.
                await query.answer(EXPIRED)
                await query.edit_message_reply_markup(None)
        except TelegramError as error:
            log.debug('could not acknowledge a button: %s', error)

    async def on_other(self, update, context):
        ''' photos, voice, stickers and the rest, until a capability can read them '''

        if self.allows(update):
            await send(context.bot, update.effective_chat.id, TEXT_ONLY)


async def tick(bot, agent, allowed, every=TICK):
    ''' send what the capabilities have to say unprompted, forever

    Each message is sent, then recorded in the chat's history so the next turn
    knows it was said. One that cannot be sent is logged and not recorded; the
    poller has already let it go, so it is not retried.
    '''

    while True:
        await asyncio.sleep(every)

        # Anything escaping here would end the task, and with it every reminder,
        # silently: an exception in a background task is never seen.
        try:
            await tick_once(bot, agent, allowed)
        except Exception:
            log.exception('reminder tick failed')


async def tick_once(bot, agent, allowed):
    for chat_id, text in await agent.registry.poll(datetime.now(timezone.utc)):
        if chat_id not in allowed:
            log.warning('not sending to chat %s: it is not allowed', chat_id)
            continue

        try:
            await send(bot, chat_id, text)
        except TelegramError:
            log.exception('could not send an unprompted message to chat %s', chat_id)
            continue

        await agent.inform(chat_id, text)


async def keep_typing(bot, chat_id, paused=None):
    ''' show "typing..." until cancelled, except while paused() (waiting on the operator)

    A failure here is cosmetic, so it is swallowed.
    '''

    while True:
        try:
            if paused is None or not paused():
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramError as error:
            log.debug('typing indicator failed: %s', error)

        await asyncio.sleep(TYPING_EVERY)


async def send(bot, chat_id, markdown):
    ''' send a reply as Telegram HTML, in as many messages as it takes

    Each piece falls back to plain text on its own: if Telegram rejects the HTML
    (a tag the converter got wrong) or it comes out over the limit, the words still
    arrive, just unformatted.
    '''

    for piece in split(markdown):
        rendered = to_html(piece)

        if len(rendered) <= MessageLimit.MAX_TEXT_LENGTH:
            try:
                await bot.send_message(chat_id, rendered, parse_mode=ParseMode.HTML)
                continue
            except BadRequest as error:
                log.warning('telegram rejected the html, sending plain text: %s', error)

        await bot.send_message(chat_id, piece[:MessageLimit.MAX_TEXT_LENGTH])


def run(cfg, start):
    ''' poll Telegram until stopped (Ctrl-C or SIGTERM)

    start() is awaited inside the polling loop and returns (agent, status, stop):
    the core, the /status reporter, and a stop() that closes what start() opened. Messages sent while Shelly was down are
    still answered when it comes back -- dropping them would lose what was said.

    Updates are handled concurrently: a turn waiting on an approval tap must not
    stop the tap itself from being handled. A chat's turns still run in the
    order their messages arrived -- each update's task starts in arrival order
    and the agent's per-chat lock is first come, first served.
    '''

    state = {}

    async def post_init(app):
        agent, status, state['stop'] = await start()
        approvals = Approvals(app.bot)
        agent.registry.approver = approvals.ask
        channel = Channel(agent, cfg.allowed_chat_ids, status, approvals)

        app.add_handler(CommandHandler('start', channel.on_start))
        app.add_handler(CommandHandler('reset', channel.on_reset))
        app.add_handler(CommandHandler('status', channel.on_status))
        app.add_handler(MessageHandler(filters.COMMAND, channel.on_unknown))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, channel.on_text))
        app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, channel.on_other))
        app.add_handler(CallbackQueryHandler(channel.on_button))
        app.add_error_handler(on_error)

        state['ticker'] = asyncio.create_task(tick(app.bot, agent, cfg.allowed_chat_ids))

        await app.bot.set_my_commands(COMMANDS)
        me = await app.bot.get_me()
        log.info('polling as @%s', me.username)

    async def post_shutdown(app):
        if 'ticker' in state:
            state['ticker'].cancel()

        if 'stop' in state:
            await state['stop']()

    app = (Application.builder()
           .token(cfg.telegram_token)
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .concurrent_updates(True)
           .build())

    # messages and button taps only: edits, reactions and the rest are not conversation
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


async def on_error(update, context):
    log.error('unhandled error in a telegram handler', exc_info=context.error)
