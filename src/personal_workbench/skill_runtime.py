"""Hermes-style skill discovery, invocation and progressive disclosure."""
import json
import re

from langchain_core.tools import tool

from personal_workbench.skill_store import SkillStore


MAX_SKILLS_PER_ACTOR = 3
RESOURCE_CHUNK_CHARS = 12000
RESOURCE_TURN_CHARS = 48000
AUTO_TRIGGER_STOP_TERMS = {
    '使用', '帮我', '需要', '可以', '这个', '那个', '进行', '一个', 'the', 'and',
    'use', 'using', 'with', 'for', 'skill', 'agent',
}


def _active_skills(store):
    return [item for item in store.list()
            if item['enabled'] and not item['archived'] and item['compatible']
            and item.get('runtime_ready', True)]


def skill_index_context(settings):
    """Only advertise metadata in the stable system prompt."""
    skills = [{'name': item['name'], 'display_name': item['display_name'],
               'description': item['description'],
               'support_level': item.get('support_level', 'full'),
               'callable': bool(item.get('runtime_ready')),
               'limitations': item.get('limitations', [])}
              for item in _active_skills(SkillStore(settings))]
    if not skills:
        return ''
    return ('\n\n【可用技能】\n技能是按需加载的任务方法。只根据名称和简介判断是否相关；'
            '需要使用时先调用 skill_view(name)，不要根据简介猜测具体步骤。'
            '列表中的技能全部可调用；support_level=partial 只表示部分附加能力受限，不表示技能不可用。'
            '判断是否可用应以 callable、enabled 和 compatible 为准，不要为了把 partial 改成 full 而修改第三方技能包。'
            '用户明确选择的技能会随当前用户消息完整加载，应优先按其中步骤执行。\n'
            + json.dumps(skills, ensure_ascii=False))


def skill_invocation_message(settings, snapshot, user_text):
    """Bind explicitly selected skills to this user turn, without mutating the system prompt."""
    refs = snapshot.get('skill_refs', []) if snapshot else []
    if not refs:
        return user_text
    store = SkillStore(settings)
    blocks = []
    for ref in refs:
        meta = store.revision(ref['id'], ref['revision'])
        resources = [file['path'] for file in meta['files'] if file['readable']
                     and file['path'] not in {'SKILL.md', 'workbench.json', 'requirements.lock'}]
        blocks.append({
            'skill_id': ref['id'],
            'name': ref.get('package_name') or meta['name'],
            'display_name': ref.get('name') or meta['name'],
            'revision': ref['revision'],
            'selection': ref.get('selection', 'manual'),
            'instructions': meta['body'],
            'resources': _resource_manifest(resources),
            'support_level': meta.get('support_level', 'full'),
            'limitations': meta.get('limitations', []),
        })
    return ('[Skill Invocation]\n'
            '用户明确调用了下面的技能来完成本次任务。把技能作为本次任务的方法并优先遵循；'
            'support_level=partial 表示技能可调用，但应遵守 limitations 中列出的能力边界；不要因此拒绝任务或要求修改技能包。'
            '技能要求先询问时，不要提前检索或生成。只在步骤需要时使用工具；'
            '调用 read_skill_resource 时使用对应技能块中的 skill_id，不要猜测。\n\n'
            '<skills>\n' + json.dumps(blocks, ensure_ascii=False) + '\n</skills>\n\n'
            '<user_request>\n' + user_text + '\n</user_request>')


def _resource_manifest(paths):
    """Keep the initial prompt small while preserving resource discovery.

    A skill can contain hundreds of template files.  Sending every immutable
    path (plus importer diagnostics) on every model call wastes the context
    window.  Root resources and shallow indexes are enough for progressive
    disclosure; deeper files are discoverable from those indexes.
    """
    visible, groups = [], {}
    for path in paths:
        parts = path.split('/')
        if len(parts) <= 2:
            visible.append(path)
        else:
            groups[parts[0]] = groups.get(parts[0], 0) + 1
    return {
        'files': visible,
        'groups': [{'path': name, 'additional_files': count}
                   for name, count in sorted(groups.items())],
        'total_files': len(paths),
        'usage': ('先读取上面的 README、索引或清单，再按其中给出的精确路径读取深层资源；'
                  '不要猜测资源路径。'),
    }


