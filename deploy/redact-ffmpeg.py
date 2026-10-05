#!/usr/bin/env python3
"""Forward FFmpeg diagnostics without ever exposing the YouTube stream key."""

from __future__ import annotations

import os
import sys

stream_key = os.environ.get("YOUTUBE_STREAM_KEY", "")
try:
    for line in sys.stdin:
        if stream_key:
            line = line.replace(stream_key, "[REDACTED]")
        sys.stderr.write(line)
        sys.stderr.flush()
except KeyboardInterrupt:
    pass
