"""Delete exact trusted storage paths, without following directory symlinks."""
import os
from pathlib import Path
import shutil
import stat


def remove_storage_path(root, relative, *, keep_directory=False):
    parts = Path(relative).parts
    if (not parts or Path(relative).is_absolute() or any(p in {'.', '..'} for p in parts)
            or '\\' in str(relative)):
        raise ValueError('Unsafe cleanup path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*Path(root).absolute().parts[1:], *parts[:-1]):
            if part in {'.', '..'}:
                raise ValueError('Unsafe cleanup root')
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return
            os.close(fd)
            fd = child
        name = parts[-1]
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if keep_directory:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                for entry in os.listdir(child):
                    item = os.stat(entry, dir_fd=child, follow_symlinks=False)
                    if stat.S_ISDIR(item.st_mode):
                        shutil.rmtree(entry, dir_fd=child)
                    else:
                        os.unlink(entry, dir_fd=child)
                os.fsync(child)
            finally:
                os.close(child)
        elif stat.S_ISDIR(info.st_mode):
            shutil.rmtree(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)
