''' python -m shelly: check the configuration, then run the Telegram channel '''

import logging
import os
import sys
from pathlib import Path

import httpx

from shelly import config, logs
from shelly.capabilities import calendar, mail, mail_alerts, memory, todo
from shelly.channels import telegram
from shelly.core import agent as core
from shelly.core.prompt import Prompt, load_profile
from shelly.core.tools import Registry
from shelly.core.usage import COLLECTION as TURNS, Ledger
from shelly.integrations.google import CALENDAR_SCOPE, GMAIL_SCOPE, GoogleAuth, GoogleCalendar, Gmail
from shelly.memory import history
from shelly.memory.mail_watch import MailWatch
from shelly.memory.notes import Notes
from shelly.memory.tasks import Tasks
from shelly.status import Status


log = logging.getLogger('shelly')


def main():
    # The service sets SHELLY_LOG_FILE; by hand, logs go to the terminal.
    logs.configure(os.environ.get('SHELLY_LOG_FILE'))

    try:
        cfg = config.load()
    except config.ConfigError as error:
        log.error('cannot start: %s', error)
        return 2

    log.info('starting with %r', cfg)

    async def start():
        mongo, store = history.connect(cfg)
        await store.ensure_indexes()
        claude = core.client(cfg)

        db = mongo[cfg.mongodb_db]
        notes = Notes(db)
        await notes.ensure_indexes()
        ledger = Ledger(db[TURNS])
        await ledger.ensure_indexes()
        tasks = Tasks(db, cfg.timezone)
        await tasks.ensure_indexes()

        # Capabilities register their tools and prompt context here.
        registry = Registry()
        memory.register(registry, notes)
        todo.register(registry, tasks)

        google = None

        if Path(cfg.google_token_path).exists():
            google = httpx.AsyncClient(timeout=20)
            auth = GoogleAuth(cfg.google_token_path, google)

            # Each is on only if the saved login covers it: a login from before
            # a capability was added lacks its scope until the operator logs in again.
            if CALENDAR_SCOPE in auth.scopes:
                calendar.register(registry, GoogleCalendar(auth), cfg.timezone)

            if GMAIL_SCOPE in auth.scopes:
                gmail = Gmail(auth)
                mail.register(registry, gmail, cfg.timezone)
                # Alerts go to the operator's chat; with several allowed, which is unclear.
                alert_chat = next(iter(cfg.allowed_chat_ids)) if len(cfg.allowed_chat_ids) == 1 else None
                mail_alerts.register(registry, gmail, MailWatch(db), cfg.timezone, alert_chat)

            log.info('google: calendar %s, gmail %s',
                     *('on' if scope in auth.scopes else 'off (log in again)' for scope in (CALENDAR_SCOPE, GMAIL_SCOPE)))
        else:
            log.info('google off: no login saved at %s (python -m shelly.google_login)',
                     cfg.google_token_path)

        async def stop():
            await claude.close()
            await mongo.close()

            if google is not None:
                await google.aclose()

        prompt = Prompt(load_profile(cfg.profile_path))

        agent = core.Agent(claude, store, registry, cfg.model, cfg.effort, prompt, ledger)

        return agent, Status(agent, notes, ledger, cfg.timezone), stop

    telegram.run(cfg, start)

    return 0


if __name__ == '__main__':
    sys.exit(main())
