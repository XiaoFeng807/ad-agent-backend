# ===== 知识库 API =====

@app.get("/api/knowledge/stats")
async def knowledge_stats():
    """知识库统计"""
    from backend.rag.rag_knowledge import RAGKnowledge
    try:
        rag = RAGKnowledge()
        data = rag.collection.get()
        docs = data["documents"] if data and data["documents"] else []
        metas = data["metadatas"] if data and data["metadatas"] else []
        sources = set()
        categories = set()
        for m in metas:
            if m and m.get("source"): sources.add(m["source"])
            if m and m.get("category"): categories.add(m["category"])
        return {"total": len(docs), "sources": len(sources), "categories": len(categories)}
    except Exception as e:
        return {"total": 0, "sources": 0, "categories": 0, "error": str(e)}


@app.get("/api/knowledge/all")
async def knowledge_all():
    """获取所有知识"""
    from backend.rag.rag_knowledge import RAGKnowledge
    try:
        rag = RAGKnowledge()
        data = rag.collection.get()
        items = []
        if data and data["documents"]:
            for i, doc in enumerate(data["documents"]):
                meta = data["metadatas"][i] if data["metadatas"] and i < len(data["metadatas"]) else {}
                items.append({
                    "text": doc[:300] + ("..." if len(doc) > 300 else ""),
                    "source": meta.get("source", "未知"),
                    "category": meta.get("category", "未分类")
                })
        return {"items": items}
    except Exception as e:
        return {"items": [], "error": str(e)}


@app.get("/api/knowledge/search")
async def knowledge_search(q: str = ""):
    """搜索知识库"""
    if not q:
        return {"results": [], "sources": []}
    from backend.rag.rag_knowledge import RAGKnowledge
    try:
        rag = RAGKnowledge()
        result = rag.generate(q, top_k=5)
        if result and result.get("context"):
            blocks = result["context"].split("\n\n---\n\n")
            return {"results": blocks, "sources": result.get("sources", [])}
        return {"results": [], "sources": []}
    except Exception as e:
        return {"results": [], "sources": [], "error": str(e)}


@app.get("/knowledge")
async def knowledge_page():
    """知识库页面"""
    kb_path = os.path.join(static_dir, "knowledge_base.html")
    if os.path.exists(kb_path):
        return FileResponse(kb_path)
    return {"msg": "知识库页面未找到"}