def _terms(value):
    text = (value or '').casefold()
    words = set(re.findall(r'[a-z0-9][a-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}', text))
    chinese = ''.join(re.findall(r'[\u4e00-\u9fff]', text))
    words.update(chinese[i:i + 2] for i in range(max(0, len(chinese) - 1)))
    return words - AUTO_TRIGGER_STOP_TERMS


def _compact(value):
    return ''.join(re.findall(r'[a-z0-9\u4e00-\u9fff]', (value or '').casefold()))


def automatic_skill_refs(store, text, selected=(), limit=MAX_SKILLS_PER_ACTOR):
    """本地确定性路由；不调用模型，也不会自动加载内部专用技能。"""
    chosen = list(selected)
    has_explicit_selection = bool(chosen)
    # 用户或伙伴已经明确选择了方法时，保持选择的确定性。自动叠加会让
    # 未经选择的技能获得工具范围，也让失败原因难以复现。
    if has_explicit_selection:
        return chosen[:limit]
    selected_ids = {r['id'] for r in chosen}
    query = _terms(text)
    candidates = []
    compact_query = _compact(text)
    for item in store.list():
        if item['id'] in selected_ids or not item['enabled'] or item['archived'] or not item['compatible']:
            continue
        if not item.get('auto_trigger') or item.get('internal_only'):
            continue
        activation = item.get('activation', {})
        haystack = _terms(' '.join([item['display_name'], item['name'], item['description'], *activation.get('keywords', [])]))
        exact = sum(3 for keyword in activation.get('keywords', []) if keyword.casefold() in (text or '').casefold())
        names = {_compact(item['name']), _compact(item['display_name'])}
        named = any(len(name) >= 3 and name in compact_query for name in names)
        score = len(query & haystack) + exact + (8 if named else 0)
        # A single generic description term is too weak once every installed
        # skill participates in automatic routing. Explicit names/keywords or
        # two independent metadata terms are required.
        # A manually selected skill already expresses the user's intended
        # method.  Do not silently combine a second skill from broad prose
        # similarity (for example, a generic development skill matching the
        # words "project" and "design" in a logo request).  An additional
        # automatic skill must have an explicit name or activation keyword.
        if named or exact or (not has_explicit_selection and len(query & haystack) >= 2):
            candidates.append((score, activation.get('priority', 50), item['display_name'], item))
    candidates.sort(key=lambda row: (-row[0], -row[1], row[2]))
    selected_names = {store.get(ref['id'])['name'] for ref in chosen}
    for _, _, _, item in candidates:
        if len(chosen) >= limit:
            break
        if set(item.get('conflicts_with', [])) & selected_names:
            continue
        if any(item['name'] in set(store.get(ref['id']).get('conflicts_with', [])) for ref in chosen):
            continue
        chosen.append({'id': item['id'], 'revision': item['revision'], 'selection': 'auto'})
        selected_names.add(item['name'])
    return chosen


