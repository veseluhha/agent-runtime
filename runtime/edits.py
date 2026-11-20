"""Stage shell edits on the client and publish regular files without clobbering another writer."""
import ctypes
import errno
import fcntl
import hashlib
import os
import json
import shutil
import stat
import tempfile
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

RESERVED = {".git", ".agent-edits"}
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class Version:
    digest: str
    mode: int
    identity: tuple

    @property
    def contents(self):
        return self.digest, self.mode


def file_version(parent, name, copy=None):
    """Read from the opened inode. A changing source cannot establish a base version."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        os.close(fd)
        raise ValueError(f"expected a regular file: {name}")
    with os.fdopen(fd, "rb") as source:
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(256 * 1024), b""):
            digest.update(block)
            if copy is not None:
                copy.write(block)
        after = os.fstat(fd)
        if (before.st_ctime_ns, before.st_size) != (after.st_ctime_ns, after.st_size):
            raise BlockingIOError(f"file changed while reading: {name}")
        return Version(digest.hexdigest(), stat.S_IMODE(after.st_mode), (after.st_dev, after.st_ino))


@contextmanager
def open_directory(root, relative=Path()):
    fd = os.open(root, DIRECTORY)
    try:
        for part in relative.parts:
            child = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def snapshot(root, destination=None):
    files, directories = {}, {}

    def fail(error):
        raise error

    for path, children, names, fd in os.fwalk(root, onerror=fail):
        relative = Path(path).relative_to(root)
        for name in children[:]:
            if name.casefold() in RESERVED:
                children.remove(name)
            elif not stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                if destination is None:
                    raise ValueError(f"unsupported staged link: {relative / name}")
                children.remove(name)
        children.sort()
        if relative.parts:
            st = os.fstat(fd)
            directories[relative] = (st.st_dev, st.st_ino)
            if destination:
                (destination / relative).mkdir()
        for name in sorted(names):
            if name.casefold() in RESERVED:
                continue
            if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                if destination is None:
                    raise ValueError(f"unsupported staged entry: {relative / name}")
                continue
            key = relative / name
            if destination:
                with (destination / key).open("xb") as out:
                    version = file_version(fd, name, out)
                    if version is None:
                        raise BlockingIOError(f"file disappeared while copying: {key}")
                    os.fchmod(out.fileno(), version.mode)
            else:
                version = file_version(fd, name)
                if version is None:
                    raise BlockingIOError(f"file disappeared while scanning: {key}")
            files[key] = version
    return files, directories


def matches(parent, name, expected):
    try:
        return file_version(parent, name) == expected
    except (IsADirectoryError, ValueError, PermissionError, BlockingIOError):
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return False
        raise


def rename_at(source, parent, name, *, exchange=False):
    # Darwin: RENAME_SWAP / RENAME_EXCL. Linux: RENAME_EXCHANGE / RENAME_NOREPLACE.
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        result = libc.renameatx_np(-2, os.fsencode(source), parent, os.fsencode(name), 2 if exchange else 4)
    elif sys.platform.startswith("linux"):
        result = libc.renameat2(-100, os.fsencode(source), parent, os.fsencode(name), 2 if exchange else 1)
    else:
        raise OSError("exclusive rename requires macOS or Linux")
    if result == 0:
        return True
    error = ctypes.get_errno()
    if not exchange and error in (errno.EEXIST, errno.ENOTEMPTY):
        return False
    raise OSError(error, os.strerror(error), str(source))


def copy_attributes(parent, name, destination):
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(source).st_mode):
            return
        with destination.open("rb") as target:
            if sys.platform == "darwin":
                libc = ctypes.CDLL(None, use_errno=True)
                if libc.fcopyfile(source, target.fileno(), None, 1 | 4):  # COPYFILE_ACL | COPYFILE_XATTR
                    code = ctypes.get_errno()
                    raise OSError(code, os.strerror(code), name)
            else:
                for key in os.listxattr(source):
                    os.setxattr(target.fileno(), key, os.getxattr(source, key))
    finally:
        os.close(source)


def same_directory(parent, name, identity):
    try:
        fd = os.open(name, DIRECTORY, dir_fd=parent)
    except (FileNotFoundError, NotADirectoryError):
        return False
    try:
        info = os.fstat(fd)
        return (info.st_dev, info.st_ino) == identity and not os.listdir(fd)
    finally:
        os.close(fd)


class WorkingCopy:
    def __init__(self, root, tree):
        self.root = root
        self.tree = tree
        self.archive = root / ".agent-edits"
        self.last_view = None
        self.externally_changed = set()

    def refresh(self):
        with tempfile.TemporaryDirectory(prefix="refresh-", dir=self.tree.parent) as fresh:
            generation = Path(fresh)
            base, directories = snapshot(self.root, generation)
            with open_directory(self.tree.parent) as parent:
                rename_at(generation, parent, self.tree.name, exchange=True)
            view = {p: v.contents for p, v in base.items()}
            if self.last_view is not None:
                self.externally_changed.update(p for p in view.keys() | self.last_view.keys()
                                               if view.get(p) != self.last_view.get(p))
            self.last_view = view
            self.base, self.directories = base, directories

    @contextmanager
    def locked(self):
        self.archive.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(self.archive, DIRECTORY)
        try:
            # Only publishers take this lock. Editors on the Mac need no cooperation.
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def recover(self):
        conflicts = []
        for record in sorted(self.archive.glob("edit-*")):
            if record.is_symlink() or not record.is_dir():
                continue
            claimed, swapped = record / "claimed", record / "exchange"
            if not os.path.lexists(claimed) and not os.path.lexists(swapped):
                continue
            path = Path(json.loads((record / "path.json").read_text()))
            if path.is_absolute() or not path.parts or ".." in path.parts or any(p.casefold() in RESERVED for p in path.parts):
                raise ValueError("invalid path in interrupted edit")
            try:
                with open_directory(self.root, path.parent) as parent:
                    if os.path.lexists(swapped):
                        journal = record / "versions.json"
                        if not journal.exists():  # publication has not started
                            continue
                        versions = json.loads(journal.read_text())
                        base, installed = (Version(v["digest"], v["mode"], tuple(v["identity"])) for v in versions)
                        st = swapped.lstat()
                        if (st.st_dev, st.st_ino) != installed.identity:
                            if not self.finish_exchange(record, parent, path, base, installed):
                                conflicts.append((path, record / "agent" / path.name))
                    elif (record / "versions.json").exists():
                        claimed.rename(record / "during-restore")
                    elif not rename_at(claimed, parent, path.name):
                        claimed.rename(record / "previous")
                        conflicts.append((path, record / "delete"))
            except (FileNotFoundError, NotADirectoryError):
                conflicts.append((path, record))
        return conflicts

    @staticmethod
    def finish_exchange(record, parent, path, base, installed):
        swapped = record / "exchange"
        held = record / "claimed"
        with open_directory(record) as fd:
            if not os.path.lexists(held) and matches(fd, "exchange", base):
                swapped.rename(record / "previous")
                return True
            # Only reclaim the agent's own install. A subsequent editor save has priority.
            if not os.path.lexists(held) and matches(parent, path.name, installed):
                try:
                    os.rename(path.name, held, src_dir_fd=parent)
                except FileNotFoundError:
                    pass
            if os.path.lexists(held):
                if not matches(fd, "claimed", installed):
                    rename_at(held, parent, path.name)
                rename_at(swapped, parent, path.name)
                if os.path.lexists(held):
                    held.rename(record / "during-restore")
            if os.path.lexists(swapped):
                swapped.rename(record / "previous")
            return False

    def publish(self):
        final, directories = snapshot(self.tree)
        changed = [path for path in sorted(self.base.keys() | final.keys())
                   if path not in self.base or path not in final or
                   self.base[path].contents != final[path].contents]
        conflicts = []
        type_changes = (self.base.keys() & directories.keys()) | (final.keys() & self.directories.keys())
        blocked = set()
        with self.locked():
            conflicts.extend(self.recover())
            for path in sorted(type_changes):
                record = self.record(path)
                proposal = record / "agent" / path.name
                proposal.parent.mkdir()
                if path in directories:
                    shutil.copytree(self.tree / path, proposal, symlinks=True)
                else:
                    shutil.copy2(self.tree / path, proposal)
                conflicts.append((path, proposal))
                blocked.add(path)
            for path in sorted(directories.keys() - self.directories.keys()):
                if path in blocked or blocked.intersection(path.parents):
                    continue
                try:
                    with open_directory(self.root, path.parent) as parent:
                        os.mkdir(path.name, dir_fd=parent)
                except FileExistsError:
                    with open_directory(self.root, path.parent) as parent:
                        if stat.S_ISDIR(os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode):
                            continue
                    blocked.add(path)
                except (FileNotFoundError, NotADirectoryError):
                    blocked.add(path)
                if path in blocked:
                    proposal = self.record(path) / "agent" / path.name
                    proposal.parent.mkdir()
                    shutil.copytree(self.tree / path, proposal, symlinks=True)
                    conflicts.append((path, proposal))
            for path in changed:
                if path in blocked or blocked.intersection(path.parents):
                    continue
                conflict = self.publish_one(path, self.base.get(path), final.get(path))
                if conflict:
                    conflicts.append((path, conflict))
            for path in sorted(self.directories.keys() - directories.keys(), reverse=True):
                if path in blocked or blocked.intersection(path.parents):
                    continue
                conflict = self.publish_one(path, self.directories[path], None, directory=True)
                if conflict:
                    conflicts.append((path, conflict))
        self.last_view = {p: v.contents for p, v in final.items()}
        return conflicts

    def record(self, path):
        record = Path(tempfile.mkdtemp(dir=self.archive, prefix="edit-"))
        (record / "path.json").write_text(json.dumps(str(path)) + "\n")
        return record

    def publish_one(self, path, base, new, directory=False):
        record = self.record(path)
        proposal = record / "agent" / path.name if new else record / "delete"
        if new:
            proposal.parent.mkdir()
            with open_directory(self.tree, path.parent) as fd, proposal.open("xb") as out:
                if file_version(fd, path.name, out) != new:
                    raise BlockingIOError(f"staged file changed: {path}")
                os.fchmod(out.fileno(), new.mode)
                out.flush()
                os.fsync(out.fileno())
        else:
            proposal.touch(exist_ok=False)
        if path in self.externally_changed:
            return proposal
        check = same_directory if directory else matches
        try:
            with open_directory(self.root, path.parent) as parent:
                if not check(parent, path.name, base):
                    return proposal
                if base is None:
                    if rename_at(proposal, parent, path.name):
                        shutil.rmtree(record)
                        return None
                    return proposal
                if new:
                    candidate = record / "exchange"
                    shutil.copy2(proposal, candidate)
                    try:
                        copy_attributes(parent, path.name, candidate)
                    except OSError as exc:
                        if exc.errno in (errno.ELOOP, errno.ENOENT):
                            return proposal
                        raise
                    with open_directory(record) as fd:
                        installed = file_version(fd, "exchange")
                    journal = record / "versions.tmp"
                    journal.write_text(json.dumps([asdict(base), asdict(installed)]) + "\n")
                    journal.rename(record / "versions.json")
                    rename_at(candidate, parent, path.name, exchange=True)
                    return None if self.finish_exchange(record, parent, path, base, installed) else proposal
                claimed = record / "claimed"
                # This moves the actual occupant, even if the editor replaced it since the check.
                os.rename(path.name, claimed, src_dir_fd=parent)
                try:
                    with open_directory(record) as captured:
                        if not check(captured, "claimed", base):
                            return proposal
                    claimed.rename(record / "previous")
                    return None
                finally:
                    if os.path.lexists(claimed) and not rename_at(claimed, parent, path.name):
                        claimed.rename(record / "previous")
        except (FileNotFoundError, NotADirectoryError):
            return proposal
