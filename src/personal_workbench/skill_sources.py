"""远程技能来源：受限 URL 与 GitHub 目录读取，不执行下载内容。"""
from __future__ import annotations

import io
import ipaddress
import json
import re
import socket
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

from personal_workbench.skill_package import UPLOAD_LIMIT


MAX_REDIRECTS = 5
MAX_REMOTE_FILES = 200
MAX_REMOTE_TOTAL = 30 * 1024 * 1024
ALLOWED_SUPPORT_DIRS = {"references", "templates", "scripts", "assets", "examples"}


def _assert_public_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("技能链接必须是公开的 HTTPS 地址。")
    if parsed.port not in {None, 443}:
        raise ValueError("技能链接只能使用标准 HTTPS 端口。")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    except OSError:
        raise ValueError("无法解析技能链接地址。") from None
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("技能链接不能指向本机或私有网络。")
    return value


def _fetch(url: str, *, limit: int = UPLOAD_LIMIT, accept: str = "text/plain, application/octet-stream") -> tuple[bytes, str]:
    current = url
    with httpx.Client(timeout=20, follow_redirects=False, headers={
        "User-Agent": "personal-workbench-skill-installer/1.0", "Accept": accept,
    }) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _assert_public_url(current)
            try:
                with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location: raise ValueError("技能链接重定向无效。")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise ValueError(f"读取技能失败（HTTP {response.status_code}）。")
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > limit: raise ValueError("远程技能文件超过大小限制。")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > limit: raise ValueError("远程技能文件超过大小限制。")
                    return bytes(data), str(response.url)
            except httpx.HTTPError as exc:
                raise ValueError("无法连接技能来源，请检查地址后重试。") from exc
    raise ValueError("技能链接重定向次数过多。")


def _zip_files(files: dict[str, bytes]) -> bytes:
    total = sum(map(len, files.values()))
    if len(files) > MAX_REMOTE_FILES or total > MAX_REMOTE_TOTAL:
        raise ValueError("远程技能最多包含 200 个文件、解压后 30 MiB。")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in sorted(files.items()): archive.writestr(path, data)
    return buffer.getvalue()


def _linked_support_paths(skill_md: bytes) -> list[str]:
    try: text = skill_md.decode("utf-8-sig")
    except UnicodeError: raise ValueError("远程 SKILL.md 必须是 UTF-8 文本。") from None
    found = set()
    pattern = r"(?<![\w./-])((?:references|templates|scripts|assets|examples)/[A-Za-z0-9._/-]+)"
    for match in re.finditer(pattern, text):
        path = unquote(match.group(1)).rstrip(".,;:)'\"`]")
        parts = PurePosixPath(path).parts
        if parts and parts[0] in ALLOWED_SUPPORT_DIRS and ".." not in parts:
            found.add(path)
    return sorted(found)


def fetch_url_skill(url: str) -> tuple[str, bytes, dict]:
    skill_md, final_url = _fetch(url, limit=1024 * 1024)
    if PurePosixPath(urlparse(final_url).path).name.lower() != "skill.md":
        raise ValueError("直接链接必须指向 SKILL.md。")
    files = {"SKILL.md": skill_md}
    base = final_url.rsplit("/", 1)[0] + "/"
    for path in _linked_support_paths(skill_md):
        files[path], _ = _fetch(urljoin(base, path), limit=1024 * 1024)
    source = {"kind":"url", "label":"直接链接", "identifier":final_url,
              "url":final_url, "trust_level":"community"}
    return "remote-skill.zip", _zip_files(files), source


