"""OS-released exclusive installation lock; SQLite is still used for claims."""
import os
from geo_workbench import store

_file = None


def acquire():
    global _file
    if _file is not None:
        raise RuntimeError('同一安装目录已有服务实例；只支持单进程')
    handle = (store.ROOT / 'service.lock').open('a+b')
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if not handle.read(1):
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('该数据目录正由另一进程使用；请使用单进程启动') from None
    _file = handle


def release():
    global _file
    if _file is not None:
        _file.close()
        _file = None
