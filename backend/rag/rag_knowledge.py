"""
RAG 知识库模块
功能：文档 → 分块 → 向量化 → 存储 → 检索 → 生成回答
"""
import os
import json
import hashlib
import chromadb
from chromadb.config import Settings

# ===== 知识库路径 =====
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "rag_db")


class RAGKnowledge:
    """RAG 知识库：存文档、搜文档、基于文档回答问题"""

    def __init__(self):
        # 1. 初始化 ChromaDB 客户端（持久化存储）
        self.client = chromadb.PersistentClient(
            path=DB_PATH,
            settings=Settings(anonymized_telemetry=False)
        )
        # 2. 创建或获取集合（类似数据库的表）
        self.collection = self.client.get_or_create_collection(
            name="ad_knowledge"
        )
        print(f"  [RAG] 知识库已加载，当前文档数: {self.collection.count()}")

    def add_text(self, text: str, metadata: dict = None, doc_id: str = None):
        """
        添加一段文本到知识库
        - text: 要存入的文本内容
        - metadata: 附加信息（如来源、分类）
        - doc_id: 文档ID（不传则自动生成）
        """
        if not text or not text.strip():
            return

        doc_id = doc_id or hashlib.md5(text.encode()).hexdigest()[:12]
        metadata = metadata or {}

        # ChromaDB 会自动做 embedding，我们只管传文本
        self.collection.add(
            documents=[text],
            metadatas=[metadata],
            ids=[doc_id]
        )

    def search(self, query: str, top_k: int = 3):
        """
        搜索最相关的文档片段
        - query: 用户的问题
        - top_k: 返回几条最相关的结果
        """
        if self.collection.count() == 0:
            return []

        results = self.collection.query(
            query_texts=[query],
            n_results=min(top_k, self.collection.count())
        )

        # 整理返回结果
        docs = []
        if results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                docs.append({
                    "content": doc,
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "distance": results["distances"][0][i] if results["distances"] else None
                })
        return docs

    def generate(self, query: str, top_k: int = 3):
        """
        检索 + 生成：搜到相关文档，拼接成提示词返回（让 Agent 去调 LLM）
        """
        docs = self.search(query, top_k)
        if not docs:
            return None  # 知识库没有相关内容

        # 把检索到的文档拼成上下文
        context = "\n\n---\n\n".join([d["content"] for d in docs])
        return {
            "context": context,
            "sources": [d["metadata"].get("source", "未知来源") for d in docs if d.get("metadata")],
            "doc_count": len(docs)
        }


# ===== 初始化一些种子知识（广告投放相关） =====
def seed_knowledge(rag: RAGKnowledge):
    """往知识库里塞一些初始资料"""
    seed_data = [
        {
            "text": "Google Ads 采用按点击付费（CPC）模式，适合搜索意图明确的广告主。"
                      "关键词匹配类型有：广泛匹配、词组匹配、精确匹配。"
                      "建议每天至少 50 元预算才能积累有效数据。",
            "metadata": {"source": "Google Ads 入门指南", "category": "平台知识"}
        },
        {
            "text": "Meta Ads（Facebook/Instagram）支持按展示（CPM）或按点击（CPC）付费。"
                      "优势在于精准的人群定向，可以按年龄、兴趣、行为等维度投放。"
                      "图文广告建议尺寸 1080x1080px，视频建议 15 秒以内。",
            "metadata": {"source": "Meta Ads 投放手册", "category": "平台知识"}
        },
        {
            "text": "TikTok Ads 适合短视频形式的广告，以 CPV（按播放）或 CPM 计费。"
                      "广告素材建议 9:16 竖屏，时长 15-30 秒。"
                      "TikTok 的算法推荐机制能让优质内容获得自然流量加成。",
            "metadata": {"source": "TikTok Ads 投放指南", "category": "平台知识"}
        },
        {
            "text": "ROAS（广告支出回报率）= 广告带来的收入 ÷ 广告花费。"
                      "例如：花了 1000 元，带来 5000 元收入，ROAS = 5。"
                      "一般电商行业 ROAS 达到 3-4 算合格，5 以上算优秀。"
                      "CPA（单次获客成本）= 总花费 ÷ 转化数。",
            "metadata": {"source": "广告指标百科", "category": "行业知识"}
        },
        {
            "text": "广告投放的黄金时间：B2C 电商建议 19:00-22:00 加大预算，"
                      "B2B 行业建议工作日上午投放。周末 CPC 通常比工作日低 20-30%。"
                      "新广告计划前 3 天为学习期，建议不要频繁调整出价。",
            "metadata": {"source": "广告优化经验", "category": "优化策略"}
        },
        {
            "text": "A/B 测试是广告优化的核心方法。每次只测试一个变量："
                      "比如素材、文案、受众、出价四选一。"
                      "每个版本至少跑 3-5 天、积累 100+ 点击才有统计意义。"
                      "测试前先确定核心指标（CTR、CVR、ROAS）。",
            "metadata": {"source": "广告测试方法论", "category": "优化策略"}
        }
    ]

    for item in seed_data:
        rag.add_text(item["text"], item["metadata"])
    print(f"  [RAG] 种子数据加载完成，共 {len(seed_data)} 条知识")


# ===== 快速测试 =====
if __name__ == "__main__":
    rag = RAGKnowledge()
    if rag.collection.count() == 0:
        seed_knowledge(rag)

    # 测试检索
    while True:
        q = input("\\n请输入问题（输入 q 退出）: ")
        if q.lower() == "q":
            break
        result = rag.generate(q)
        if result:
            print(f"\\n📚 找到 {result['doc_count']} 条相关文档：")
            print(f"📄 来源：{', '.join(result['sources'])}")
            print(f"\\n📝 参考内容：\\n{result['context'][:500]}...")
        else:
            print("\\n❌ 知识库中没有相关内容")
