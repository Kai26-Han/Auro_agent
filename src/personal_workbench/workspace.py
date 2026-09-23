"""文件访问边界与可恢复写入。资料只读，成果写入当前会话的独立目录。"""

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

MAX_FILE_BYTES = 128 * 1024
MAX_NOTE_BYTES = 32 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_FILES = 200
SUFFIXES = {".md", ".txt"}
ARTIFACT_SUFFIXES = {".md", ".txt", ".html", ".css", ".js", ".json"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def path_parts(path: str, suffixes=SUFFIXES) -> tuple[str, ...]:
    parts = Path(path).parts
    if not parts or Path(path).is_absolute() or any(
        p in {".", ".."} or p.startswith(".") or any(ord(c) < 32 for c in p)
        for p in parts
    ):
        raise ValueError("只能使用资料目录内、不含隐藏目录或上级跳转的相对路径。")
    if Path(path).suffix.lower() not in suffixes:
        raise ValueError("不支持此文件类型。")
    return parts


@contextmanager
def directory_fd(root: Path):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def read_bytes(root: Path, path: str, suffixes=SUFFIXES, max_bytes=MAX_FILE_BYTES) -> bytes:
    """逐层用目录描述符打开，不跟随符号链接，也不打开设备、管道或硬链接。"""
    parts = path_parts(path, suffixes)
    with directory_fd(root) as root_fd:
        current = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = next_fd
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            with os.fdopen(fd, "rb") as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("只允许读取普通文件，不支持链接、设备或管道。")
                data = file.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError("文件超过允许大小。")
                return data
        finally:
            os.close(current)


class Workspace:
    def __init__(self, notes: Path, outputs: Path):
        self.notes = notes
        self.outputs = outputs

    def list_files(self) -> dict:
        files = []
        # os.walk 默认不跟随目录链接；再过滤隐藏目录及链接文件。
        visited = 0
        for root, dirs, names in os.walk(self.notes, followlinks=False):
            visited += 1
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(root) / d).is_symlink())
            for name in sorted(names):
                path = Path(root) / name
                if name.startswith(".") or path.suffix.lower() not in SUFFIXES or path.is_symlink():
                    continue
                if path.is_file() and path.stat().st_nlink == 1:
                    files.append(path.relative_to(self.notes).as_posix())
                if len(files) >= MAX_FILES:
                    return {"files": files, "truncated": True}
            if visited >= 500:
                return {"files": files, "truncated": True}
        return {"files": files, "truncated": False}

    def read_file(self, path: str, start_line: int = 1, end_line: int = 80) -> dict:
        if start_line < 1 or end_line < start_line or end_line - start_line >= 120:
            raise ValueError("行号从 1 开始，每次最多读取 120 行。")
        data = read_bytes(self.notes, path)
        lines = data.decode("utf-8-sig").splitlines()
        if not lines:
            return {"path": path, "lines": [], "sources": [], "total_lines": 0}
        if start_line > len(lines):
            raise ValueError("起始行超出了文件总行数。")
        end_line = min(end_line, len(lines))
        selected = lines[start_line - 1:end_line]
        if sum(len(line) for line in selected) > 12000:
            raise ValueError("所选文本过长，请缩小读取行数。")
        source = {"path": path, "start": start_line, "end": end_line, "sha256": digest(data)}
        return {"path": path, "lines": [f"L{i}: {line}" for i, line in enumerate(selected, start_line)],
                "total_lines": len(lines), "sources": [source]}

    def search_files(self, query: str) -> dict:
        if not query.strip() or len(query) > 100:
            raise ValueError("请使用 1 到 100 字符的非空检索词；使用字面匹配。")
        inventory = self.list_files()
        hits, sources, skipped = [], [], []
        for path in inventory["files"]:
            try:
                data = read_bytes(self.notes, path)
                lines = data.decode("utf-8-sig").splitlines()
            except (OSError, ValueError):
                skipped.append(path)
                continue
            for i, line in enumerate(lines, 1):
                if query.casefold() in line.casefold():
                    hits.append({"path": path, "line": i, "text": line[:500], "line_truncated": len(line) > 500})
                    sources.append({"path": path, "start": i, "end": i, "sha256": digest(data)})
                    if len(hits) >= 20:
                        return {"hits": hits, "sources": sources, "truncated": True, "skipped": skipped}
        return {"hits": hits, "sources": sources, "truncated": inventory["truncated"], "skipped": skipped}

    def prepare_note(self, filename: str, content: str) -> dict:
        if len(path_parts(filename)) != 1 or len(filename.encode("utf-8")) > 180:
            raise ValueError("成果名称必须是一个 .md/.txt 文件名，不能包含目录。")
        if not content.strip() or len(content.encode("utf-8")) > MAX_NOTE_BYTES:
            raise ValueError("笔记内容不能为空，且不能超过 32 KiB。")
        try:
            old = read_bytes(self.outputs, filename)
        except FileNotFoundError:
            old = None
        return {"filename": filename, "content": content, "old_sha": digest(old) if old is not None else None,
                "old_content": old.decode("utf-8") if old is not None else None,
                "new_sha": digest(content.encode("utf-8"))}

    def prepare_artifact(self, filename: str, content: str) -> dict:
        if len(path_parts(filename, ARTIFACT_SUFFIXES)) != 1 or len(filename.encode("utf-8")) > 180:
            raise ValueError("成果名称必须是单个文件名，不能包含目录。")
        encoded = content.encode("utf-8")
        if not content.strip() or len(encoded) > MAX_ARTIFACT_BYTES:
            raise ValueError("成果内容不能为空，且不能超过 1 MiB。")
        try:
            old = read_bytes(self.outputs, filename, ARTIFACT_SUFFIXES, MAX_ARTIFACT_BYTES)
        except FileNotFoundError:
            old = None
        return {"filename": filename, "content": content, "old_sha": digest(old) if old is not None else None,
                "old_content": old.decode("utf-8") if old is not None else None,
                "new_sha": digest(encoded), "artifact": True}

    def list_outputs(self) -> list[str]:
        if not self.outputs.exists():
            return []
        files = []
        for path in sorted(self.outputs.iterdir(), key=lambda value: value.name.casefold()):
            if (not path.name.startswith('.') and path.suffix.lower() in ARTIFACT_SUFFIXES
                    and path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1):
                files.append(path.name)
            if len(files) >= MAX_FILES:
                break
        return files

    def save_note(self, proposal: dict, action_id: str, db: sqlite3.Connection) -> dict:
        """先记写入意图，再原子替换，再记完成；崩溃后按同一动作 ID 恢复。"""
        filename, content = proposal["filename"], proposal["content"]
        # 重新验证路径及大小，检查点中的提案也不能跳过执行边界。
        current = self.prepare_artifact(filename, content) if proposal.get("artifact") else self.prepare_note(filename, content)
        payload = json.dumps(proposal, ensure_ascii=False, sort_keys=True)
        row = db.execute("SELECT payload, status FROM writes WHERE action_id=?", (action_id,)).fetchone()
        if row:
            if row[0] != payload:
                raise ValueError("同一写入动作的内容发生变化，已停止。")
            if current["old_sha"] == proposal["new_sha"]:
                db.execute("UPDATE writes SET status='done' WHERE action_id=?", (action_id,))
                db.commit()
                return {"saved": filename, "replayed": True}
            if row[1] == "done":
                raise ValueError("已保存的成果被外部修改，不会重复写入覆盖它。")
        if current["old_sha"] != proposal["old_sha"]:
            raise ValueError("文件在预览后发生变化，请重新发起任务并查看新的预览。")
        db.execute("INSERT OR IGNORE INTO writes VALUES (?, ?, 'pending')", (action_id, payload))
        db.commit()
        # 临时文件与目标在同一目录；恢复时不会增加新的成果名称。
        with directory_fd(self.outputs) as out_fd:
            temp_name = f".writing-{uuid4().hex}"
            fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=out_fd)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(content.encode("utf-8"))
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp_name, filename, src_dir_fd=out_fd, dst_dir_fd=out_fd)
                os.fsync(out_fd)
            finally:
                try:
                    os.unlink(temp_name, dir_fd=out_fd)
                except FileNotFoundError:
                    pass
        db.execute("UPDATE writes SET status='done' WHERE action_id=?", (action_id,))
        db.commit()
        return {"saved": filename, "replayed": False}
