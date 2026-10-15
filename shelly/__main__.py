''' python -m shelly: check the configuration, then run the Telegram channel '''

import logging
import os
import sys
from pathlib import Path

import httpx

from shelly import config, logs
from shelly.capabilities import calendar, memory, todo
from shelly.channels import telegram
from shelly.core import agent as core
from shelly.core.prompt import Prompt, load_profile
from shelly.core.tools import Registry
from shelly.core.usage import COLLECTION as TURNS, Ledger
from shelly.integrations.google import GoogleAuth, GoogleCalendar
from shelly.memory import history
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
            calendar.register(registry, GoogleCalendar(GoogleAuth(cfg.google_token_path, google)), cfg.timezone)
            log.info('google calendar on')
        else:
            log.info('google calendar off: no login saved at %s (python -m shelly.google_login)',
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
