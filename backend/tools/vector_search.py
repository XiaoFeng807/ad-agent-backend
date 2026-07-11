"""向量检索模块 v2：优先 Chroma，不可用时回退 TF-IDF"""
import os, json

CHROMA_DIR = os.path.join(os.path.dirname(__file__), "..", "chroma_db")
KB_PATH = os.path.join(os.path.dirname(__file__), "..", "knowledge_base", "ad_knowledge.json")

_collection = None
_tfidf_ready = False


def _init_chroma():
    global _collection
    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        try:
            _collection = client.get_collection("ad_knowledge")
            return _collection is not None
        except:
            _collection = client.create_collection("ad_knowledge")
        
        if os.path.exists(KB_PATH):
            with open(KB_PATH, "r", encoding="utf-8") as f:
                items = json.load(f)
            ids = [f"kb_{item['id']}" for item in items]
            docs = [item["content"] for item in items]
            metas = [{"id": item["id"]} for item in items]
            _collection.add(ids=ids, documents=docs, metadatas=metas)
        return True
    except:
        _collection = None
        return False


def _init_tfidf():
    global _tfidf_ready, _vectorizer, _vectors, _items
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        import numpy as np
        if os.path.exists(KB_PATH):
            with open(KB_PATH, "r", encoding="utf-8") as f:
                _items = json.load(f)
            texts = [item["content"] for item in _items]
            _vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(1, 2), max_features=5000)
            _vectors = _vectorizer.fit_transform(texts)
            _tfidf_ready = True
    except:
        pass


def search(query, top_k=3):
    # 1. 优先用 Chroma
    if _collection is None:
        _init_chroma()
    if _collection is not None:
        try:
            results = _collection.query(query_texts=[query], n_results=top_k)
            docs = results.get("documents", [[]])[0]
            dists = results.get("distances", [[]])[0]
            output = [{"content": doc, "score": round(float(1.0 - dists[i] if i < len(dists) else 0.5), 4)} 
                      for i, doc in enumerate(docs)]
            return {"query": query, "results": output, "total": len(output), 
                    "source": "Chroma 语义检索"}
        except:
            pass

    # 2. 回退 TF-IDF
    if not _tfidf_ready:
        _init_tfidf()
    if _tfidf_ready:
        import numpy as np
        q_vec = _vectorizer.transform([query])
        scores = (_vectors @ q_vec.T).toarray().flatten()
        top = np.argsort(scores)[-top_k:][::-1]
        results = [{"content": _items[i]["content"], "score": round(float(scores[i]), 4)} 
                   for i in top if scores[i] > 0]
        return {"query": query, "results": results, "total": len(results), 
                "source": "TF-IDF 语义匹配"}
    
    return {"query": query, "results": [], "total": 0, "source": "none"}
