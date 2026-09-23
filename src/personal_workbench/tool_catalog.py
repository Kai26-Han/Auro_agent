"""Read-only paged tool directory; browsing never grants execution permission."""
import json
import re
from functools import lru_cache
from pathlib import Path

from personal_workbench.file_tools import builtin_tool_catalog
from personal_workbench.tool_policy import catalog_policy


@lru_cache(maxsize=1)
def translations():
    # Reuse the displayed labels so searching English UI names also works.
    path = Path(__file__).resolve().parents[2] / 'frontend/src/locales/en.json'
    return json.loads(path.read_text()) if path.is_file() else {}


def status(tool):
    if tool['source'] == 'builtin': return 'contextual'
    if tool['availability'] == 'disconnected': return 'disconnected'
    return 'available' if tool['policy'] != 'disabled' else 'connected'


def entry(tool, connector=None):
    item = {**tool, 'connector_id': connector['id'] if connector else None,
            'connector_name': connector['name'] if connector else '工作台内置',
            'source': 'mcp' if connector else 'builtin',
            'policy': tool.get('policy', 'contextual'),
            'availability': connector['status'] if connector else 'contextual'}
    declaration = catalog_policy(tool, connector)
    item['execution_policy'] = declaration
    for key in ('effect','approval','idempotency','retry','timeout_seconds'):
        item[key] = declaration.get(key) if declaration else None
    item['status'] = status(item)
    return item


def page(connectors, *, q='', source='all', connector_id=None, policy='all', availability='all', page=1, page_size=20):
    connected = connectors.list(summary=True)
    tools = [entry(tool) for tool in builtin_tool_catalog()]
    tools += [entry(tool, c) for c in connected for tool in c['tools']]
    total = len(tools)
    words = q.casefold().split()
    labels = translations()
    def matches(tool):
        if source != 'all' and tool['source'] != source: return False
        if connector_id and tool['connector_id'] != connector_id: return False
        if policy != 'all' and tool['policy'] != policy: return False
        if availability != 'all' and tool['status'] != availability: return False
        values = [str(tool.get(key) or '') for key in ('id', 'name', 'description', 'connector_name')]
        if tool['source'] == 'builtin': values += [labels.get(value, '') for value in values]
        text = ' '.join(values).casefold()
        return all(word in text for word in words)
    tools = sorted(filter(matches, tools), key=lambda t: (t['source'] != 'builtin', t['connector_name'].casefold(), t['name'].casefold(), t['id']))
    count = len(tools)
    pages = max(1, (count + page_size - 1) // page_size)
    page = min(page, pages)
    fields = ('id', 'name', 'description', 'connector_id', 'connector_name', 'source', 'policy', 'availability', 'status',
              'effect', 'approval', 'idempotency', 'retry', 'timeout_seconds')
    items = [{key: tool.get(key) for key in fields} for tool in tools[(page-1)*page_size:page*page_size]]
    for item in items: item['description'] = item['description'][:180]
    return {'items':items, 'total':total, 'filtered_total':count, 'page':page, 'page_size':page_size, 'pages':pages,
            'connectors':sorted([{'id':c['id'], 'name':c['name'], 'tool_count':len(c['tools'])} for c in connected], key=lambda c:(c['name'].casefold(), c['id']))}


def detail(connectors, tool_id):
    if not tool_id.startswith('mcp_'):
        return next((entry(tool) for tool in builtin_tool_catalog() if tool['id'] == tool_id), None)
    match = re.fullmatch(r'mcp_([a-f0-9]{16})_[a-f0-9]{12}', tool_id)
    if not match: return None
    with connectors.lock:
        try: connector = connectors.store.public(match[1])
        except ValueError: return None
        connector['status'] = 'connected' if connectors.connected(connector['id']) else 'disconnected'
        tool = next((t for t in connector['tools'] if t['id'] == tool_id), None)
        return {**entry(tool, connector), 'applicability':'对话或伙伴中明确选择后可用。'} if tool else None


def compact_connections(items):
    # Pickers need names and permissions, never command arguments or JSON schemas.
    return [{'id':c['id'], 'name':c['name'], 'status':c['status'], 'transport':c['transport'],
             'tools':[{k:(t[k][:180] if k=='description' else t[k]) for k in ('id','name','description','policy','read_hint')} for t in c['tools']]}
            for c in items]
