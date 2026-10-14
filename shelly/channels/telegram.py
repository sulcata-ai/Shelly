''' the Telegram channel: long polling, the allowlist, and getting replies onto a phone

The channel knows nothing about Claude. It hands the agent (chat_id, text) and
sends back whatever text it returns. The agent is built inside the running event
loop by a start() coroutine -- the async Mongo and Anthropic clients bind to the
loop they are first used in, and run_polling() owns that loop.
'''

import asyncio
import logging

from telegram import BotCommand, Update
from telegram.constants import ChatAction, MessageLimit, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from shelly.channels.telegram_format import split, to_html


log = logging.getLogger(__name__)

# Telegram shows "typing..." for about five seconds per action, so it is renewed
# a little more often than that while a turn runs.
TYPING_EVERY = 4

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


class Channel:
    ''' the handlers, bound to an agent and an allowlist '''

    def __init__(self, agent, allowed_chat_ids, status=None):
        self.agent = agent
        self.allowed = allowed_chat_ids
        self.status = status

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
        typing = asyncio.create_task(keep_typing(context.bot, chat_id))

        try:
            answer = await self.agent.reply(chat_id, update.message.text)
        except Exception:  # the store, most likely; the agent itself answers its own failures
            log.exception('turn failed in chat %s', chat_id)
            answer = BROKEN
        finally:
            typing.cancel()

        await send(context.bot, chat_id, answer)

    async def on_other(self, update, context):
        ''' photos, voice, stickers and the rest, until a capability can read them '''

        if self.allows(update):
            await send(context.bot, update.effective_chat.id, TEXT_ONLY)


async def keep_typing(bot, chat_id):
    ''' show "typing..." until cancelled; a failure here is cosmetic, so it is swallowed '''

    while True:
        try:
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
    Updates are handled one at a time, which keeps a chat's turns in order.
    '''

    state = {}

    async def post_init(app):
        agent, status, state['stop'] = await start()
        channel = Channel(agent, cfg.allowed_chat_ids, status)

        app.add_handler(CommandHandler('start', channel.on_start))
        app.add_handler(CommandHandler('reset', channel.on_reset))
        app.add_handler(CommandHandler('status', channel.on_status))
        app.add_handler(MessageHandler(filters.COMMAND, channel.on_unknown))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, channel.on_text))
        app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, channel.on_other))
        app.add_error_handler(on_error)

        await app.bot.set_my_commands(COMMANDS)
        me = await app.bot.get_me()
        log.info('polling as @%s', me.username)

    async def post_shutdown(app):
        if 'stop' in state:
            await state['stop']()

    app = (Application.builder()
           .token(cfg.telegram_token)
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())

    # message only: edits, reactions and the rest are not conversation
    app.run_polling(allowed_updates=[Update.MESSAGE])


async def on_error(update, context):
    log.error('unhandled error in a telegram handler', exc_info=context.error)
