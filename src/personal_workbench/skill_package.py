"""只解析技能包，不运行其中的程序。目录与大小校验在落盘前完成。"""
import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath

import yaml
from yaml.tokens import AliasToken, AnchorToken
from personal_workbench.file_tools import TOOL_INFO

UPLOAD_LIMIT = 10 * 1024 * 1024
SKILL_FILE_LIMIT = 256 * 1024
SKILL_BODY_LIMIT = 48000
TEXT_LIMIT = 512 * 1024
TEXT_SUFFIXES = {'.md', '.txt', '.json', '.yaml', '.yml', '.csv', '.lock', '.css', '.js', '.html'}
SCRIPT_LIMIT = 256 * 1024

# Agent Skills packages commonly use provider-specific tool names.  Keep the
# original declaration for display, but resolve well-known capabilities to the
# tools that provide the same boundary in Workbench.  The terminal tool still
# applies Workbench's command policy and approval checks.
TOOL_ALIASES = {
    'read': ('terminal',),
    'grep': ('terminal',),
    'glob': ('terminal',),
    'shell': ('terminal',),
    'bash': ('terminal',),
    'edit': ('terminal',),
    'write': ('terminal',),
    'websearch': ('web_search',),
    'webfetch': ('web_fetch',),
}
OPTIONAL_EXTERNAL_TOOLS = {'task'}


def parse_allowed_tools(value):
    if value is None:
        return None
    if isinstance(value, str):
        items = [item for item in re.split(r'[\s,]+', value.strip()) if item]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = [item.strip() for item in value]
    else:
        raise ValueError('allowed-tools 须为工具名字符串或字符串数组。')
    if len(items) > 50 or any(not item or len(item) > 200 for item in items):
        raise ValueError('allowed-tools 最多包含 50 个有效工具名。')
    return list(dict.fromkeys(items))


def resolve_allowed_tools(declared):
    resolved, optional_missing, unknown = [], [], []
    for name in declared or []:
        if name in TOOL_INFO:
            targets = (name,)
        else:
            base = name.split('(', 1)[0].casefold()
            targets = TOOL_ALIASES.get(base, ())
            if not targets:
                (optional_missing if base in OPTIONAL_EXTERNAL_TOOLS else unknown).append(name)
                continue
        for target in targets:
            if target not in resolved:
                resolved.append(target)
    return resolved, optional_missing, unknown


def safe_path(value):
    if (not value or len(value) > 240 or '\\' in value or '\x00' in value or ':' in value
            or value.startswith('/') or any(p in {'', '.', '..'} for p in value.split('/'))):
        raise ValueError('技能包路径无效。')
    return value


