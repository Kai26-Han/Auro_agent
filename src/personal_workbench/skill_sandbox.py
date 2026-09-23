"""受限 Skill Python 脚本执行器。

脚本使用独立临时目录、隔离 Python、资源上限和操作系统沙箱。当前只接受
无第三方依赖的锁文件；没有可用的系统沙箱时拒绝执行，而不是降级裸跑。
"""
import json
import os
import platform
import resource
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import tool


MAX_INPUT = 32_000
MAX_OUTPUT = 24_000
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


@lru_cache(maxsize=1)
def sandbox_backend():
    if platform.system() == 'Darwin' and Path('/usr/bin/sandbox-exec').is_file():
        try:
            probe=subprocess.run(['/usr/bin/sandbox-exec','-p','(version 1)(allow default)','/usr/bin/true'],
                                 capture_output=True,timeout=3)
            if probe.returncode==0:return 'macos-sandbox-exec'
        except (OSError,subprocess.SubprocessError):pass
    if shutil.which('bwrap'):
        return 'linux-bwrap'
    return None


def _limits(timeout):
    def apply():
        address_space = 2 * 1024 * 1024 * 1024 if platform.system() == 'Darwin' else 768 * 1024 * 1024
        for kind, soft, hard in (
            (resource.RLIMIT_CPU, timeout, timeout + 1),
            # macOS maps the Python framework and shared cache into a large
            # virtual address range before user code starts.  384 MiB caused
            # SIGABRT even for an empty script; 2 GiB remains bounded while
            # the Seatbelt profile still controls actual filesystem/network IO.
            (resource.RLIMIT_AS, address_space, address_space),
            (resource.RLIMIT_FSIZE, 8 * 1024 * 1024, 8 * 1024 * 1024),
            (resource.RLIMIT_NOFILE, 64, 64),
            (resource.RLIMIT_NPROC, 16, 16),
        ):
            try:
                _, current_hard=resource.getrlimit(kind)
                cap=hard if current_hard==resource.RLIM_INFINITY else min(hard,current_hard)
                resource.setrlimit(kind,(min(soft,cap),cap))
            except (OSError,ValueError):
                # The OS sandbox and subprocess timeout remain mandatory; a
                # kernel that lacks one supplemental rlimit must not make the
                # child setup itself crash.
                pass
    return apply


def _mac_profile(python, skill_root, work, notes, script):
    def literal(value):
        return str(value).replace('\\', '\\\\').replace('"', '\\"')
    reads = [skill_root, work]
    if script.get('read_notes'):
        reads.append(notes.resolve())
    # Python on current macOS releases consults system paths outside the
    # framework itself during dyld/locale startup and aborts if those reads are
    # individually omitted.  Permit host reads, then carve out user and mounted
    # data and reopen only this immutable skill plus its ephemeral work folder.
    # This keeps system runtime discovery functional without exposing user data.
    rules = ['(version 1)', '(deny default)', '(allow process*)',
             '(allow sysctl-read)', '(allow mach-lookup)', '(allow signal (target self))',
             '(allow file-read*)']
    for protected in (Path.home().resolve(), Path('/Volumes'), Path('/Network'), Path('/home')):
        rules.append('(deny file-read* (subpath "'+literal(protected)+'"))')
    rules.extend('(allow file-read* (subpath "'+literal(path)+'"))' for path in reads if path.exists())
    rules.append('(allow file-write* (subpath "'+literal(work)+'"))')
    rules.append('(allow network-outbound)' if script.get('network') == 'full' else '(deny network*)')
    return ''.join(rules)


def _command(backend, executable, profile, skill_root, work, notes, script_path, script):
    runtime_args = ([executable, '-I', '-S', str(script_path)] if script.get('runtime','python') == 'python'
                    else [executable, '--disallow-code-generation-from-strings', str(script_path)])
    if backend == 'macos-sandbox-exec':
        return ['/usr/bin/sandbox-exec', '-p', profile, *runtime_args]
    args = ['bwrap', '--die-with-parent', '--unshare-all', '--new-session', '--ro-bind', '/', '/',
            '--bind', str(work), str(work), '--chdir', str(work)]
    if script.get('network') == 'full':
        args.remove('--unshare-all'); args += ['--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts']
    if not script.get('read_notes'):
        args += ['--tmpfs', str(notes.resolve())]
    return args + runtime_args


