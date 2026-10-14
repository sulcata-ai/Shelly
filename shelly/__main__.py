''' python -m shelly: check the configuration, then run '''

import logging
import sys

from shelly import config


log = logging.getLogger('shelly')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    try:
        cfg = config.load()
    except config.ConfigError as error:
        log.error('cannot start: %s', error)
        return 2

    log.info('starting with %r', cfg)

    # Nothing to run yet: the Telegram channel is the next item on the roadmap.
    log.info('no channels are wired up yet; exiting')

    return 0


if __name__ == '__main__':
    sys.exit(main())
