"""M2 资料库：原件、版本、原文片段和笔记保存在本地 SQLite。"""

import hashlib
import io
import math
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from personal_workbench.assistant_service import now
from personal_workbench.workspace import Workspace, read_bytes

UPLOAD_LIMIT = 50 * 1024 * 1024
PDF_PAGE_LIMIT = 500
TEXT_CHAR_LIMIT = 1_000_000
STOP = set("的了是在有和与及或为把被中下上我你他它这那个们吗呢么什如怎样请说明解释根据资料文档多少可以应该哪些")


def terms(text):
    result = set(re.findall(r"[a-z0-9_]{2,}", text.casefold()))
    for word in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(word[i:i + 2] for i in range(len(word) - 1) if not (set(word[i:i + 2]) & STOP))
    return result


def extract_pages(name: str, data: bytes):
    suffix = Path(name).suffix.lower()
    if suffix in {".md", ".txt"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("文本须为 UTF-8 编码，请转换后重新导入。") from None
        if "\x00" in text:
            raise ValueError("文件包含二进制内容，无法作为文本导入。")
        pages, warnings = [(None, text)], []
    elif suffix == ".pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise ValueError("请先解除 PDF 密码保护，再导入。")
            if len(reader.pages) > PDF_PAGE_LIMIT:
                raise ValueError("当前每份 PDF 最多 500 页，请先拆分。")
            pages, blank = [], []
            for index, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if not text.strip():
                    blank.append(index)
                else:
                    pages.append((index, text))
            warnings = ["以下页无可提取文本，未建立索引：" + ", ".join(map(str, blank))] if blank else []
        except ValueError:
            raise
        except Exception:
            raise ValueError("无法解析该 PDF，请确认文件完整且包含文本层。") from None
    else:
        raise ValueError("仅支持 .md、.txt 和含文本层的 .pdf。")
    if not any(text.strip() for _, text in pages):
        raise ValueError("没有可提取的文本。扫描 PDF 需要先做 OCR，当前版本不会识别图片文字。")
    if sum(len(text) for _, text in pages) > TEXT_CHAR_LIMIT:
        raise ValueError("提取文本超过 100 万字符，请拆分资料。")
    return pages, warnings


class Library:
    def __init__(self, settings):
        self.settings = settings
        settings.data_dir.mkdir(mode=0o700, exist_ok=True)
        self.path = settings.data_dir / "library.sqlite"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE, origin TEXT,
                    version TEXT, deleted INTEGER DEFAULT 0, updated TEXT
                );
                CREATE TABLE IF NOT EXISTS versions (
                    source_id TEXT, version TEXT, original BLOB, warnings TEXT,
                    PRIMARY KEY(source_id, version)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, source_id TEXT, version TEXT, page INTEGER,
                    paragraph INTEGER, start_line INTEGER, end_line INTEGER, text TEXT
                );
                CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source_id, version);
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY, title TEXT, content TEXT, thread_id TEXT, updated TEXT
                );
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, name: str, data: bytes, origin="upload"):
        if not name or len(name) > 160 or name.startswith(".") or any(c in name for c in "/\\") or any(ord(c) < 32 for c in name):
            raise ValueError("请使用不含目录或控制字符的正常文件名。")
        if not data or len(data) > UPLOAD_LIMIT:
            raise ValueError("文件不能为空，单个文件最多 50 MiB。")
        pages, warnings = extract_pages(name, data)
        version = hashlib.sha256(data).hexdigest()
        with self.db() as db:
            old = db.execute("SELECT * FROM documents WHERE name=?", (name,)).fetchone()
            if old and origin.startswith("local:") and (old["deleted"] or old["origin"] == "upload"):
                return {"id": old["id"], "skipped": True}
            if (not old or old["deleted"]) and db.execute("SELECT COUNT(*) FROM documents WHERE deleted=0").fetchone()[0] >= 100:
                raise ValueError("初版资料库最多 100 份活动资料，请先移出不需要的资料。")
            sid = old["id"] if old else uuid4().hex[:16]
            db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT(id) DO UPDATE SET version=excluded.version, origin=excluded.origin, deleted=0, updated=excluded.updated",
                       (sid, name, origin, version, now()))
            import json
            db.execute("INSERT OR IGNORE INTO versions VALUES (?, ?, ?, ?)", (sid, version, data, json.dumps(warnings, ensure_ascii=False)))
            for page, text in pages:
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                offset, paragraph = 0, 0
                for piece in re.split(r"\n\s*\n", text):
                    position = text.find(piece, offset)
                    offset = position + len(piece)
                    if not piece.strip():
                        continue
                    paragraph += 1
                    for part in range(0, len(piece), 1000):
                        body = piece[part:part + 1000]
                        start = text[:position + part].count("\n") + 1
                        end = start + body.rstrip("\n").count("\n")
                        cid = "c_" + hashlib.sha256(f"{sid}:{version}:{page}:{paragraph}:{part}".encode()).hexdigest()[:24]
                        db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET end_line=excluded.end_line",
                                   (cid, sid, version, page, paragraph, start, end, body))
        return {"id": sid, "version": version, "warnings": warnings, "updated": bool(old)}

    def sync_local(self):
        files = Workspace(self.settings.notes_dir, self.settings.outputs_dir).list_files()
        synced, skipped = 0, []
        for path in files["files"]:
            try:
                # 目录内重名文件使用相对路径的可读替代名，实际来源保存在 origin。
                self.ingest(path.replace("/", " · "), read_bytes(self.settings.notes_dir, path), origin="local:" + path)
                synced += 1
            except (ValueError, OSError):
                skipped.append(path)
        # 本地删除的资料移出检索，但原版本仍可供历史引用打开。
        if not files["truncated"]:
            current = {"local:" + path for path in files["files"]}
            with self.db() as db:
                for row in db.execute("SELECT id, origin FROM documents WHERE origin LIKE 'local:%'").fetchall():
                    if row["origin"] not in current:
                        db.execute("UPDATE documents SET deleted=1 WHERE id=?", (row["id"],))
        return {"synced": synced, "skipped": skipped, "truncated": files["truncated"]}

    def documents(self):
        import json
        with self.db() as db:
            return [{**dict(row), "warnings": json.loads(row["warnings"])} for row in db.execute("""
                SELECT d.*, v.warnings, (SELECT COUNT(*) FROM chunks c WHERE c.source_id=d.id AND c.version=d.version) AS chunks
                FROM documents d JOIN versions v ON v.source_id=d.id AND v.version=d.version
                WHERE deleted=0 ORDER BY updated DESC, name
            """)]

    def chunks(self, sid):
        with self.db() as db:
            return [dict(row) for row in db.execute("""
                SELECT c.*, d.name AS title FROM chunks c JOIN documents d ON d.id=c.source_id
                WHERE d.id=? AND d.deleted=0 AND c.version=d.version ORDER BY c.page, c.paragraph, c.start_line, c.rowid
            """, (sid,))]

    def source(self, cid):
        with self.db() as db:
            row = db.execute("SELECT c.*, d.name AS title, d.deleted, d.version AS current_version FROM chunks c JOIN documents d ON d.id=c.source_id WHERE c.id=?", (cid,)).fetchone()
            if not row:
                raise ValueError("找不到这条原文引用。")
            return {**dict(row), "archived": bool(row["deleted"] or row["version"] != row["current_version"])}

    def remove(self, sid):
        with self.db() as db:
            if not db.execute("UPDATE documents SET deleted=1, updated=? WHERE id=?", (now(), sid)).rowcount:
                raise ValueError("资料不存在。")

    def search(self, query, limit=6):
        if not query.strip() or len(query) > 1000:
            raise ValueError("检索内容须为 1–1000 字符。")
        tokens = terms(query)
        with self.db() as db:
            rows = [dict(row) for row in db.execute("""
                SELECT c.*, d.name AS title FROM chunks c JOIN documents d ON d.id=c.source_id
                WHERE d.deleted=0 AND c.version=d.version
            """)]
        indexed = [(row, terms(row["text"] + " " + row["title"])) for row in rows]
        frequencies = {t: sum(t in found for _, found in indexed) for t in tokens}
        hits = []
        for row, found in indexed:
            overlap = tokens & found
            exact = query.casefold() in row["text"].casefold()
            if not overlap and not exact:
                continue
            score = sum(math.log(1 + len(rows) / (1 + frequencies[t])) for t in overlap)
            score += 4 * exact + sum(1 for t in overlap if t in row["title"].casefold())
            hits.append({**row, "score": round(score, 3)})
        return sorted(hits, key=lambda row: (-row["score"], row["id"]))[:limit]

    def save_user_note(self, title, content, thread_id=None, note_id=None):
        if not title.strip() or len(title) > 120 or not content.strip() or len(content) > 32000:
            raise ValueError("笔记标题须为 1–120 字符，内容须为 1–32000 字符。")
        nid = note_id or uuid4().hex
        with self.db() as db:
            if note_id and not db.execute("SELECT id FROM notes WHERE id=?", (note_id,)).fetchone():
                raise ValueError("笔记不存在。")
            db.execute("INSERT INTO notes VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, content=excluded.content, updated=excluded.updated",
                       (nid, title, content, thread_id, now()))
        return {"id": nid}

    def notes(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM notes ORDER BY updated DESC")]
