# 变更日志

> 本项目所有重要变更均记录在此文件中。

## [22.0.0] - 2026-07-11

### 新增
- **重要性裁剪滑动窗口**：backend/memory/importance.py
  - 纯规则评分（1-10分），不调 LLM
  - 含数字/指标/操作的对话高分，"好的谢谢"低分
  - build_important_window 替代原 build_sliding_window
- **对话记忆向量库 v2**：backend/memory/conversation_memory.py
  - 实体+状态提取：入库时自动抽取出计划名/指标/决策存入 metadata
  - ChromaDB 独立 collection，与知识库分离
  - LLM 按需调用 search_conversation_memory 工具检索历史
  - 遗忘机制：30 天 TTL，每次检索前自动清理过期数据
- **元认知引导**：search_conversation_memory 工具描述含自查提示
- **因果推理层**：backend/rag/causal_reasoning.py
  - 检测因果问题（为什么/原因/导致）
  - 多路展开（原因分析+数据支撑+优化建议）
  - 按因果重要性排序输出
- **中文 Embedding 函数**：backend/rag/chinese_embedding.py
  - 基于 jieba 分词 + 哈希 TF-IDF
  - ChromaDB EmbeddingFunction 协议兼容
  - 无需联网下载模型

### 重构
- **代码结构拆分**：multi_agent.py（36KB/8类 → 15行兼容入口）
  - sub_agent.py / agent_factory.py / competition.py / orchestrator.py
  - pipeline.py / agent_pool.py / tool_registry.py
- **配置统一**：backend/config/config.py 改为 pydantic-settings
  - 所有 os.getenv() 汇总到一处
  - 类型自动校验（PORT= int, DEBUG= bool）

### 修复
- BOM 清理：48 个 .py 文件移除 U+FEFF
- 循环导入：orchestrator ↔ agent_pool 依赖链解开
- database.py 建表不全：补齐 8 张缺失业务表（ad_plans/alerts/…）
- SubAgent 缺少 ToolRegistry import
- AgentPool 缺少 PipelineCoordinator import

## [21.0.0] - 2026-07-10

### 重构
- 根目录清理：20 个散文件 → 2 个入口 + scripts/ + tests/

## [20.0.0] - 2026-07-10

### 新增
- 混合搜索（向量+BM25）：rag_knowledge.py 重写
- 网络知识爬虫：web_knowledge.py
- 上下文管理 v2：context_window.py

[后续版本同现有 CHANGELOG...]
## v23.0.0 (2026-07-11) — BGE Embedding 升级

### 变更
- **重写 chinese_embedding.py**：从 jieba+hash TF-IDF（伪256维）替换为 **BAAI/bge-small-zh-v1.5**（真实512维中文语义模型）
  - sentence-transformers 离线加载，首次运行需联网下载模型
  - query 自动加 BGE 检索前缀，提升检索准确率
  - 全局单例模式，避免重复加载
- **升级 rag_knowledge.py → v7**：
  - ChromaDB 使用 BGEEmbedding 作为正式 embedding 函数
  - 新增 ecall() 方法（向量+BM25+RRF 多路召回），供 causal_reasoning 使用
  - 新增 _rrf_fusion() RRF 融合算法
  - BM25 使用 sklearn TfidfVectorizer，支持中文分词 + bigram
- **修复兼容性**：
  - conversation_memory.py → 改用 BGEEmbedding()
  - data_cleaner.py → 适配新的 search() 返回格式

### 破坏性变更
- ChromaDB 集合维度 256→512，已有数据已清空重建
- ChineseEmbedding(dim=256) 不再可用，改用 BGEEmbedding()

### 效果
- 语义检索质量大幅提升：bge-small-zh-v1.5 在中文语义相似度任务上 SOTA
- BM25 关键词检索补充向量检索的盲区
- RRF 融合两路结果，召回更全面
