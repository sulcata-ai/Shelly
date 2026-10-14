''' python -m shelly: check the configuration, then run the Telegram channel '''

import logging
import sys

from shelly import config
from shelly.channels import telegram
from shelly.core import agent as core
from shelly.core.tools import Registry
from shelly.memory import history


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

        # Capabilities register their tools here as they are built.
        registry = Registry()

        async def stop():
            await claude.close()
            await mongo.close()

        return core.Agent(claude, store, registry, cfg.model, cfg.effort), stop

    telegram.run(cfg, start)

    return 0


if __name__ == '__main__':
    sys.exit(main())
