"""Read physical SQLite files in a process that holds no SQLite POSIX locks."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import sys


PROTOCOL = b'atlas-sqlite-probe/1\n'


class InputDisappeared(FileNotFoundError):
    pass


@contextmanager
def open_regular(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as error:
        raise InputDisappeared from error
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('expected a regular file')
        yield stream


def main(args):
    if args == ['ready']:
        return b''
    command, name, *rest = args
    path = Path(name)
    if command == 'header' and not rest:
        with open_regular(path) as source:
            opened = os.fstat(source.fileno())
            return f'{opened.st_dev}:{opened.st_ino}\n'.encode() + source.read(64)
    if command != 'copy' or len(rest) != 1:
        raise RuntimeError('invalid SQLite inspection command')
    scratch = Path(rest[0])
    for member in (path, path.with_name(path.name + '-journal')):
        with open_regular(member) as source, (scratch / member.name).open('xb') as target:
            shutil.copyfileobj(source, target)
    return b''


if __name__ == '__main__':
    try:
        payload = main(sys.argv[1:])
    except InputDisappeared:
        sys.stdout.buffer.write(PROTOCOL)
        sys.exit(os.EX_NOINPUT)
    except (ValueError, OSError) as error:
        print(error, file=sys.stderr)
        sys.stdout.buffer.write(PROTOCOL)
        sys.exit(os.EX_DATAERR)
    sys.stdout.buffer.write(PROTOCOL + payload)
