"""Experimental decision-tool settings. Credentials never enter public snapshots."""
from pydantic import BaseModel, ConfigDict, Field


class DecisionToolsConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    jev_decide: bool = False
    model: str = Field(default='jev-latest', pattern=r'^jev-[a-zA-Z0-9.-]{1,80}$')
    api_key: str = Field(default='', max_length=4096, repr=False)
    clear_key: bool = False
    timeout: int = Field(default=15, ge=5, le=60)
    max_state_chars: int = Field(default=12000, ge=1000, le=16000)

    def enabled_ids(self):
        return ['jev_decide'] if self.jev_decide else []

    def public(self):
        return {**self.model_dump(exclude={'api_key','clear_key'}), 'api_key_set':bool(self.api_key)}
