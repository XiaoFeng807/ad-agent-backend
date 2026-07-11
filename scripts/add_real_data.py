import sys
sys.path.insert(0, "E:\\codex++\\文件\\ad_agent_backend")
from backend.rag.rag_knowledge import RAGKnowledge

rag = RAGKnowledge()

# 添加多条真实广告知识
new_data = [
    ("Google Ads 质量得分是 Google 对广告质量和相关性的评分，满分 10 分。由 3 个因素决定：预期点击率、广告相关性、着陆页体验。得分越高，CPC 越低，排名越高。", "Google Ads 官方文档"),
    ("Facebook 广告受众定向方式：核心受众（年龄/性别/地区/兴趣）、自定义受众（上传客户列表）、类似受众（基于现有客户找相似人群）。建议三种组合使用效果最佳。", "Meta Ads 官方指南"),
    ("抖音信息流广告规格：视频时长 15-60 秒，建议 15-30 秒。分辨率 1080x1920（9:16 竖屏）。文案不超过 100 字。支持行动按钮（立即购买/了解更多/下载应用）。", "巨量引擎官方文档"),
    ("电商广告 ROAS 优化四步法：①优化受众定向减少浪费 ②优化素材提升点击率 ③优化落地页提升转化率 ④调整出价策略控制成本。每步可提升 ROAS 10-30%。", "电商广告优化实战"),
    ("Google Ads 再营销（Remarketing）：向访问过你网站但未转化的用户展示广告。设置方法：安装 Google 跟踪代码 → 创建受众名单 → 设置再营销广告系列。再营销转化率通常比普通广告高 3-5 倍。", "Google Ads 官方文档"),
    ("预算分配策略：70-20-10 法则。70% 预算给已验证效果好的人群和素材，20% 给有潜力的新组合测试，10% 给创新的全新尝试。此策略平衡稳定性和增长。", "广告优化经验"),
]

for text, source in new_data:
    rag.add_text(text, {"source": source, "category": "行业知识"})

print("添加完成，当前文档数:", rag.collection.count())
