"""
atomic_io.py
============
Replacing a small JSON store file on Windows. os.replace fails with
PermissionError while another thread or process (the observer re-reads
project and routine files every few seconds, an antivirus scanner, the
search indexer) has the target open for a moment - so the write is retried
briefly instead of failing the user's save.
"""
from __future__ import annotations

import os
import time


def replace(src: str, dst: str, tries: int = 20, delay: float = 0.05):
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(delay)
