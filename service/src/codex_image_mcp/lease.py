"""Non-blocking process ownership of a service state directory."""

from pathlib import Path
import errno
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class StateLease:
    def __init__(self, state: Path):
        state.mkdir(parents=True, exist_ok=True)
        self._file = None
        handle = (state / ".service.lock").open("a+b")
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            handle.close()
            if isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise RuntimeError(
                    "STATE_DIRECTORY_IN_USE: another service owns this state directory"
                ) from exc
            raise
        self._file = handle

    def close(self) -> None:
        # Closing releases the OS lock, including after process death. Never unlink the
        # file: a competing process could otherwise lock a different inode at this path.
        if self._file is not None:
            self._file.close()
            self._file = None