def _parse_github(value: str) -> tuple[str, str, str, str]:
    raw = value.strip().rstrip("/")
    if raw.startswith("https://github.com/"):
        parts = urlparse(raw).path.strip("/").split("/")
    elif re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/.+)?", raw):
        parts = raw.split("/")
    else:
        raise ValueError("请输入 GitHub 仓库地址或 owner/repo/技能目录。")
    if len(parts) < 2: raise ValueError("GitHub 技能地址不完整。")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    ref, path = "", ""
    if len(parts) > 2 and parts[2] in {"tree", "blob"}:
        if len(parts) < 4: raise ValueError("GitHub 技能地址缺少分支。")
        ref, path = parts[3], "/".join(parts[4:])
    else:
        path = "/".join(parts[2:])
    if path.endswith("/SKILL.md") or path == "SKILL.md": path = str(PurePosixPath(path).parent)
    if path == ".": path = ""
    return owner, repo, ref, path


def _github_json(url: str):
    data, _ = _fetch(url, limit=2 * 1024 * 1024, accept="application/vnd.github+json")
    try: return json.loads(data)
    except json.JSONDecodeError: raise ValueError("GitHub 返回了无效数据。") from None


def fetch_github_skill(value: str) -> tuple[str, bytes, dict]:
    owner, repo, ref, root = _parse_github(value)
    queue = [root]
    files: dict[str, bytes] = {}
    source_revision = ""
    while queue:
        path = queue.pop(0)
        api = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/contents/{quote(path)}"
        if ref: api += "?ref=" + quote(ref)
        listing = _github_json(api)
        if isinstance(listing, dict): listing = [listing]
        if not isinstance(listing, list): raise ValueError("GitHub 技能目录无法读取。")
        for entry in listing:
            if not isinstance(entry, dict): continue
            kind, full_path = entry.get("type"), entry.get("path", "")
            if kind == "dir": queue.append(full_path); continue
            if kind != "file" or not entry.get("download_url"): continue
            try: rel = PurePosixPath(full_path).relative_to(PurePosixPath(root)) if root else PurePosixPath(full_path)
            except ValueError: raise ValueError("GitHub 技能目录包含异常路径。") from None
            if len(files) >= MAX_REMOTE_FILES: raise ValueError("远程技能最多包含 200 个文件。")
            content, _ = _fetch(entry["download_url"], limit=1024 * 1024)
            files[rel.as_posix()] = content
            source_revision = source_revision or str(entry.get("sha") or "")
            if sum(map(len, files.values())) > MAX_REMOTE_TOTAL: raise ValueError("远程技能解压后最多 30 MiB。")
    if "SKILL.md" not in files:
        raise ValueError("所选 GitHub 目录根部没有 SKILL.md。")
    identifier = f"{owner}/{repo}" + (f"/{root}" if root else "")
    tree_url = f"https://github.com/{owner}/{repo}" + (f"/tree/{ref}/{root}" if ref and root else f"/tree/{ref}" if ref else f"/{root}" if root else "")
    source = {"kind":"github", "label":"GitHub", "identifier":identifier,
              "url":tree_url, "trust_level":"community", "revision":source_revision,
              "github":{"owner":owner,"repo":repo,"ref":ref,"path":root}}
    return "github-skill.zip", _zip_files(files), source


def fetch_skill_source(kind: str, value: str) -> tuple[str, bytes, dict]:
    if kind == "github": return fetch_github_skill(value)
    if kind == "url": return fetch_url_skill(value)
    raise ValueError("不支持的技能来源。")


def refetch_skill_source(source: dict) -> tuple[str, bytes, dict]:
    kind = source.get("kind")
    if kind == "url": return fetch_url_skill(str(source.get("url") or source.get("identifier") or ""))
    if kind == "github":
        github = source.get("github") or {}
        owner,repo,ref,path=(str(github.get(key) or "") for key in ("owner","repo","ref","path"))
        if owner and repo:
            value=f"{owner}/{repo}" + (f"/tree/{ref}/{path}" if ref and path else f"/tree/{ref}" if ref else f"/{path}" if path else "")
        else:
            value=str(source.get("identifier") or source.get("url") or "")
        return fetch_github_skill(value)
    raise ValueError("此技能来源不支持自动更新，请手动导入新版本。")
