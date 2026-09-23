"""知识仓库的公开配置。密钥从不写入会话或返回给浏览器。"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retrieval_profile: Literal["hybrid", "vector"] = "hybrid"
    top_k: int = Field(default=6, ge=1, le=50)
    vector_top_k_multiplier: int = Field(default=2, ge=1, le=10)
    bm25_top_k_multiplier: int = Field(default=2, ge=1, le=10)
    vector_index_type: Literal["flat", "hnsw"] = "flat"
    hnsw_m: int = Field(default=32, ge=4, le=128)
    hnsw_ef_construction: int = Field(default=200, ge=8, le=1000)
    hnsw_ef_search: int = Field(default=64, ge=1, le=1000)
    chunk_size: int = Field(default=512, ge=64, le=4096)
    chunk_overlap: int = Field(default=64, ge=0, le=1024)
    reranker_model: str = Field(default="", max_length=200)
    rerank_top_k: int = Field(default=30, ge=1, le=100)

    @model_validator(mode="after")
    def geometry(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("分块重叠必须小于分块大小。")
        return self

    def build_config(self):
        return {k: getattr(self, k) for k in (
            "chunk_size", "chunk_overlap", "vector_index_type", "hnsw_m", "hnsw_ef_construction"
        )}


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    api_key: str = Field(default="", max_length=4096, repr=False)
    clear_key: bool = False

    @model_validator(mode="after")
    def endpoint(self):
        self.base_url = self.base_url.strip().rstrip("/")
        self.model = self.model.strip()
        if self.base_url:
            url = urlsplit(self.base_url)
            if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                    or url.password or url.query or url.fragment):
                raise ValueError("Base URL 必须是 http(s) 地址，不能含密钥、查询参数或用户名。")
            if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("远程 Embedding 服务须使用 HTTPS；本机服务可使用 HTTP。")
        return self


class KnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    engine: Literal["llamaindex", "pageindex"] = "llamaindex"
    use_default_embedding: bool = False
    pageindex_mode: Literal["flash", "standard"] = "flash"

    @model_validator(mode="after")
    def title(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("请填写知识库名称。")
        return self
