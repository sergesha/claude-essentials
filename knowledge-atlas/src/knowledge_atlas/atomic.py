"""Publish complete owner-private files by a same-directory rename."""
import os
from pathlib import Path
import tempfile


def write_private(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
