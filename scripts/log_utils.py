#!/usr/bin/env python3
"""
APEX - Log Utilities
====================
Wraps stdout so every print() line gets a timestamp prefix.
"""

import sys
from datetime import datetime


class TimestampedWriter:
    def __init__(self, stream):
        self.stream = stream
        self._at_line_start = True

    def write(self, msg):
        if not msg:
            return
        if self._at_line_start and msg != "\n":
            ts = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
            self.stream.write(ts)
        self.stream.write(msg)
        self._at_line_start = msg.endswith("\n")

    def flush(self):
        self.stream.flush()

    def fileno(self):
        return self.stream.fileno()

    def isatty(self):
        return False


def setup_logging():
    """Wrap sys.stdout so all print() calls include a timestamp."""
    if not isinstance(sys.stdout, TimestampedWriter):
        sys.stdout = TimestampedWriter(sys.stdout)
