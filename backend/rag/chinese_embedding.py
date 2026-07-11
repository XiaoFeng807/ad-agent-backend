"""中文 Embedding — bge-small-zh-v1.5（BAAI 中文语义模型，512维）"""
import os
import numpy as np
from chromadb.api.types import EmbeddingFunction

# 全局单例
_MODEL = None

def _get_model():
    global _MODEL
    if _MODEL is None:
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer("BAAI/bge-small-zh-v1.5")
    return _MODEL


def _to_list(texts):
    """统一输入为 list"""
    if isinstance(texts, str):
        return [texts]
    return list(texts)


class BGEEmbedding(EmbeddingFunction):
    """
    ChromaDB 兼容中文 Embedding — BAAI/bge-small-zh-v1.5
    - 512 维, COSINE 距离, 已 L2 归一化
    - query 自动加 BGE 检索前缀
    """

    def __init__(self):
        self._dim = 512
        self._name = "bge-small-zh-v1.5"
        self._model = _get_model()

    # ── ChromaDB 协议（input= 关键字参数） ──────────────────

    def __call__(self, input):
        """文档 embedding"""
        texts = _to_list(input)
        embs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [e.tolist() for e in embs]

    def embed_query(self, input):
        """查询 embedding（加 BGE 检索前缀）"""
        prefixed = f"为这个句子生成表示以用于检索相关文章：{input}"
        emb = self._model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False)
        return [emb.tolist()]

    def embed_with_retries(self, input):
        return self(input)

    # ── 元信息 ──────────────────────────────────────────────

    def name(self):
        return self._name

    @property
    def dim(self):
        return self._dim

    def get_config(self):
        return {"dim": self._dim}

    def build_from_config(self, config):
        return self

    def validate_config(self, config):
        pass

    def validate_config_update(self, old_config, new_config):
        pass


# ── 别名 ──────────────────────────────────────────────────
ChineseEmbedding = BGEEmbedding