def resolve_skills(store, refs, available_tools, has_knowledge, frozen=None):
    if frozen is not None:
        refs = frozen
    if not refs:
        return [], list(available_tools)
    if len(refs) > MAX_SKILLS_PER_ACTOR:
        raise ValueError(f'每个执行者最多组合 {MAX_SKILLS_PER_ACTOR} 个技能。')
    if len({ref['id'] for ref in refs}) != len(refs):
        raise ValueError('不能重复选择同一个技能。')
    entries, names, resolved_meta = [], set(), []
    for ref in refs:
        item = store.get(ref['id'])
        meta = store.revision(ref['id'], ref['revision'])
        if frozen is None:
            if not item['enabled'] or item['archived']:
                raise ValueError('所选技能已停用或移除，请重新选择。')
            if not meta['compatible']:
                raise ValueError('此技能版本不兼容，无法使用。')
            if not item.get('runtime_ready', True):
                labels = {
                    'configuration_required': '需要先完成运行环境配置',
                    'limited': '当前导入不完整，尚不能执行',
                    'disabled': '已停用',
                    'incompatible': '与当前版本不兼容',
                }
                reason = labels.get(item.get('runtime_status'), '尚未通过运行验证')
                raise ValueError(f'技能“{item["display_name"]}”{reason}，请到技能广场处理。')
            selection = ref.get('selection', 'manual')
            if selection == 'manual' and (not item.get('user_invocable', True) or item.get('internal_only', False)):
                raise ValueError('此技能不允许在对话中手动选择。')
            allowed = list(item['allowed_tools'])
        else:
            allowed, selection = ref['allowed_tools'], ref.get('selection', 'manual')
        if meta['requires_knowledge_base'] and not has_knowledge:
            raise ValueError(f'技能“{item["display_name"]}”需要先选择可用知识库。')
        if set(meta['required_tools']) - set(available_tools):
            raise ValueError(f'技能“{item["display_name"]}”缺少必需工具，请检查知识库类型和工具设置。')
        if set(meta.get('conflicts_with', [])) & names:
            raise ValueError('所选技能存在冲突，请减少组合。')
        package_name = meta.get('name', item.get('name', ref['id']))
        names.add(package_name)
        resolved_meta.append((meta, package_name))
        scripts_enabled = bool(item.get('scripts_enabled') and meta.get('scripts')) if frozen is None else bool(ref.get('scripts_enabled'))
        entry = ref if frozen is not None else {
            'id': item['id'], 'revision': meta['revision'], 'name': item['display_name'],
            'package_name': package_name, 'version': meta.get('version') or item.get('version',''), 'selection': selection,
            'allowed_tools': allowed, 'required_tools': meta['required_tools'],
            'requires_knowledge_base': meta['requires_knowledge_base'], 'scripts_enabled': scripts_enabled,
            'output_contract': meta.get('output_contract', 'none'),
        }
        entries.append(entry)
    for meta, package_name in resolved_meta:
        if set(meta.get('conflicts_with', [])) & (names - {package_name}):
            raise ValueError('所选技能存在冲突，请减少组合。')
    contextual = ['skill_view', 'read_skill_resource']
    if any(entry.get('scripts_enabled') for entry in entries):
        contextual.append('run_skill_script')
    return entries, list(dict.fromkeys([*available_tools, *contextual]))


# 保留旧调用名，避免第三方扩展和旧测试失效。
resolve_skill = resolve_skills


