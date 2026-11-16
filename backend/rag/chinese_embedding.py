"""中文 Embedding — bge-small-zh-v1.5（BAAI 中文语义模型，512维）"""
import os
from threading import Lock

from chromadb.api.types import EmbeddingFunction
from chromadb.utils.embedding_functions import register_embedding_function

# 按设备复用编码器。
_MODELS = {}
_MODEL_LOCK = Lock()
BGE_REVISION = "7999e1d3359715c523056ef9478215996d62a620"

def _get_model(device=None):
    with _MODEL_LOCK:
        if device not in _MODELS:
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            from sentence_transformers import SentenceTransformer
            _MODELS[device] = SentenceTransformer("BAAI/bge-small-zh-v1.5", revision=BGE_REVISION,
                                                  local_files_only=True, device=device)
        return _MODELS[device]


def _to_list(texts):
    """统一输入为 list"""
    if isinstance(texts, str):
        return [texts]
    return list(texts)


@register_embedding_function
class BGEEmbedding(EmbeddingFunction):
    """
    ChromaDB 兼容中文 Embedding — BAAI/bge-small-zh-v1.5
    - 512 维，已 L2 归一化
    - query 自动加 BGE 检索前缀
    """

    def __init__(self, *, device=None):
        self._dim = 512
        self._device = device

    # ── ChromaDB 协议（input= 关键字参数） ──────────────────

    def __call__(self, input):
        """文档 embedding"""
        texts = _to_list(input)
        model = _get_model() if self._device is None else _get_model(self._device)
        embs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [e.tolist() for e in embs]

    def embed_query(self, input):
        """查询 embedding（加 BGE 检索前缀）"""
        texts = [f"为这个句子生成表示以用于检索相关文章：{text}" for text in _to_list(input)]
        return self(texts)

    def embed_with_retries(self, input):
        return self(input)

    # ── 元信息 ──────────────────────────────────────────────

    @staticmethod
    def name():
        return "bge-small-zh-v1.5"

    @property
    def dim(self):
        return self._dim

    def get_config(self):
        return {"dim": self._dim}

    @staticmethod
    def build_from_config(config):
        return BGEEmbedding()


# ── 别名 ──────────────────────────────────────────────────
ChineseEmbedding = BGEEmbedding

