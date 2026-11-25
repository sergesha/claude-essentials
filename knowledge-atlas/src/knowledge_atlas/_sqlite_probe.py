"""Read physical SQLite files in a process that holds no SQLite POSIX locks."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import sys


PROTOCOL = b'atlas-sqlite-probe/2\n'


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


def fingerprint(stream):
    info = os.fstat(stream.fileno())
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def main(args):
    command, name, *rest = args
    path = Path(name)
    if command == 'header' and not rest:
        with open_regular(path) as source:
            before = fingerprint(source)
            header = source.read(64)
            if fingerprint(source) != before:
                return b'changed'
            return ':'.join(map(str, before)).encode() + b'\n' + header
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