def digest(files):
    h = hashlib.sha256()
    for path, data in sorted(files.items()):
        h.update(path.encode()); h.update(b'\0'); h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError('技能元数据包含重复或无效字段。')
        result[key] = loader.construct_object(value_node, deep=deep)
        # Version is a display label: preserve YAML's original spelling (e.g.
        # 1.10 or 2026-09-17) instead of converting it to a float or date.
        if key == 'metadata' and isinstance(value_node, yaml.MappingNode):
            for metadata_key, metadata_value in value_node.value:
                if metadata_key.value == 'version' and isinstance(metadata_value, yaml.ScalarNode):
                    result[key]['version'] = '' if metadata_value.tag == 'tag:yaml.org,2002:null' else metadata_value.value
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def parse_package(filename, data):
    if len(data) > UPLOAD_LIMIT:
        raise ValueError('技能包最多 10 MiB。')
    files = {}
    if filename.lower().endswith('.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 200 or sum(e.file_size for e in entries) > 30 * 1024 * 1024:
                    raise ValueError('技能包解压后最多 30 MiB、200 个文件。')
                seen = set()
                for entry in entries:
                    path = safe_path(entry.filename.rstrip('/'))
                    mode = entry.external_attr >> 16
                    if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or entry.flag_bits & 1:
                        raise ValueError('技能包不支持链接、特殊文件或加密文件。')
                    if path.casefold() in seen:
                        raise ValueError('技能包存在重复路径。')
                    seen.add(path.casefold())
                    if entry.is_dir():
                        continue
                    if PurePosixPath(path).name == '.DS_Store' or path.startswith('__MACOSX/'):
                        continue
                    content = archive.read(entry)
                    if len(content) != entry.file_size:
                        raise ValueError('技能包文件不完整。')
                    files[path] = content
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
            raise ValueError('无法读取此 ZIP 技能包。') from None
        roots = [p for p in files if PurePosixPath(p).name == 'SKILL.md']
        if len(roots) != 1 or len(PurePosixPath(roots[0]).parts) > 2:
            raise ValueError('请导入一个技能，根目录或单一顶层目录须包含 SKILL.md。')
        prefix = roots[0][:-len('SKILL.md')]
        if any(not p.startswith(prefix) for p in files):
            raise ValueError('ZIP 中存在技能目录之外的文件。')
        files = {p[len(prefix):]: value for p, value in files.items()}
    elif filename.lower().endswith('.md'):
        files = {'SKILL.md': data}
    else:
        raise ValueError('请选择 SKILL.md 或 ZIP 技能包。')
    if any(str(parent) in files for name in files for parent in PurePosixPath(name).parents if str(parent) != '.'):
        raise ValueError('技能包路径存在文件和目录冲突。')
    if len(files.get('SKILL.md', b'')) > SKILL_FILE_LIMIT:
        raise ValueError('SKILL.md 最多 256 KiB，请把参考资料拆分到包内其他文件。')
    try:
        text = files['SKILL.md'].decode('utf-8-sig')
    except (KeyError, UnicodeError):
        raise ValueError('SKILL.md 必须是 UTF-8 文本。') from None
    match = re.match(r'\A---\s*\n(.*?)\n---\s*\n(.*)\Z', text, re.S)
    if not match:
        raise ValueError('SKILL.md 需要 YAML 元数据：name 和 description。')
    try:
        if len(match[1]) > 8192 or any(isinstance(t, (AliasToken, AnchorToken)) for t in yaml.scan(match[1])):
            raise ValueError('技能元数据过长或包含不支持的引用。')
        meta = yaml.load(match[1], Loader=UniqueLoader)
    except (yaml.YAMLError, RecursionError):
        raise ValueError('技能 YAML 元数据格式无效。') from None
    if not isinstance(meta, dict) or not isinstance(meta.get('name'), str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', meta['name']) or len(meta['name']) > 64:
        raise ValueError('技能 name 须为最多 64 个小写字母、数字和短横线。')
    if not isinstance(meta.get('description'), str) or not 1 <= len(meta['description'].strip()) <= 1024:
        raise ValueError('技能 description 须为 1–1024 个字符。')
    body = match[2].strip()
    if not body:
        raise ValueError('SKILL.md 正文不能为空，请在 YAML 元数据后填写技能说明。')
    if len(body) > SKILL_BODY_LIMIT:
        raise ValueError(f'SKILL.md 正文共 {len(body)} 个字符，超过 {SKILL_BODY_LIMIT} 字符上限。请把参考资料拆分到 references/ 文件夹，并在正文中说明何时读取。')
    metadata = meta.get('metadata', {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError('metadata 必须是键值对象，例如 metadata: {version: "1.0"}；也可以省略。')
    # Extra descriptive metadata stays in the original file and is not used
    # for permissions or execution. Lists and nested mappings are allowed.
    version = metadata.get('version', '')
    if not isinstance(version, str):
        raise ValueError('metadata.version 必须是单个版本值，例如 "1.0"，不能是列表或对象。')
    display_name = meta['name']
    # Community packages often keep catalog-only presentation metadata in
    # manifest.json rather than duplicating it in SKILL.md frontmatter.
    if 'manifest.json' in files:
        try:
            manifest = json.loads(files['manifest.json'].decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            manifest = None
        if isinstance(manifest, dict) and manifest.get('name') in (None, meta['name']):
            manifest_version = manifest.get('version', '')
            if not version and isinstance(manifest_version, str):
                version = manifest_version
            localized = manifest.get('localized') or {}
            zh = localized.get('zh') if isinstance(localized, dict) else None
            candidate = (zh or {}).get('display_name') if isinstance(zh, dict) else None
            candidate = candidate or manifest.get('display_name')
            if isinstance(candidate, str) and candidate.strip():
                display_name = candidate.strip()[:120]
    compatibility = meta.get('compatibility', '')
    if not isinstance(compatibility, str) or len(compatibility) > 500:
        raise ValueError('compatibility 须为最多 500 个字符。')
    declared = parse_allowed_tools(meta.get('allowed-tools'))
    resolved_declared, optional_external_tools, unknown_declared_tools = resolve_allowed_tools(declared)
    extension = {
        'schema_version': 1, 'required_tools': [], 'requires_knowledge_base': False,
        'optional_tools': [], 'support_level': 'full', 'limitations': [],
        'output_contract': 'none',
        'activation': {'auto': True, 'user_invocable': True, 'internal_only': False,
                       'keywords': [], 'priority': 50},
        'conflicts_with': [], 'connector_dependencies': [], 'scripts': [],
    }
    if 'workbench.json' in files:
        try:
            extra = json.loads(files['workbench.json'])
            if not isinstance(extra, dict) or set(extra) - set(extension): raise ValueError()
            extension.update(extra)
            if type(extension['schema_version']) is not int or extension['schema_version'] not in {1, 2}: raise ValueError()
            if type(extension['requires_knowledge_base']) is not bool: raise ValueError()
            if not isinstance(extension['required_tools'], list) or len(extension['required_tools']) > 20 or any(not isinstance(t,str) for t in extension['required_tools']): raise ValueError()
            if not isinstance(extension['optional_tools'], list) or len(extension['optional_tools']) > 20 or any(not isinstance(t,str) for t in extension['optional_tools']): raise ValueError()
            if extension['support_level'] not in {'full','partial'}: raise ValueError()
            if extension['output_contract'] not in {'none','optional','artifact'}: raise ValueError()
            if (not isinstance(extension['limitations'],list) or len(extension['limitations'])>20
                    or any(not isinstance(x,str) or not 1<=len(x.strip())<=300 for x in extension['limitations'])): raise ValueError()
            activation=extension['activation']
            if not isinstance(activation,dict) or set(activation)-{'auto','user_invocable','internal_only','keywords','priority'}: raise ValueError()
            activation={**{'auto':True,'user_invocable':True,'internal_only':False,'keywords':[],'priority':50},**activation}
            if any(type(activation[k]) is not bool for k in ('auto','user_invocable','internal_only')): raise ValueError()
            if activation['internal_only'] and activation['user_invocable']: raise ValueError()
            if (not isinstance(activation['keywords'],list) or len(activation['keywords'])>30
                    or any(not isinstance(k,str) or not 1<=len(k.strip())<=80 for k in activation['keywords'])): raise ValueError()
            if type(activation['priority']) is not int or not 0<=activation['priority']<=100: raise ValueError()
            activation['keywords']=[k.strip() for k in activation['keywords']]
            extension['activation']=activation
            if (not isinstance(extension['conflicts_with'],list) or len(extension['conflicts_with'])>20
                    or any(not isinstance(x,str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',x) for x in extension['conflicts_with'])): raise ValueError()
            dependencies=extension['connector_dependencies']
            if not isinstance(dependencies,list) or len(dependencies)>12: raise ValueError()
            for dep in dependencies:
                if (not isinstance(dep,dict) or set(dep)-{'package_id','name','min_version','tools'}
                        or not isinstance(dep.get('package_id'),str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',dep['package_id'])
                        or not isinstance(dep.get('name',dep['package_id']),str)
                        or not isinstance(dep.get('min_version',''),str)
                        or not isinstance(dep.get('tools',[]),list)
                        or any(not isinstance(x,str) for x in dep.get('tools',[]))): raise ValueError()
            scripts=extension['scripts']
            if not isinstance(scripts,list) or len(scripts)>8: raise ValueError()
            ids=set()
            for script in scripts:
                if (not isinstance(script,dict) or set(script)-{'id','path','runtime','timeout','network','read_notes'}
                        or not isinstance(script.get('id'),str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',script['id'])
                        or script['id'] in ids or not isinstance(script.get('path'),str)
                        or script.get('runtime','python') not in {'python','node'}
                        or type(script.get('timeout',15)) is not int or not 1<=script.get('timeout',15)<=60
                        or script.get('network','none') not in {'none','full'}
                        or type(script.get('read_notes',False)) is not bool): raise ValueError()
                ids.add(script['id']); path=safe_path(script['path'])
                runtime=script.get('runtime','python')
                suffix=PurePosixPath(path).suffix.lower()
                allowed_suffixes={'.py'} if runtime=='python' else {'.js','.cjs','.mjs'}
                if not path.startswith('scripts/') or suffix not in allowed_suffixes or path not in files or len(files[path])>SCRIPT_LIMIT: raise ValueError()
                script.update(runtime=runtime,timeout=script.get('timeout',15),network=script.get('network','none'),read_notes=script.get('read_notes',False))
        except (ValueError, UnicodeError):
            raise ValueError('workbench.json 依赖声明无效。') from None
    reasons = []
    script_paths={s['path'] for s in extension['scripts']}
    runtime_suffixes={'.py','.js','.cjs','.mjs'}
    runtime_support_paths={p for p in files if extension['scripts'] and p.startswith('scripts/')
                           and PurePosixPath(p).suffix.lower() in runtime_suffixes}
    unsupported = [p for p in files if (p.startswith('scripts/') and p not in runtime_support_paths)
                   or (p not in runtime_support_paths and PurePosixPath(p).suffix.lower() not in TEXT_SUFFIXES)]
    if unsupported:
        reasons.append('包含未声明脚本或当前不支持的资源类型。')
    python_scripts=[script for script in extension['scripts'] if script['runtime']=='python']
    if python_scripts:
        lock=files.get('requirements.lock')
        if lock is None: reasons.append('脚本技能必须包含 requirements.lock。')
        else:
            try: lock_text=lock.decode('utf-8-sig')
            except UnicodeError: lock_text='invalid'
            if len(lock)>65536 or any(line.strip() and not line.lstrip().startswith('#') for line in lock_text.splitlines()):
                reasons.append('当前脚本沙箱只支持无第三方依赖；requirements.lock 只能包含注释。')
    if any(script['network'] != 'none' for script in extension['scripts']):
        reasons.append('当前脚本沙箱默认断网；声明外部网络访问的脚本暂不兼容。')
    unknown = set(extension['required_tools']) - set(TOOL_INFO)
    if unknown or unknown_declared_tools:
        reasons.append('声明了当前不支持的工具。')
    missing_optional = set(extension['optional_tools']) - set(TOOL_INFO)
    support_level = 'incompatible' if reasons else extension['support_level']
    limitations = [x.strip() for x in extension['limitations']]
    if optional_external_tools and not reasons:
        support_level = 'partial'
        limitations.append('部分外部工具在当前工作台没有等价能力：' + ', '.join(optional_external_tools))
    if missing_optional and not reasons:
        support_level = 'partial'
        limitations.append('部分可选工具在当前工作台不可用：' + ', '.join(sorted(missing_optional)))
    listing = []
    for path, content in sorted(files.items()):
        safe_path(path)
        readable = path not in unsupported and path not in runtime_support_paths
        if readable:
            if len(content) > TEXT_LIMIT:
                raise ValueError('单个技能文本资源最多 512 KiB。')
            try: content.decode('utf-8-sig')
            except UnicodeError: raise ValueError('技能文本资源必须使用 UTF-8 编码。') from None
            if b'\0' in content: raise ValueError('技能文本资源包含无效字符。')
        listing.append({'path':path, 'bytes':len(content), 'sha256':hashlib.sha256(content).hexdigest(), 'readable':readable})
    return {'name':meta['name'], 'display_name':display_name, 'description':meta['description'].strip(), 'body':body,
            'version':version[:80], 'compatibility':compatibility,
            'declared_tools':declared, 'required_tools':extension['required_tools'],
            'optional_tools':extension['optional_tools'], 'support_level':support_level,
            'output_contract':extension['output_contract'],
            'limitations':limitations,
            'requires_knowledge_base':extension['requires_knowledge_base'],
            'activation':extension['activation'], 'conflicts_with':extension['conflicts_with'],
            'connector_dependencies':extension['connector_dependencies'], 'scripts':extension['scripts'],
            'compatible':not reasons, 'reasons':reasons, 'files':listing,
            'revision':digest(files), 'suggested_tools':resolved_declared if declared is not None else list(TOOL_INFO)}, files
