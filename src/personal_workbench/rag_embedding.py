"""OpenAI 兼容 Embedding 适配器；也适用于 Ollama 的 /v1 接口。"""

import math

import httpx
from llama_index.core.embeddings import BaseEmbedding
from pydantic import PrivateAttr


class EndpointEmbedding(BaseEmbedding):
    # endpoint 与凭据使用私有字段，LlamaIndex 序列化索引时不会带上它们。
    _endpoint: str = PrivateAttr()
    _key: str = PrivateAttr()

    def __init__(self, config):
        if not config["base_url"] or not config["model"]:
            raise ValueError("请在知识库设置中填写 Embedding Base URL 和模型名称。")
        super().__init__(model_name=config["model"], embed_batch_size=16)
        self._endpoint = config["base_url"].rstrip("/") + "/embeddings"
        self._key = config.get("api_key", "")

    def _vectors(self, texts):
        try:
            headers = {"Authorization": "Bearer " + self._key} if self._key else {}
            with httpx.Client(timeout=90, follow_redirects=False) as client:
                response = client.post(self._endpoint, headers=headers, json={
                    "model": self.model_name, "input": texts, "encoding_format": "float",
                })
                if response.status_code != 200:
                    raise ValueError(f"Embedding 请求失败（HTTP {response.status_code}），请检查地址、模型和密钥。")
                rows = sorted(response.json()["data"], key=lambda row: row["index"])
            if [r["index"] for r in rows] != list(range(len(texts))):
                raise ValueError("Embedding 服务返回的向量数量或顺序无效。")
            vectors = []
            for row in rows:
                vector = [float(x) for x in row["embedding"]]
                norm = math.sqrt(sum(x*x for x in vector))
                if not vector or not math.isfinite(norm) or norm == 0:
                    raise ValueError("Embedding 服务返回了无效向量。")
                vectors.append([x / norm for x in vector])
            if len({len(v) for v in vectors}) != 1:
                raise ValueError("Embedding 向量维度不一致。")
            return vectors
        except httpx.HTTPError:
            raise ValueError("无法连接 Embedding 服务，请检查 Base URL 和网络。") from None
        except (KeyError, TypeError):
            raise ValueError("Embedding 服务响应格式不兼容。") from None

    def _get_text_embedding(self, text):
        return self._vectors([text])[0]

    def _get_text_embeddings(self, texts):
        return self._vectors(texts)

    def _get_query_embedding(self, query):
        return self._vectors([query])[0]

    async def _aget_query_embedding(self, query):
        import asyncio
        return await asyncio.to_thread(self._get_query_embedding, query)
