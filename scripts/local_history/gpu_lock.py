"""One loaded MLX model per machine, across every local recognition process.

A pipelined queue may start the next lecture's acquisition while the previous
lecture is still recognizing; the second process then waits here before it
loads the model. The kernel releases the lock if a holder dies, so a crash can
never leave the GPU claimed.
"""
import contextlib
import fcntl
import os
import tempfile
import time
from pathlib import Path


def lock_path():
    return Path(os.environ.get('ICOURSE_GPU_LOCK') or Path(tempfile.gettempdir())/'icoursenotes-mlx-gpu.lock')


@contextlib.contextmanager
def exclusive_gpu(on_wait=None, *, poll=2.0, path=None):
    """Hold the machine-wide GPU slot; `on_wait` runs once if another process has it."""
    target = Path(path) if path else lock_path()
    with open(target, 'a') as handle:
        waited = False
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waited and on_wait:
                    on_wait()
                waited = True
                # Polling keeps the wait interruptible by the app's pause (SIGINT).
                time.sleep(poll)
        try:
            yield waited
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
