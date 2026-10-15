''' python -m shelly: check the configuration, then run the Telegram channel '''

import logging
import os
import sys

from shelly import config, logs
from shelly.capabilities import memory, todo
from shelly.channels import telegram
from shelly.core import agent as core
from shelly.core.prompt import Prompt, load_profile
from shelly.core.tools import Registry
from shelly.core.usage import COLLECTION as TURNS, Ledger
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

        async def stop():
            await claude.close()
            await mongo.close()

        prompt = Prompt(load_profile(cfg.profile_path))

        agent = core.Agent(claude, store, registry, cfg.model, cfg.effort, prompt, ledger)

        return agent, Status(agent, notes, ledger, cfg.timezone), stop

    telegram.run(cfg, start)

    return 0


if __name__ == '__main__':
    sys.exit(main())
