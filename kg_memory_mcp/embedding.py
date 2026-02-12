"""Ollama embedding 封装"""

import os

import httpx
import numpy as np

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "bge-m3")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Ollama 是本地服务，不走代理
        _client = httpx.AsyncClient(
            base_url=OLLAMA_BASE_URL,
            timeout=30.0,
            transport=httpx.AsyncHTTPTransport(retries=2),
            trust_env=False,  # 不走系统代理，确保数据不出本机
        )
    return _client


async def get_embedding(text: str) -> np.ndarray:
    """获取单条文本的 embedding 向量，返回 numpy array"""
    resp = await _get_client().post(
        "/api/embed",
        json={"model": OLLAMA_EMBED_MODEL, "input": text},
    )
    resp.raise_for_status()
    return np.array(resp.json()["embeddings"][0], dtype=np.float32)


async def get_embeddings(texts: list[str]) -> list[np.ndarray]:
    """批量获取 embedding"""
    if not texts:
        return []
    resp = await _get_client().post(
        "/api/embed",
        json={"model": OLLAMA_EMBED_MODEL, "input": texts},
    )
    resp.raise_for_status()
    return [np.array(e, dtype=np.float32) for e in resp.json()["embeddings"]]


async def close():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