def execute(settings, store, refs, skill_id, script_id, input_value, run_id=''):
    ref = next((value for value in refs if value['id'] == skill_id), None)
    if not ref:
        raise ValueError('脚本不属于本轮已冻结的技能。')
    meta = store.revision(ref['id'], ref['revision'])
    script = next((value for value in meta.get('scripts', []) if value['id'] == script_id), None)
    if not script or not ref.get('scripts_enabled'):
        raise ValueError('此脚本未声明或尚未获得执行授权。')
    encoded = json.dumps(input_value, ensure_ascii=False)
    if len(encoded.encode()) > MAX_INPUT:
        raise ValueError('脚本输入超过 32 KiB。')
    backend = sandbox_backend()
    if not backend:
        raise ValueError('当前系统没有受支持的脚本沙箱，已拒绝执行。')
    root = store.root / ref['id'] / ref['revision']
    script_path = root / script['path']
    # revision() has verified every file; repeat the script bytes immediately before execution.
    file = next(value for value in meta['files'] if value['path'] == script['path'])
    store.read_bytes(ref['id'], ref['revision'], file)
    runtime=script.get('runtime','python')
    candidate=(os.environ.get('WORKBENCH_SANDBOX_PYTHON') or '/usr/bin/python3') if runtime=='python' else shutil.which('node')
    executable=str(Path(candidate).resolve()) if candidate else ''
    if not executable or not Path(executable).is_file():
        raise ValueError(f'脚本沙箱找不到 {runtime} 运行时。')
    with tempfile.TemporaryDirectory(prefix='workbench-skill-') as folder:
        work = Path(folder).resolve()
        profile = _mac_profile(executable, root.resolve(), work, settings.notes_dir, script) if backend == 'macos-sandbox-exec' else ''
        command = _command(backend, executable, profile, root.resolve(), work, settings.notes_dir, script_path.resolve(), script)
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
               'WORKBENCH_SKILL_ID': ref['id'], 'WORKBENCH_SCRIPT_ID': script_id,
               'WORKBENCH_OUTPUT_DIR': str(work)}
        try:
            completed = subprocess.run(command, input=encoded, text=True, cwd=work, env=env,
                                       capture_output=True, timeout=script['timeout'] + 2,
                                       preexec_fn=_limits(script['timeout']))
        except subprocess.TimeoutExpired:
            return {'ok': False, 'error': '脚本执行超时。'}
        output = completed.stdout[:MAX_OUTPUT]
        error = completed.stderr[:4000]
        generated = []
        artifacts = []
        safe_run_id = ''.join(ch for ch in str(run_id or 'manual') if ch.isalnum() or ch in '-_')[:64] or 'manual'
        artifact_root = (settings.project_dir / 'outputs' / safe_run_id / 'skills' /
                         ref['id'] / script_id).resolve()
        for path in sorted(work.rglob('*')):
            if not path.is_file() or path.is_symlink():
                continue
            relative = str(path.relative_to(work))
            size = path.stat().st_size
            if size > MAX_ARTIFACT_BYTES:
                generated.append({'path': relative, 'omitted': '文件超过 8 MiB'})
                continue
            destination = (artifact_root / relative).resolve()
            if artifact_root not in destination.parents:
                generated.append({'path': relative, 'omitted': '输出路径无效'})
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            saved = str(destination)
            artifacts.append({'path': saved, 'name': destination.name, 'bytes': size})
            try:
                content = path.read_text('utf-8')
                generated.append({'path': relative, 'saved': saved, 'content': content[:12000], 'truncated': len(content) > 12000})
            except UnicodeError:
                generated.append({'path': relative, 'saved': saved, 'binary': True})
        result = {'ok': completed.returncode == 0, 'exit_code': completed.returncode,
                  'stdout': output, 'stderr': error, 'files': generated, 'artifacts': artifacts,
                  'sandbox': backend, 'runtime': runtime, 'network': script['network'], 'notes_read': script['read_notes']}
        try:
            parsed = json.loads(output)
            result['result'] = parsed
        except (ValueError, TypeError):
            pass
        return result


def build_skill_script_tool(settings, store, refs, run_id=''):
    @tool
    def run_skill_script(skill_id: str, script_id: str, input: dict) -> dict:
        """运行当前技能声明并已授权的 Python/Node 固定入口。输入必须是 JSON 对象；生成文件会保存为本轮产物。"""
        try:
            return execute(settings, store, refs, skill_id, script_id, input, run_id)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
    return run_skill_script


def script_tool_catalog():
    value = build_skill_script_tool(None, None, [])
    return {'id': value.name, 'name': '运行技能脚本', 'description': '在独立沙箱中运行本轮已授权技能的 Python 脚本。',
            'applicability': '仅脚本技能可用；依赖锁定，文件和网络范围按包权限执行。',
            'source': 'builtin', 'availability': 'contextual', 'schema': value.args_schema.model_json_schema()}
