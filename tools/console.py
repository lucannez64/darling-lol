"""Status icons shared by the tools that report update progress.

`scripts/update.sh` paints its own `✔`/`✖` only when stdout is a terminal and
`NO_COLOR` is unset, and leaves them plain in logs and in tests. These helpers
apply the same rule so a stamp line matches the step lines printed around it.
"""
import os
import sys

GREEN = '\033[32m'
YELLOW = '\033[33m'
RED = '\033[31m'
RESET = '\033[0m'

OK = '✔'
NOTE = '!'
FAIL = '✖'


def supports_color(stream):
    if os.environ.get('NO_COLOR'):
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def status(icon, color, message, stream):
    if supports_color(stream):
        stream.write('%s%s%s %s\n' % (color, icon, RESET, message))
    else:
        stream.write('%s %s\n' % (icon, message))


def ok(message, stream=None):
    """Report a step that was skipped because nothing it consumes changed."""
    status(OK, GREEN, message, sys.stdout if stream is None else stream)


def note(message, stream=None):
    """Report why the next step runs, such as a stamp that no longer matches."""
    status(NOTE, YELLOW, message, sys.stdout if stream is None else stream)


def fail(message, stream=None):
    """Report a step whose stamp could not be read or recorded."""
    status(FAIL, RED, message, sys.stderr if stream is None else stream)
