"""Local, content-addressed image attachments for chat messages.

Only opaque attachment IDs are persisted in LangGraph. Image bytes are read
and converted to provider-compatible data URLs immediately before a model call.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import HumanMessage
from PIL import Image, ImageOps, UnidentifiedImageError


MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_TURN_BYTES = 24 * 1024 * 1024
MAX_IMAGES_PER_TURN = 4
MAX_IMAGE_EDGE = 4096
MAX_IMAGE_PIXELS = 40_000_000
STAGED_TTL_HOURS = 24
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatAttachmentStore:
    def __init__(self, settings):
        self.root = settings.data_dir / "chat-attachments"
        self.blobs = self.root / "blobs"
        self.database = self.root / "attachments.sqlite"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.blobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attachments(
                id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, filename TEXT NOT NULL,
                mime_type TEXT NOT NULL, bytes INTEGER NOT NULL, width INTEGER NOT NULL,
                height INTEGER NOT NULL, extension TEXT NOT NULL, status TEXT NOT NULL,
                thread_id TEXT, run_id TEXT, created TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS attachment_thread ON attachments(thread_id)")
        self.cleanup_staged()

    def connect(self):
        db = sqlite3.connect(self.database, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _safe_name(name: str | None) -> str:
        value = Path(name or "image").name
        value = re.sub(r"[^\w .()\-\u4e00-\u9fff]", "_", value).strip(" .")
        return (value or "image")[:160]

    @staticmethod
    def _normalise(data: bytes) -> tuple[bytes, str, str, int, int]:
        if not data:
            raise ValueError("图片内容为空。")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("单张图片不能超过 12 MiB。")
        try:
            with Image.open(io.BytesIO(data)) as opened:
                if (opened.width * opened.height) > MAX_IMAGE_PIXELS:
                    raise ValueError("图片像素过大，请缩小后重试。")
                image = ImageOps.exif_transpose(opened)
                if getattr(image, "is_animated", False):
                    image.seek(0)
                image.load()
                image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
                has_alpha = image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                )
                output = io.BytesIO()
                if opened.format == "JPEG" and not has_alpha:
                    image.convert("RGB").save(output, "JPEG", quality=90, optimize=True)
                    mime, extension = "image/jpeg", ".jpg"
                else:
                    image.convert("RGBA" if has_alpha else "RGB").save(output, "PNG", optimize=True)
                    mime, extension = "image/png", ".png"
                clean = output.getvalue()
                if len(clean) > MAX_IMAGE_BYTES:
                    flattened = Image.new("RGB", image.size, "white")
                    if has_alpha:
                        rgba = image.convert("RGBA")
                        flattened.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        flattened.paste(image.convert("RGB"))
                    output = io.BytesIO()
                    flattened.save(output, "JPEG", quality=86, optimize=True)
                    clean, mime, extension = output.getvalue(), "image/jpeg", ".jpg"
                if len(clean) > MAX_IMAGE_BYTES:
                    raise ValueError("图片处理后仍超过 12 MiB，请缩小后重试。")
                return clean, mime, extension, image.width, image.height
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ValueError("无法识别这张图片，请使用 JPEG、PNG、WebP 或 GIF。") from None

    def save(self, filename: str | None, data: bytes, declared_type: str | None = None) -> dict:
        if declared_type and declared_type not in ALLOWED_MIME_TYPES:
            raise ValueError("仅支持 JPEG、PNG、WebP 或 GIF 图片。")
        clean, mime, extension, width, height = self._normalise(data)
        digest = hashlib.sha256(clean).hexdigest()
        path = self.blobs / f"{digest}{extension}"
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=self.blobs, prefix=".upload-")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as file:
                    file.write(clean)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        attachment_id = uuid4().hex
        with self.connect() as db:
            db.execute("""INSERT INTO attachments
                (id,sha256,filename,mime_type,bytes,width,height,extension,status,created)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (attachment_id, digest, self._safe_name(filename), mime, len(clean),
                 width, height, extension, "staged", _now()))
        return self.public(attachment_id)

    def row(self, attachment_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", attachment_id or ""):
            raise ValueError("图片附件 ID 无效。")
        with self.connect() as db:
            row = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if row is None:
            raise ValueError("图片附件不存在或已被清理。")
        return dict(row)

    def public(self, attachment_id: str) -> dict:
        row = self.row(attachment_id)
        return {"id": row["id"], "name": row["filename"], "mime_type": row["mime_type"],
                "bytes": row["bytes"], "width": row["width"], "height": row["height"],
                "url": f"/api/chat-attachments/{row['id']}"}

    def path(self, attachment_id: str) -> Path:
        row = self.row(attachment_id)
        path = self.blobs / f"{row['sha256']}{row['extension']}"
        if not path.is_file() or path.is_symlink():
            raise ValueError("图片附件文件不存在。")
        return path

    def validate(self, attachment_ids: list[str] | None) -> list[dict]:
        ids = list(dict.fromkeys(attachment_ids or []))
        if len(ids) > MAX_IMAGES_PER_TURN:
            raise ValueError("每次最多添加 4 张图片。")
        rows = [self.row(item) for item in ids]
        if sum(item["bytes"] for item in rows) > MAX_TURN_BYTES:
            raise ValueError("本次图片总大小不能超过 24 MiB。")
        return rows

    def bind(self, attachment_ids: list[str] | None, thread_id: str, run_id: str) -> list[dict]:
        rows = self.validate(attachment_ids)
        with self.connect() as db:
            for row in rows:
                if row["status"] == "bound" and row["thread_id"] != thread_id:
                    raise ValueError("图片附件已经属于其他会话。")
                db.execute("UPDATE attachments SET status='bound',thread_id=?,run_id=? WHERE id=?",
                           (thread_id, run_id, row["id"]))
        return [self.public(row["id"]) for row in rows]

    def clone(self, attachment_ids: list[str] | None) -> list[str]:
        """Create staged references to existing immutable blobs for a new thread."""
        rows = self.validate(attachment_ids)
        created = []
        with self.connect() as db:
            for row in rows:
                attachment_id = uuid4().hex
                db.execute("""INSERT INTO attachments
                    (id,sha256,filename,mime_type,bytes,width,height,extension,status,created)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (attachment_id, row["sha256"], row["filename"], row["mime_type"], row["bytes"],
                     row["width"], row["height"], row["extension"], "staged", _now()))
                created.append(attachment_id)
        return created

    def delete_staged(self, attachment_id: str):
        row = self.row(attachment_id)
        if row["status"] != "staged":
            raise ValueError("已发送的图片请随对话统一管理。")
        self._delete_rows([row])

    def delete_thread(self, thread_id: str):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM attachments WHERE thread_id=?", (thread_id,)).fetchall()]
        self._delete_rows(rows)

    def cleanup_staged(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=STAGED_TTL_HOURS)).isoformat(timespec="seconds")
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM attachments WHERE status='staged' AND created<?", (cutoff,)).fetchall()]
        self._delete_rows(rows)

    def _delete_rows(self, rows: list[dict]):
        if not rows:
            return
        ids = [row["id"] for row in rows]
        with self.connect() as db:
            db.executemany("DELETE FROM attachments WHERE id=?", [(item,) for item in ids])
            for row in rows:
                referenced = db.execute("SELECT 1 FROM attachments WHERE sha256=?", (row["sha256"],)).fetchone()
                if not referenced:
                    path = self.blobs / f"{row['sha256']}{row['extension']}"
                    if path.is_file() and not path.is_symlink():
                        path.unlink()

    def materialize_messages(self, messages):
        """Return model-only message copies with local images embedded as data URLs."""
        # Keep the total model payload bounded across conversation history.
        # Prefer the newest visual turns; older text remains available.
        selected: dict[int, list[dict]] = {}
        seen: set[str] = set()
        total = 0
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            refs = (getattr(message, "additional_kwargs", None) or {}).get("attachments", [])
            if message.type != "human" or not refs:
                continue
            rows = self.validate([item.get("id") for item in refs
                                  if isinstance(item, dict) and item.get("id")])
            for row in rows:
                if row["id"] in seen or len(seen) >= MAX_IMAGES_PER_TURN:
                    continue
                if total + row["bytes"] > MAX_TURN_BYTES:
                    continue
                selected.setdefault(index, []).append(row)
                seen.add(row["id"])
                total += row["bytes"]
        result = []
        for index, message in enumerate(messages):
            rows = selected.get(index, [])
            if not rows:
                result.append(message)
                continue
            blocks = [{"type": "text", "text": message.text}]
            for row in rows:
                payload = base64.b64encode(self.path(row["id"]).read_bytes()).decode("ascii")
                blocks.append({"type": "image_url", "image_url": {
                    "url": f"data:{row['mime_type']};base64,{payload}", "detail": "auto"
                }})
            result.append(HumanMessage(content=blocks, id=message.id,
                                       additional_kwargs=message.additional_kwargs,
                                       response_metadata=message.response_metadata))
        return result
