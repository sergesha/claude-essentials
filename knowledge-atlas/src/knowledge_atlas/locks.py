"""OS locks survive service restarts and never infer ownership from a PID."""
import fcntl
import hashlib
from pathlib import Path
from .contracts import AtlasError


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.stream.close(); self.stream = None
            raise AtlasError('busy', 'another worker holds the writer lock', 409) from error
        return self

    def __exit__(self, *_):
        if self.stream is not None:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close(); self.stream = None


class AtlasLock(FileLock):
    def __init__(self, atlas_path: Path):
        canonical = Path(atlas_path).expanduser().resolve()
        digest = hashlib.sha256(str(canonical).encode()).hexdigest()
        super().__init__(canonical.parent / ('.knowledge-atlas-' + digest + '.lock'))


def is_locked(path: Path) -> bool:
    try:
        with FileLock(path):
            return False
    except AtlasError as error:
        if error.code != 'busy': raise
        return True
