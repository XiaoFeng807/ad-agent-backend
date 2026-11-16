"""BGE 向量与 TF-IDF 关键词检索，经 RRF 融合。"""
import hashlib
import logging
import os
import re
from functools import lru_cache
from threading import Lock

import chromadb
from chromadb.config import Settings
from sklearn.feature_extraction.text import TfidfVectorizer

from backend.config.config import settings
from backend.rag.chinese_embedding import BGEEmbedding
from backend.rag.rule_reference import rule_query_scope, rule_reference

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "rag_db")
_KEYWORD_PARTS = re.compile(r"[a-z0-9_]+(?:[.#-][a-z0-9_]+)*|[\u3400-\u9fff]+")
_EVIDENCE_FIELDS = ("rule_id", "version", "title", "source")
_OPEN_LOCK = Lock()


def _keyword_tokens(text):
    for part in _KEYWORD_PARTS.findall(text.lower()):
        if part.isascii() or len(part) == 1:
            yield part
        else:
            yield from (part[i:i + 2] for i in range(len(part) - 1))


def _keyword_text(hit):
    metadata = hit["metadata"]
    rule_id, version = metadata.get("rule_id"), metadata.get("version")
    reference = rule_reference(rule_id, version) if isinstance(rule_id, str) and isinstance(version, str) else ""
    return f"{reference} {metadata.get('title', '')} {hit['content']}"


def _hit(chunk_id, content, metadata, distance=None):
    return {"chunk_id": chunk_id, "content": content, "metadata": metadata or {}, "distance": distance}


def _evidence(hit):
    metadata = hit["metadata"]
    return {"chunk_id": hit["chunk_id"], **{key: metadata[key] for key in _EVIDENCE_FIELDS if key in metadata}}


class RAGKnowledge:
    def __init__(self, *, db_path=None, embedding_fn=None, create=True):
        self.embedding_fn = BGEEmbedding() if embedding_fn is None else embedding_fn
        self.client = chromadb.PersistentClient(
            path=str(db_path or settings.RAG_DB_PATH or DB_PATH),
            settings=Settings(anonymized_telemetry=False),
        )
        open_collection = self.client.get_or_create_collection if create else self.client.get_collection
        try:
            self.collection = open_collection(name="ad_knowledge", embedding_function=self.embedding_fn)
        except Exception:
            self.close()
            raise
        self._keyword_lock = Lock()
        self._keyword_ids = set()
        self._keyword_index = None

    def close(self):
        close = getattr(self.client, "close", None)
        if close is not None:
            close()

    def add_text(self, text: str, metadata: dict = None, doc_id: str = None, auto_chunk: bool = False):
        """添加文本到知识库。已有分块 ID 保留首次写入的原文。"""
        if not text or not text.strip():
            return
        text = text.strip()
        doc_id = doc_id or hashlib.md5(text.encode()).hexdigest()[:12]
        self.collection.add(documents=[text], metadatas=[metadata] if metadata else None, ids=[doc_id])

    def _keyword_search(self, query, top_k):
        with self._keyword_lock:
            # Check IDs so external imports and deletions also refresh the index.
            ids = set(self.collection.get(include=[])["ids"])
            if ids != self._keyword_ids:
                data = self.collection.get(include=["documents", "metadatas"])
                hits = [
                    _hit(chunk_id, content, metadata)
                    for chunk_id, content, metadata in zip(data["ids"], data["documents"], data["metadatas"])
                    if content
                ]
                texts = [_keyword_text(hit) for hit in hits]
                self._keyword_index = None
                if any(_KEYWORD_PARTS.search(text.lower()) for text in texts):
                    vectorizer = TfidfVectorizer(analyzer=_keyword_tokens)
                    self._keyword_index = hits, vectorizer, vectorizer.fit_transform(texts)
                self._keyword_ids = set(data["ids"])
            index = self._keyword_index
        if index is None:
            return []
        hits, vectorizer, matrix = index
        # TF-IDF rows are L2 normalized, so the dot product is cosine similarity.
        scores = (matrix @ vectorizer.transform([query]).T).toarray().ravel()
        ranked = sorted((i for i, score in enumerate(scores) if score > 0),
                        key=lambda i: (-scores[i], hits[i]["chunk_id"]))
        return [hits[i] for i in ranked[:top_k]]

    def search(self, query: str, top_k: int = 3):
        """纯向量检索，保留距离供现有调用方使用。"""
        n_results = min(top_k, self.collection.count())
        if n_results <= 0:
            return []
        results = self.collection.query(query_texts=[query], n_results=n_results)
        return [_hit(*row) for row in zip(results["ids"][0], results["documents"][0],
                                          results["metadatas"][0], results["distances"][0])]

    def hybrid_search(self, query: str, top_k: int = 3):
        """融合两路召回，使用 Chroma 分块 ID 累加排名分。"""
        if top_k <= 0:
            return []
        recall_k = max(30, top_k)
        vector_hits = self.search(query, recall_k)
        if not vector_hits:
            return []
        keyword_hits = self._keyword_search(query, recall_k)
        hits, scores = {}, {}
        for channel in (vector_hits, keyword_hits):
            for rank, hit in enumerate(channel, start=1):
                chunk_id = hit["chunk_id"]
                hits.setdefault(chunk_id, hit)
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
        ranked_ids = sorted(hits, key=lambda chunk_id: -scores[chunk_id])
        return [hits[chunk_id] for chunk_id in ranked_ids[:top_k]]

    def recall(self, query: str, recall_k: int = 30):
        """保留因果检索所用的 (content, metadata) 返回格式。"""
        return [(hit["content"], hit["metadata"]) for hit in self.hybrid_search(query, recall_k)]

    def generate(self, query: str, top_k: int = 3):
        """返回检索上下文、来源和可回查的分块证据。"""
        if top_k <= 0:
            return None
        scope = rule_query_scope(query)
        if scope is None:
            docs = self.hybrid_search(query, top_k)
        else:
            rule_ids, version = scope
            result = self.collection.get(
                where={"$and": [{"rule_id": {"$in": list(rule_ids)}}, {"version": version}]},
                limit=top_k,
                include=["documents", "metadatas"],
            )
            docs = [_hit(*row) for row in zip(result["ids"], result["documents"], result["metadatas"])]
        if not docs:
            return None
        return {
            "context": "\n\n---\n\n".join(hit["content"] for hit in docs),
            "sources": [hit["metadata"].get("source", "未知来源") for hit in docs if hit["metadata"]],
            "doc_count": len(docs),
            "evidence": [{**_evidence(hit), "content": hit["content"]} for hit in docs],
        }

    def get_chunk(self, chunk_id: str):
        result = self.collection.get(ids=[chunk_id], include=["documents", "metadatas"])
        if not result["ids"]:
            return None
        hit = _hit(chunk_id, result["documents"][0], result["metadatas"][0])
        return {**_evidence(hit), "content": hit["content"]}


