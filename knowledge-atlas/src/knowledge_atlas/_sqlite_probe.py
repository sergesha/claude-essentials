"""Read physical SQLite files in a process that holds no SQLite POSIX locks."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import sys


@contextmanager
def open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('expected a regular file')
        yield stream


def main(args):
    path = Path(args[0])
    if len(args) == 1:
        with open_regular(path) as source:
            sys.stdout.buffer.write(source.read(64))
        return
    scratch = Path(args[1])
    for member in (path, path.with_name(path.name + '-journal')):
        with open_regular(member) as source, (scratch / member.name).open('xb') as target:
            shutil.copyfileobj(source, target)


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except FileNotFoundError:
        sys.exit(2)
    except (ValueError, OSError) as error:
        sys.exit(str(error))
