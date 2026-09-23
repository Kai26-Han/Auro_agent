"""Consistent, credential-free LangMem store export and isolated restore.

Includes this instance's memory data (all its spaces), not chats or settings.
Never restores into an active workbench or changes engine activation.
"""
import hashlib
import io
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime,timezone
from pathlib import Path


def export_store(store):
    with tempfile.TemporaryDirectory(prefix='langmem-export-') as folder:
        path=Path(folder)/'store.sqlite'
        with store.connect() as src, sqlite3.connect(path) as dst:src.backup(dst)
        # No application settings database / API keys are copied. These settings
        # contain only engine flags and model profile IDs, not provider secrets.
        data=path.read_bytes()
    manifest={'format':'workbench-langmem-p5','created':datetime.now(timezone.utc).isoformat(),
              'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),
              'scope':'LangMem instance, all spaces','excludes':['raw chats','checkpoint history','application model settings and keys','output artifacts','other engines','older backups']}
    result=io.BytesIO()
    with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('store.sqlite',data);archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    return result.getvalue()


def restore_isolated(archive_path,destination):
    destination=Path(destination)
    if destination.exists():raise ValueError('恢复目录必须是尚不存在的新目录，不覆盖正式数据。')
    with zipfile.ZipFile(archive_path) as archive:
        if sorted(archive.namelist())!=['manifest.json','store.sqlite']:raise ValueError('备份文件清单无效。')
        manifest=json.loads(archive.read('manifest.json'));data=archive.read('store.sqlite')
    if manifest.get('format')!='workbench-langmem-p5' or hashlib.sha256(data).hexdigest()!=manifest.get('sha256') or len(data)!=manifest.get('bytes'):raise ValueError('备份校验失败。')
    with tempfile.TemporaryDirectory(prefix='langmem-verify-') as folder:
        source=Path(folder)/'store.sqlite';source.write_bytes(data)
        with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('备份数据库不完整。')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_tombstones'").fetchone():raise ValueError('备份版本不匹配。')
            if db.execute("SELECT 1 FROM spaces WHERE engine!='langmem'").fetchone():raise ValueError('备份含其他引擎数据。')
        destination.mkdir(mode=0o700,parents=True,exist_ok=False)
        output=destination/'store.sqlite';output.write_bytes(data);output.chmod(0o600)
        (destination/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    return {'verified':True,'path':str(output),'activated':False}
