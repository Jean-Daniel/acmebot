import abc
import contextlib
import io
import logging
import os
import shlex
import subprocess
import tempfile
from collections import OrderedDict
from typing import AnyStr
from typing import Optional, Iterable

import collections

from .logging import log

# ========= File System
FileOwner = collections.namedtuple('FileOwner', ('uid', 'gid', 'is_self'))


def dirmode(mode: int) -> int:
    if mode & 0o700:
        mode |= 0o100
    if mode & 0o070:
        mode |= 0o010
    if mode & 0o007:
        mode |= 0o001
    return mode


class Operation(metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def apply(self, archive_dir: Optional[str]):
        pass

    @abc.abstractmethod
    def revert(self):
        pass

    def cleanup(self):
        pass


class WriteOperation(Operation):
    __slots__ = ['file_path', 'mode', 'owner', '_content', '_tmp_path', '_rmdir']

    def __init__(self, file_path: str, mode: int, owner: Optional[FileOwner] = None):
        self.file_path = file_path
        self.mode = mode
        self.owner = owner if owner and not owner.is_self else None

        self._tmp_path = None
        self._content = None  # type: AnyStr
        self._rmdir = False

    @contextlib.contextmanager
    def file(self, binary=True):
        stream = io.BytesIO() if binary else io.StringIO()
        yield stream
        self._content = stream.getvalue()

    @property
    def is_write(self) -> bool:
        return bool(self._content)

    def tmp_path(self, archive_dir: Optional[str]):
        return tempfile.mktemp(prefix='.old-', dir=os.path.dirname(self.file_path))

    def apply(self, archive_dir: Optional[str]):
        tmp_path = self.tmp_path(archive_dir)
        try:
            os.makedirs(os.path.dirname(tmp_path), dirmode(self.mode or 0o700))
            self._rmdir = True
        except FileExistsError:
            pass

        # Move existing file out of the way
        try:
            os.rename(self.file_path, tmp_path)
            self._tmp_path = tmp_path
        except FileNotFoundError:
            if self._rmdir:
                os.removedirs(os.path.dirname(tmp_path))

        if not self._content:
            return

        mode = 'wb' if isinstance(self._content, bytes) else 'w'
        with open(self.file_path, mode) as f:
            if self.mode:
                try:
                    os.fchmod(f.fileno(), self.mode)
                except PermissionError as error:
                    logging.warning('Unable to set file mode for "%s" to %s: %s', self.file_path, oct(self.mode), str(error))
            if self.owner:
                try:
                    os.fchown(f.fileno(), self.owner.uid, self.owner.gid)
                except PermissionError as error:
                    logging.warning('Unable to set file ownership for "%s" to %s:%s: %s', self.file_path, self.owner.uid, self.owner.gid, str(error))
            f.write(self._content)
        log.debug("'%s' saved", self.file_path)
        self._content = None

    def revert(self):
        try:
            os.remove(self.file_path)
            log.debug('%s removed', self.file_path)
        except FileNotFoundError:
            pass

        if self._tmp_path:
            os.rename(self._tmp_path, self.file_path)
            log.debug('%s restored', self.file_path)
            self._tmp_path = None

    def cleanup(self):
        if self._tmp_path:
            try:
                os.remove(self._tmp_path)
            except FileNotFoundError:
                pass

            if self._rmdir:
                try:
                    os.removedirs(os.path.dirname(self._tmp_path))
                except (FileNotFoundError, OSError):
                    pass

            self._tmp_path = None


class ArchiveAndWriteOperation(WriteOperation):
    __slots__ = ['file_type', '_archived']

    def __init__(self, file_type: str, file_path: str, mode: int, owner: Optional[FileOwner] = None):
        super().__init__(file_path, mode, owner)
        self.file_type = file_type
        self._archived = False

    def tmp_path(self, archive_dir: Optional[str]):
        if archive_dir:
            self._archived = True
            return os.path.join(archive_dir, self.file_type, os.path.basename(self.file_path))
        # return a temporary path used to be able to revert in case of error
        return super().tmp_path(archive_dir)

    def apply(self, archive_dir: Optional[str]):
        super().apply(archive_dir)
        if self._tmp_path and self._archived:
            log.debug("'%s' archived", self.file_path)

    def cleanup(self):
        # skip cleanup step if file archived (should not be removed)
        if self._archived:
            return
        return super().cleanup()


class ArchiveOperation(ArchiveAndWriteOperation):

    def __init__(self, file_type: str, file_path: str):
        super().__init__(file_type, file_path, 0, None)

    def file(self, binary: bool = False):
        raise NotImplementedError("archive operation does not support writing. Use ArchiveAndWriteOperation instead.")


def commit_file_transactions(operations: Iterable[Operation], archive_dir: Optional[str] = None):
    if not operations:
        return

    log.debug('Committing file transaction')
    applied = []
    try:
        with log.prefix("  "):
            for op in operations:
                op.apply(archive_dir)
                applied.append(op)
            # log.debug("%s: %s", file_transaction.message or 'file saved', file_transaction.file_path)
    except Exception as e:  # restore any archived files
        log.error('File transaction error. Rolling back changes')
        with log.prefix("  "):
            for op in applied:
                try:
                    op.revert()
                except Exception as err:
                    log.error("reverting operation '%s' failed: %s", str(op), str(err))
        raise e
    else:
        for op in applied:
            try:
                op.cleanup()
            except Exception as err:
                log.error("cleanup operation '%s' failed: %s", str(op), str(err))


class Hooks(object):

    def __init__(self, commands: dict):
        self._hooks = OrderedDict()
        self._commands = commands

    # Hook Management
    def add(self, hook_name: str, **kwargs):
        hooks = self._commands[hook_name]
        if not hooks:
            return

        if hook_name not in self._hooks:
            self._hooks[hook_name] = []

        # Hook take an array of commands, or a single command
        if isinstance(hooks, (str, dict)):
            hooks = (hooks,)
        try:
            for hook in hooks:
                if isinstance(hook, str):
                    hook = {
                        'args': shlex.split(hook)
                    }
                else:
                    hook = hook.copy()
                hook['args'] = [arg.format(**kwargs) for arg in hook['args']]
                self._hooks[hook_name].append(hook)
        except KeyError as e:
            log.warning('Invalid hook specification for %s, unknown key %s', hook_name, e)

    def call(self):
        for hook_name, hooks in self._hooks.items():
            for hook in hooks:
                try:
                    log.info('Calling hook %s: %s', hook_name, hook['args'])
                    # TODO: add support for cwd, env, …
                    log.info(subprocess.check_output(hook['args'], stderr=subprocess.STDOUT, shell=False))
                except subprocess.CalledProcessError as error:
                    log.warning('Hook %s returned error, code: %s:\n%s', hook_name, error.returncode, error.output)
                except Exception as e:
                    log.warning('Failed to call hook %s (%s): %s', hook_name, hook['args'], str(e))
        self._clear_hooks()

    def _clear_hooks(self):
        self._hooks.clear()