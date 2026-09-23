"""Public memory configuration; credentials remain in model profiles."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Mem0Config(BaseModel):
    events: bool = True
    procedures: bool = True
    context_strategy: Literal["recent", "summary"] = "recent"
    model_config = ConfigDict(extra="forbid")
    model_profile_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    embedding_profile_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    similarity_threshold: float = Field(default=0.3, ge=0, le=1)


class LangMemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    episodes: bool = True
    hot_path: bool = False
    debounce_seconds: int = Field(default=60, ge=0, le=300)
    retrieval: Literal["keyword", "semantic"] = "keyword"
    embedding_profile_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    similarity_threshold: float = Field(default=0.3, ge=0, le=1)


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: Literal["langmem", "mem0"] = "langmem"
    enabled: bool = True
    use_memories: bool = True
    learn_memories: bool = False
    model_profile_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    recall_limit: int = Field(default=8, ge=1, le=20)
    context_chars: int = Field(default=4000, ge=500, le=12000)
    langmem: LangMemConfig = Field(default_factory=LangMemConfig)
    mem0: Mem0Config = Field(default_factory=Mem0Config)


class MemorySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    write_memories: bool = True
    enabled: bool = True
    space_id: str = Field(default="personal", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    use_memories: bool = True
    learn_memories: bool = True
    scope_kind: Literal['personal', 'project', 'partner'] = 'personal'
    scope_id: str = Field(default='personal', pattern=r'^[a-zA-Z0-9_-]{1,64}$')


class SpaceInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    engine: Literal["langmem", "mem0"] | None = None

    @field_validator("name")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("请填写记忆空间名称。")
        return value.strip()


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=1000)
    category: Literal["preference", "fact", "goal", "experience"] = "fact"
    memory_type: Literal['fact', 'profile'] = 'fact'
    profile_key: str = Field(default='', max_length=50)
    scope_kind: Literal['personal', 'project', 'partner'] = 'personal'
    scope_id: str = Field(default='personal', pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    conditions: str = Field(default='', max_length=500)
    topic_key: str = Field(default='', max_length=80)
    locked: bool = True

    @field_validator("content")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("请填写记忆内容。")
        return value.strip()


class ProfileFieldInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str = Field(min_length=1, max_length=40)
    description: str = Field(default='', max_length=200)
    content: str = Field(default='', max_length=1000)


class MemoryEdit(MemoryInput):
    version: int = Field(ge=1)


class RecallInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    scope_kind: Literal['personal', 'project', 'partner'] = 'personal'
    scope_id: str = Field(default='personal', pattern=r'^[a-zA-Z0-9_-]{1,64}$')

    @field_validator("query")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("请输入测试问题。")
        return value.strip()


class ScopeInput(BaseModel):
    kind: Literal['project', 'partner']
    name: str = Field(min_length=1, max_length=80)


class MemoryStateInput(BaseModel):
    version: int = Field(ge=1)
    status: Literal['active', 'archived']


class ReviewInput(BaseModel):
    action: Literal['keep', 'replace']
    target_version: int = Field(ge=1)
