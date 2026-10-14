''' where log lines go: stderr when run by hand, a rotating file under systemd

Under systemd, Python owns the file and rotates it itself. Rotation by anyone
else (logrotate, say) would rename the file out from under systemd's open
append handle, and systemd would carry on writing into the renamed copy. The
service's own stdout/stderr go to a separate file that only catches what escapes
logging -- a crash before logging is set up, a hard interpreter error -- and stays
small.
'''

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'

# ~30 MB on disk at most. A busy day is a few hundred lines, so five old files
# reach back months.
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5


def configure(path=None, max_bytes=MAX_BYTES, backups=BACKUPS):
    ''' send logging to path (rotated) if given, else to stderr; returns the handler '''

    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
    else:
        handler = logging.StreamHandler()

    handler.setFormatter(logging.Formatter(FORMAT))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)

    # httpx logs every Telegram long poll at INFO, which buries everything else.
    logging.getLogger('httpx').setLevel(logging.WARNING)

    # An uncaught exception would otherwise print to stderr only, outside the log.
    def log_uncaught(kind, value, traceback):
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, traceback)
            return
        logging.getLogger('shelly').critical('uncaught exception', exc_info=(kind, value, traceback))

    sys.excepthook = log_uncaught

    return handler
