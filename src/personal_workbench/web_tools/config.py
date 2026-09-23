"""Web tools are opt-in; credentials remain in the private settings store."""
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, model_validator


class WebToolsConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    web_search: bool = False
    web_fetch: bool = False
    paper_search: bool = False
    provider: Literal['bing', 'duckduckgo', 'tavily', 'brave', 'searxng'] = 'bing'
    api_key: str = Field(default='', max_length=4096, repr=False)
    clear_key: bool = False
    base_url: str = Field(default='', max_length=2048)
    max_results: int = Field(default=5, ge=1, le=10)
    timeout: int = Field(default=20, ge=5, le=60)
    max_chars: int = Field(default=12000, ge=1000, le=50000)

    @model_validator(mode='after')
    def endpoint(self):
        self.base_url = self.base_url.strip().rstrip('/')
        if self.base_url:
            u = urlsplit(self.base_url)
            if u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError('SearXNG 地址须为 HTTP(S) 服务地址，不含密钥、查询参数或片段。')
        if self.web_search and self.provider == 'searxng' and not self.base_url:
            raise ValueError('请填写 SearXNG 服务地址。')
        return self

    def enabled_ids(self):
        return [name for name in ('web_search', 'web_fetch', 'paper_search') if getattr(self, name)]

    def public(self):
        return {**self.model_dump(exclude={'api_key', 'clear_key'}), 'api_key_set': bool(self.api_key)}