def assemble_skill(settings, snapshot, tools):
    """Mount discovery and selected-version resource tools.

    Full selected skill bodies are deliberately absent here: they are bound to
    the current user turn by ``skill_invocation_message``.
    """
    refs = snapshot.get('skill_refs', [])
    store = SkillStore(settings)
    context = skill_index_context(settings)
    selected = {}
    for ref in refs:
        meta = store.revision(ref['id'], ref['revision'])
        selected[meta['name']] = ref
        selected[ref['id']] = ref

    def resolve_ref(name):
        ref = selected.get(name)
        if ref:
            return ref, store.revision(ref['id'], ref['revision'])
        item = next((value for value in _active_skills(store)
                     if value['name'] == name or value['id'] == name), None)
        if not item:
            raise ValueError('技能不存在、未启用或不兼容。')
        ref = {'id': item['id'], 'revision': item['revision']}
        return ref, store.revision(item['id'], item['revision'])

    def view_skill(name: str, path: str = '', offset: int = 0) -> dict:
        if offset < 0:
            return {'error': '资源偏移不能为负数。'}
        try:
            ref, meta = resolve_ref(name)
            if not path or path == 'SKILL.md':
                content = meta['body']
                actual_path = 'SKILL.md'
                sha = next((f['sha256'] for f in meta['files'] if f['path'] == 'SKILL.md'), ref['revision'])
            else:
                content, sha = store.resource(ref['id'], ref['revision'], path)
                actual_path = path
            if offset >= len(content) and offset != 0:
                return {'error': '资源偏移超出文本范围。'}
            part = content[offset:offset + RESOURCE_CHUNK_CHARS]
            return {'name': meta['name'], 'text': part, 'characters': len(part),
                    'next_offset': offset + len(part) if offset + len(part) < len(content) else None,
                    'skill_read': {'skill_id': ref['id'], 'revision': ref['revision'], 'path': actual_path,
                                   'sha256': sha, 'offset': offset, 'characters': len(part)}}
        except ValueError as exc:
            return {'error': str(exc)}

    def read_resource(skill_id: str = '', path: str = '', offset: int = 0) -> dict:
        if offset < 0:
            return {'error': '资源偏移不能为负数。'}
        if not skill_id and len(refs) == 1:
            skill_id = refs[0]['id']
        # The exact UUID is included in the invocation block, but accept the
        # frozen package/display name as a robust alias.  Older checkpoints
        # did not expose the UUID and models naturally used the package name.
        matches = []
        for value in refs:
            meta = store.revision(value['id'], value['revision'])
            aliases = {
                value['id'], value.get('package_name', ''), value.get('name', ''),
                meta.get('name', ''),
            }
            if skill_id in aliases:
                matches.append(value)
        ref = matches[0] if len(matches) == 1 else None
        if not ref:
            return {'error': '请使用本轮技能清单中的 skill_id 或唯一技能名称。'}
        try:
            content, sha = store.resource(ref['id'], ref['revision'], path)
            if offset >= len(content) and offset != 0:
                return {'error': '资源偏移超出文本范围。'}
            part = content[offset:offset + RESOURCE_CHUNK_CHARS]
            return {'text': part, 'characters': len(part),
                    'next_offset': offset + len(part) if offset + len(part) < len(content) else None,
                    'skill_read': {'skill_id': ref['id'], 'revision': ref['revision'], 'path': path,
                                   'sha256': sha, 'offset': offset, 'characters': len(part)}}
        except ValueError as exc:
            return {'error': str(exc)}
    filtered = [item for item in tools if item.name in snapshot['tool_ids']]
    mounted = filtered
    if 'skill_view' in snapshot['tool_ids']:
        mounted.append(build_skill_view_tool(view_skill))
    if 'propose_skill' in snapshot['tool_ids']:
        from personal_workbench.skill_proposals import build_propose_skill_tool
        mounted.append(build_propose_skill_tool(settings))
    if refs and 'read_skill_resource' in snapshot['tool_ids']:
        mounted.append(build_skill_resource_tool(read_resource))
    executable = [ref for ref in refs if ref.get('scripts_enabled')]
    if executable:
        from personal_workbench.skill_sandbox import build_skill_script_tool
        mounted.append(build_skill_script_tool(settings, store, executable, snapshot.get('run_id','')))
    return context, mounted


def build_skills_list_tool(settings):
    @tool
    def skills_list() -> dict:
        """列出当前可调用的已启用技能。partial 表示可调用但部分附加能力受限；需要方法细节时使用 skill_view。"""
        return {'skills': [{'name': item['name'], 'display_name': item['display_name'],
                            'description': item['description'],
                            'support_level': item.get('support_level', 'full'),
                            'callable': True,
                            'status': ('available_with_limitations'
                                       if item.get('support_level') == 'partial' else 'available'),
                            'limitations': item.get('limitations', []),
                            'runtime_ready': item.get('runtime_ready', True),
                            'runtime_status': item.get('runtime_status', 'ready'),
                            'version': item.get('version', '')}
                           for item in _active_skills(SkillStore(settings))]}
    return skills_list


def build_skill_view_tool(reader):
    @tool
    def skill_view(name: str, path: str = '', offset: int = 0) -> dict:
        """按名称加载完整技能说明，或读取技能目录内的一个参考文件；长内容用 offset 分页。"""
        return reader(name, path, offset)
    return skill_view


def build_skill_resource_tool(reader):
    @tool
    def read_skill_resource(path: str, offset: int = 0, skill_id: str = '') -> dict:
        """读取本轮技能的 UTF-8 参考资料。组合多个技能时必须传 skill_id；offset 用于分页。"""
        return reader(skill_id, path, offset)
    return read_skill_resource


def resource_tool_catalog():
    value = build_skill_resource_tool(None)
    return {'id': value.name, 'name': '读取技能资源', 'description': '读取本轮已选技能的参考资料和文本模板。',
            'applicability': '仅在选择技能时可用，只能读取已冻结技能版本内的文本。',
            'source': 'builtin', 'availability': 'contextual', 'schema': value.args_schema.model_json_schema()}
