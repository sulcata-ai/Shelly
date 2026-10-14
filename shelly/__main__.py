''' python -m shelly: check the configuration, then run the Telegram channel '''

import logging
import sys

from shelly import config
from shelly.capabilities import memory
from shelly.channels import telegram
from shelly.core import agent as core
from shelly.core.prompt import Prompt, load_profile
from shelly.core.tools import Registry
from shelly.core.usage import COLLECTION as TURNS, Ledger
from shelly.memory import history
from shelly.memory.notes import Notes


log = logging.getLogger('shelly')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # httpx logs every Telegram long poll at INFO, which buries everything else.
    logging.getLogger('httpx').setLevel(logging.WARNING)

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

        # Capabilities register their tools and prompt context here.
        registry = Registry()
        memory.register(registry, notes)

        async def stop():
            await claude.close()
            await mongo.close()

        prompt = Prompt(load_profile(cfg.profile_path), cfg.timezone)

        return core.Agent(claude, store, registry, cfg.model, cfg.effort, prompt, ledger), stop

    telegram.run(cfg, start)

    return 0


if __name__ == '__main__':
    sys.exit(main())
