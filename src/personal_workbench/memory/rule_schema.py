"""User-owned procedural preferences; optimizer cannot choose scope or permissions."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

Topic = Literal['explanation','presentation','workflow','verification']
class RuleProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(default=0, ge=0)
    scope_kind: Literal['personal','project','partner'] = 'personal'
    scope_id: str = Field(default='personal',pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    topic: Topic = 'explanation'
    title: str = Field(min_length=2,max_length=80)
    instruction: str = Field(min_length=5,max_length=500)
    applies_when: str = Field(min_length=2,max_length=200)
    exclusions: str = Field(default='',max_length=200)
    always_on: bool = False
    priority: int = Field(default=3,ge=1,le=5)
    feedback: str = Field(default='',max_length=1000)
    episode_ids: list[Annotated[str,Field(pattern=r'^[a-f0-9]{32}$')]] = Field(default_factory=list,max_length=4)

    @field_validator('title','instruction','applies_when')
    @classmethod
    def nonblank(cls,v):
        if not v.strip():raise ValueError('请填写规则及适用条件。')
        return v.strip()

class RuleAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    version: int = Field(ge=1)

class RuleJudge(BaseModel):
    """Separate evaluation call; only short verdicts, not hidden reasoning."""
    model_config = ConfigDict(extra='forbid')
    source_supported: bool
    condition_bounded: bool
    no_authority_change: bool
    follows_feedback: bool
    no_regression: bool
    explanation: str = Field(max_length=700)