@lru_cache(maxsize=1)
def _open_knowledge(db_path):
    return RAGKnowledge(db_path=db_path)


def get_rag_knowledge():
    with _OPEN_LOCK:
        return _open_knowledge(settings.RAG_DB_PATH or DB_PATH)


# ===== 种子数据 =====
def seed_knowledge(rag: RAGKnowledge):
    """初始化种子知识"""
    seed_data = [
        {"text": "Google Ads 采用按点击付费（CPC）模式，适合搜索意图明确的广告主。关键词匹配类型有：广泛匹配、词组匹配、精确匹配。建议每天至少 50 元预算积累有效数据。",
         "metadata": {"source": "Google Ads 入门指南", "category": "平台知识"}},
        {"text": "Meta Ads（Facebook/Instagram）支持 CPM 或 CPC 付费。优势在于精准的人群定向（年龄、兴趣、行为）。图文广告建议 1080x1080px，视频建议 15 秒以内。",
         "metadata": {"source": "Meta Ads 投放手册", "category": "平台知识"}},
        {"text": "TikTok Ads 适合短视频形式广告，以 CPV 或 CPM 计费。素材建议 9:16 竖屏 15-30 秒。算法推荐机制能让优质素材获得自然流量加成。",
         "metadata": {"source": "TikTok Ads 投放指南", "category": "平台知识"}},
        {"text": "ROAS（广告支出回报率）= 广告带来的收入 ÷ 广告花费。例如花 1000 元带来 5000 元收入，ROAS=5。一般电商 ROAS 3-4 算合格，5 以上优秀。CPA=总花费÷转化数。",
         "metadata": {"source": "广告指标百科", "category": "行业知识"}},
        {"text": "广告投放黄金时间：B2C 电商建议 19:00-22:00 加预算，B2B 工作日上午投放。周末 CPC 通常比工作日低 20-30%。新计划前 3 天为学习期，建议不频繁调价。",
         "metadata": {"source": "广告优化经验", "category": "优化策略"}},
        {"text": "A/B 测试每次只测一个变量（素材/文案/受众/出价四选一），每个版本至少跑 3-5 天、积累 100+ 点击才有统计意义。测试前先确定核心指标（CTR、CVR、ROAS）。",
         "metadata": {"source": "广告测试方法论", "category": "优化策略"}},
    ]
    for item in seed_data:
        rag.add_text(item["text"], item["metadata"])
    logger.info(f"  [RAG] 种子数据加载完成，共 {len(seed_data)} 条")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    rag = RAGKnowledge()
    if rag.collection.count() == 0:
        seed_knowledge(rag)
    while True:
        q = input("\n请输入问题（q退出）: ")
        if q.lower() == "q":
            break
        # 测试 recall
        rec = rag.recall(q, recall_k=5)
        print(f"\n📚 召回 {len(rec)} 条:")
        for i, (c, m) in enumerate(rec[:3]):
            print(f"  [{i+1}] {m.get('source','')}: {c[:60]}...")
        # 测试 search
        res = rag.search(q, top_k=3)
        print(f"\n🔍 搜索 {len(res)} 条:")
        for r in res:
            print(f"  {r['content'][:60]}... (dist={r['distance']:.3f})")
