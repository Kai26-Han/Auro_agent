"""Observable LangMem episodes; outcome and source authority are server-owned."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Outcome = Literal['success','partial','failure','unknown']
class EpisodeNote(BaseModel):
    text: str = Field(max_length=300, min_length=1)
    source_id: str = Field(max_length=160)
    quote: str = Field(max_length=500, min_length=2)

class EpisodeDraft(BaseModel):
    """Short retrospective of observable events. Never include hidden reasoning."""
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1,max_length=100)
    task_type: Literal['learning','coding','research','writing','general'] = 'general'
    context: str = Field(default='',max_length=800)
    lessons: list[EpisodeNote] = Field(default_factory=list,max_length=4)
    applicability: str = Field(default='',max_length=400)

class EpisodeEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    title: str = Field(min_length=1,max_length=100)
    context: str = Field(default='',max_length=800)
    applicability: str = Field(default='',max_length=400)
    outcome: Outcome
    reason: str = Field(min_length=2,max_length=500)
    locked: bool = True

class EpisodeState(BaseModel):
    version: int = Field(ge=1)
    status: Literal['active','archived']

class EpisodeMerge(BaseModel):
    version: int = Field(ge=1)
    target_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    target_version: int = Field(ge=1)

class EpisodeSessionInput(BaseModel):
    space_id: str = Field(default='personal',pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    scope_kind: Literal['personal','project','partner'] = 'personal'
    scope_id: str = Field(default='personal',pattern=r'^[a-zA-Z0-9_-]{1,64}$')
