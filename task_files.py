"""Readable output names and tightly scoped deletion of task-owned intermediates."""
import os
from pathlib import Path
import shutil
import stat


def available_path(parent, stem, suffix=''):
    parent = Path(parent)
    for number in range(1, 10001):
        label = stem if number == 1 else f'{stem} ({number})'
        candidate = parent / f'{label}{suffix}'
        try:
            candidate.lstat()
        except FileNotFoundError:
            return candidate
    raise FileExistsError('同名文件过多，请更换输出位置。')


def _is_reparse(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def checked_child(path, parent):
    """Resolve and check the exact absolute target BEFORE any delete operation."""
    path, parent = Path(path).absolute(), Path(parent).resolve()
    if _is_reparse(path):
        raise ValueError(f'不清理链接或联接点：{path}')
    resolved = path.resolve(strict=True)
    if resolved.parent != parent or resolved == parent or resolved == Path(resolved.anchor):
        raise ValueError(f'清理目标不在本次任务的专用目录内：{path}')
    return resolved


def remove_owned_tree(path, *, parent):
    path = Path(path)
    if not path.exists():
        return
    target = checked_child(path, parent)
    if not target.is_dir():
        raise ValueError('中间目录清理目标不是文件夹。')
    def onerror(error):
        raise error
    for directory, children, files in os.walk(target, followlinks=False, onerror=onerror):
        for name in children + files:
            if _is_reparse(Path(directory) / name):
                raise ValueError('中间目录出现链接，已停止清理。')
    shutil.rmtree(target)


def remove_owned_file(path, *, parent):
    path = Path(path)
    if not path.exists():
        return
    target = checked_child(path, parent)
    if not target.is_file():
        raise ValueError('中间文件清理目标不是文件。')
    target.unlink()
