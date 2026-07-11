"""RAG 知识库 v7 — BGE Embedding + BM25 + RRF 混合检索"""
import os
import re
import json
import hashlib
import logging
import chromadb
from chromadb.config import Settings
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

from backend.rag.chinese_embedding import BGEEmbedding

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "rag_db")


class RAGKnowledge:
    """RAG 知识库 v7 — 混合检索（密集向量 + BM25 关键词 + RRF 融合）"""

    def __init__(self):
        self.embedding_fn = BGEEmbedding()
        self.client = chromadb.PersistentClient(
            path=DB_PATH,
            settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name="ad_knowledge",
            embedding_function=self.embedding_fn
        )
        # BM25 后备（使用 sklearn TfidfVectorizer）
        self._bm25_index = None
        self._bm25_vectorizer = None
        self._bm25_docs = []
        self._bm25_metas = []
        logger.info(f"  [RAG] 知识库已加载，文档数: {self.collection.count()}")

    # ── 索引维护 ───────────────────────────────────────────

    def _rebuild_bm25(self):
        """从 ChromaDB 重建 BM25 索引"""
        if self.collection.count() == 0:
            self._bm25_index = None
            self._bm25_vectorizer = None
            self._bm25_docs = []
            self._bm25_metas = []
            return
        all_data = self.collection.get(include=["documents", "metadatas"])
        self._bm25_docs = all_data["documents"] or []
        self._bm25_metas = all_data["metadatas"] or []
        if self._bm25_docs:
            self._bm25_vectorizer = TfidfVectorizer(
                analyzer="word",
                token_pattern=r"(?u)\b\w+\b",
                max_features=5000,
                ngram_range=(1, 2),
            )
            self._bm25_index = self._bm25_vectorizer.fit_transform(self._bm25_docs)

    # ── 写入 ─────────────────────────────────────────────────

    def add_text(self, text: str, metadata: dict = None, doc_id: str = None, auto_chunk: bool = False):
        """添加文本到知识库"""
        if not text or not text.strip():
            return
        text = text.strip()
        doc_id = doc_id or hashlib.md5(text.encode()).hexdigest()[:12]
        metadata = metadata or {}
        self.collection.add(documents=[text], metadatas=[metadata], ids=[doc_id])
        # 增量更新 BM25
        if self._bm25_vectorizer is not None:
            self._bm25_docs.append(text)
            self._bm25_metas.append(metadata)
            # 重新 fit（低开销，数据量不大时可行）
            self._bm25_index = self._bm25_vectorizer.fit_transform(self._bm25_docs)
        else:
            self._rebuild_bm25()

    # ── 检索 ─────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 3):
        """
        向量检索（供外部调用）
        返回: [{"content": ..., "metadata": ..., "distance": ...}, ...]
        """
        if self.collection.count() == 0:
            return []
        results = self.collection.query(
            query_texts=[query],
            n_results=min(top_k, self.collection.count())
        )
        docs = []
        if results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                docs.append({
                    "content": doc,
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "distance": results["distances"][0][i] if results["distances"] else None
                })
        return docs

    def recall(self, query: str, recall_k: int = 30):
        """
        多路召回：向量检索 + BM25 + RRF 融合
        返回: [(content, metadata), ...]  供 causal_reasoning 使用
        """
        if self.collection.count() == 0:
            return []

        # 1. 向量检索
        vec_results = self.collection.query(
            query_texts=[query],
            n_results=min(recall_k, self.collection.count())
        )
        vec_hits = []
        if vec_results["documents"] and vec_results["documents"][0]:
            for i, doc in enumerate(vec_results["documents"][0]):
                meta = vec_results["metadatas"][0][i] if vec_results["metadatas"] else {}
                vec_hits.append((doc, meta))

        # 2. BM25 检索
        bm25_hits = []
        if self._bm25_index is not None and self._bm25_docs:
            q_vec = self._bm25_vectorizer.transform([query])
            sims = cosine_similarity(q_vec, self._bm25_index).flatten()
            top_indices = sims.argsort()[::-1][:recall_k]
            for idx in top_indices:
                if sims[idx] > 0:
                    bm25_hits.append((self._bm25_docs[idx], self._bm25_metas[idx] if idx < len(self._bm25_metas) else {}))

        # 3. RRF 融合
        return self._rrf_fusion(vec_hits, bm25_hits, k=60, top_n=recall_k)

    def _rrf_fusion(self, list_a, list_b, k=60, top_n=30):
        """Reciprocal Rank Fusion: 融合两路召回结果"""
        scores = {}
        for rank, (content, meta) in enumerate(list_a):
            scores[id(content)] = {"content": content, "meta": meta, "score": 1.0 / (k + rank + 1)}
        for rank, (content, meta) in enumerate(list_b):
            cid = id(content)
            if cid in scores:
                scores[cid]["score"] += 1.0 / (k + rank + 1)
            else:
                scores[cid] = {"content": content, "meta": meta, "score": 1.0 / (k + rank + 1)}
        sorted_items = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
        return [(item["content"], item["meta"]) for item in sorted_items[:top_n]]

    def generate(self, query: str, top_k: int = 3):
        """检索 → 拼接上下文（供 Agent 调 LLM 用）"""
        docs = self.search(query, top_k)
        if not docs:
            return None
        context = "\n\n---\n\n".join(d["content"] for d in docs)
        return {
            "context": context,
            "sources": [d["metadata"].get("source", "未知来源") for d in docs if d.get("metadata")],
            "doc_count": len(docs)
        }


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
