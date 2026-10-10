"""Private atomic checkpoints, a process lock and immutable provenance."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

from src.pipeline.history_refresh import encoded, digest


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Private directory must not be a symlink')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic(path, data):
    path = Path(path)
    private_dir(path.parent)
    if path.is_symlink():
        raise ValueError('Checkpoint must not be a symlink')
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.checkpoint-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for part in iter(lambda: stream.read(1024*1024), b''):
            h.update(part)
    return h.hexdigest()


def tree_hash(root, patterns):
    root = Path(root)
    paths = sorted({p for pattern in patterns for p in root.glob(pattern) if p.is_file()})
    if not paths:
        raise ValueError('Fingerprint input is empty')
    return digest([(str(p.relative_to(root)), file_hash(p)) for p in paths])


class Store:
    def __init__(self, root, password):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if len(password) < 32:
            raise ValueError('DB_ENCRYPTION_KEY must contain at least 32 characters')
        self.root = private_dir(root)
        self.cipher = AESGCM(hashlib.sha256(('local-history-v1\0'+password).encode()).digest())

    def save_bytes(self, name, data):
        nonce = os.urandom(12)
        atomic(self.root/name, nonce+self.cipher.encrypt(nonce, data, name.encode()))

    def read_bytes(self, name):
        data = (self.root/name).read_bytes()
        return self.cipher.decrypt(data[:12], data[12:], name.encode())

    def save(self, name, value):
        self.save_bytes(name, encoded(value))

    def read(self, name):
        return json.loads(self.read_bytes(name))

    def exists(self, name):
        return (self.root/name).is_file()

    @contextmanager
    def lock(self):
        path = self.root/'run.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                os.write(fd, b'0'); os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RuntimeError('Another local history command is running') from None
        try:
            yield
        finally:
            os.close(fd)
