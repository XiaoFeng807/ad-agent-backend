"""向量检索模块：TF-IDF 向量 + 余弦相似度"""
import os, json, numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

_vectorizer = None
_kb_vectors = None
_kb_data = None

def _load_knowledge():
    global _kb_data, _kb_vectors, _vectorizer
    if _kb_data is not None:
        return _kb_data, _kb_vectors

    kb_path = os.path.join(os.path.dirname(__file__), "..", "knowledge_base", "ad_knowledge.json")
    if not os.path.exists(kb_path):
        return [], None

    with open(kb_path, "r", encoding="utf-8") as f:
        _kb_data = json.load(f)

    texts = [item["content"] for item in _kb_data]
    _vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(1, 2), max_features=5000)
    _kb_vectors = _vectorizer.fit_transform(texts)
    return _kb_data, _kb_vectors

def search(query, top_k=3):
    """向量检索：TF-IDF + 余弦相似度"""
    items, vectors = _load_knowledge()
    if not items or vectors is None:
        return {"query": query, "results": [], "total": 0, "source": "none"}

    query_vec = _vectorizer.transform([query])
    scores = (vectors @ query_vec.T).toarray().flatten()
    top_idx = np.argsort(scores)[-top_k:][::-1]

    results = [{
        "id": items[i]["id"],
        "content": items[i]["content"],
        "score": round(float(scores[i]), 4)
    } for i in top_idx if scores[i] > 0]

    return {
        "query": query,
        "results": results,
        "total": len(results),
        "source": "向量检索（TF-IDF语义匹配）"
    }
