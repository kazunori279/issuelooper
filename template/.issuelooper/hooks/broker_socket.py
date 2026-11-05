"""Keep Unix socket addresses below macOS's 104-byte sockaddr_un limit."""
import hashlib
import os
import stat
from pathlib import Path


def socket_path(path):
    path = Path(path)
    path = path.parent.resolve() / path.name
    if len(os.fsencode(path)) < 104:
        return path
    # Resolve the parent so /tmp and /private/tmp name the same socket.
    key = hashlib.sha256(os.fsencode(path.parent.resolve() / path.name)).hexdigest()[:24]
    return Path('/tmp') / f'issuelooper-sockets-{os.getuid()}' / f'{key}.sock'


def prepare_socket(path):
    """The short directory is private to this user; keep the usual path discoverable."""
    path = Path(path)
    path = path.parent.resolve() / path.name
    target = socket_path(path)
    if target != path:
        target.parent.mkdir(mode=0o700, exist_ok=True)
        st = target.parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise PermissionError(f'unsafe socket directory: {target.parent}')
        path.unlink(missing_ok=True)
        path.symlink_to(target)
    target.unlink(missing_ok=True)
    return target
