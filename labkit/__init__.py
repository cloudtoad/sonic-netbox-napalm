"""labkit — reusable lab infrastructure for Dell Enterprise SONiC VS testing."""

import time


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
