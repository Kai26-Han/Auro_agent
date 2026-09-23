"""Versioned Mem0 capabilities and closed storage namespaces.

Ordinary facts and workbench events are enabled. Procedures require explicit user publication.
"""
KINDS = ('ordinary', 'event', 'procedure')
CATEGORIES = {'info': '个人信息', 'preference': '偏好', 'goal': '目标',
              'constraint': '长期约束', 'other': '其他信息'}


def kind(value):
    if value not in KINDS:
        raise ValueError('无效的 Mem0 记忆通道。')
    return value


def category(value):
    if value not in CATEGORIES:
        raise ValueError('记忆类型无效。')
    return value


def capabilities():
    return {'sdk': '1.0.11', 'revision': 'mr5', 'complete': True, 'channels': {
        'ordinary': {'sdk_supported': True, 'enabled': True,
                     'implementation': 'native_with_workbench_categories'},
        'event': {'sdk_supported': False, 'enabled': True,
                  'implementation': 'workbench_events_native_direct_crud'},
        'procedure': {'sdk_supported': True, 'enabled': True,
                      'implementation': 'native_procedural_with_reviewed_revisions'},
    }}
