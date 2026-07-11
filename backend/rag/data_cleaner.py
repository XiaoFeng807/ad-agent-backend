# -*- coding: utf-8 -*-
"""数据清洗模块 — 爬取内容的质量检测 + 过滤 + 清洗"""

import re
import os
import logging
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# ==================== 垃圾内容特征 ====================

GARBAGE_PATTERNS = [
    r"cookie|隐私政策|免责声明|版权[所聲申]|All Rights Reserved",
    r"广告投放\s*\[关闭\]|关[闭掉]\s*×|skip\s*ad",
    r"^第[一二三四五六七八九十\d]+章\s|^目录\s|^前言\s|^参考文献",
    r"tel:\d{8,}|微信号[：:]?\w{6,}|加微信|扫码",
    r"^\s*[◇◆▶▼▲■●]\s*$",  # 纯符号行
    r"^(首页|上一页|下一页|末页|返回|更多>>|>>>)$",
    r"热门推荐|猜你喜欢|相关推荐|为你推荐",
    r"点赞|在看|转发|分享到|收藏",
    r"订阅我们|关注我们|加入我们",
]

GOOD_KEYWORDS = [
    "roas", "roi", "cpc", "cpm", "cpa", "ctr", "cvr",
    "广告", "投放", "竞价", "出价", "预算", "转化", "点击", "展示", "曝光",
    "优化", "策略", "定向", "受众", "人群", "再营销", "类似受众",
    "google ads", "meta ads", "facebook", "instagram", "tiktok ads",
    "关键词", "匹配", "否定词", "质量得分",
    "落地页", "素材", "文案", "cta",
    "数据分析", "a/b测试", "实验",
    "跨境电商", "独立站", "shopify",
    "花费", "收入", "销售额", "利润", "成本", "回报率", "投产比",
]

BAD_KEYWORDS = [
    "整容", "医美", "网贷", "赌博", "彩票",
    "小说连载", "最新章节", "txt下载",
    "招聘", "求职", "兼职",
    "养生", "偏方", "保健品",
]


# ==================== 质量评分 ====================

def score_content(text: str) -> dict:
    """
    对文本内容进行多维质量评分。
    返回: {"score": 0-100, "issues": ["问题1", ...], "pass": bool}
    """
    issues = []
    score = 60  # 基础分

    if not text or len(text.strip()) < 20:
        return {"score": 0, "issues": ["内容过短"], "pass": False}

    # 1. 长度评分
    length = len(text)
    if length < 50:
        issues.append("内容太短")
        score -= 30
    elif length < 200:
        score -= 10
    elif length > 5000:
        score += 5  # 长文加分

    # 2. 句子结构（是否像正常文章）
    sentences = re.split(r"[。！？.!?\n]", text)
    valid_sentences = [s for s in sentences if len(s.strip()) > 5]
    if len(valid_sentences) < 2:
        issues.append("缺乏完整句子结构")
        score -= 20
    elif len(valid_sentences) >= 5:
        score += 5

    # 3. 垃圾内容检测
    garbage_count = 0
    for pattern in GARBAGE_PATTERNS:
        if re.search(pattern, text, re.I):
            garbage_count += 1
            issues.append(f"含垃圾内容特征: {pattern[:20]}")
    score -= garbage_count * 10

    # 4. 相关性检测（广告领域词汇覆盖）
    text_lower = text.lower()
    good_matches = sum(1 for kw in GOOD_KEYWORDS if kw in text_lower)
    bad_matches = sum(1 for kw in BAD_KEYWORDS if kw in text_lower)

    if good_matches >= 8:
        score += 15  # 强相关
    elif good_matches >= 4:
        score += 8
    elif good_matches >= 1:
        score += 3
    else:
        score -= 10
        issues.append("不含广告领域关键词，可能不相关")

    if bad_matches > 0:
        score -= bad_matches * 15
        issues.append("包含不相关内容")

    # 5. 信息密度（数字、百分比等）
    numbers = re.findall(r"\d+\.?\d*%?", text)
    if len(numbers) >= 5:
        score += 10  # 数据丰富
    elif len(numbers) >= 2:
        score += 5

    return {
        "score": max(0, min(100, score)),
        "issues": issues,
        "pass": score >= 40,
        "good_keywords": good_matches,
        "bad_keywords": bad_matches,
    }


# ==================== 内容清洗 ====================

def clean_text(text: str) -> str:
    """清洗文本：去噪、去重、规范化"""
    if not text:
        return ""

    # 1. 去掉垃圾行
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 跳过纯垃圾行
        if re.match(r"^\s*[◇◆▶▼▲■●※☆★○●◎◇□■]+$", line):
            continue
        if re.match(r"^(首页|上一页|下一页|末页|返回|更多>>|>>>|广告|关闭)$", line, re.I):
            continue
        if len(line) < 3:  # 太短的行跳过
            continue
        cleaned.append(line)

    # 2. 去重行（连续重复的行只保留一个）
    deduped = []
    prev = ""
    for line in cleaned:
        if line != prev:
            deduped.append(line)
            prev = line

    # 3. 合并短行
    result = []
    buffer = ""
    for line in deduped:
        if len(line) < 15 and buffer:  # 短行，接到上一句
            buffer += line
        else:
            if buffer:
                result.append(buffer)
            buffer = line
    if buffer:
        result.append(buffer)

    return "\n".join(result)


# ==================== 去重检测 ====================

def is_duplicate(text: str, existing_texts: list, threshold: float = 0.7) -> bool:
    """检测文本是否与已有内容高度重复"""
    if not existing_texts:
        return False
    for existing in existing_texts:
        if not existing:
            continue
        # 快速检查：直接包含
        if text[:100] in existing or existing[:100] in text:
            return True
        # 相似度检查
        ratio = SequenceMatcher(None, text[:200], existing[:200]).ratio()
        if ratio > threshold:
            return True
    return False


# ==================== 来源可信度 ====================

CREDIBLE_SOURCES = {
    "baike.baidu.com": 90,
    "developers.google.com": 95,
    "support.google.com": 95,
    "facebook.com/business": 90,
    "business.facebook.com": 90,
    "ads.tiktok.com": 90,
    "help.ads.tiktok.com": 85,
    "zhuanlan.zhihu.com": 70,
    "10100.com": 75,
    "sohu.com": 50,
    "36kr.com": 65,
    "zhihu.com": 60,
}

SKETCHY_SOURCES = [
    "zhuanlan.zhihu.com/p/",  # 个人专栏
    "blog.csdn.net",
    "jianshu.com",
    "toutiao.com",
]


def score_source(url: str) -> int:
    """评估来源可信度 0-100"""
    if not url:
        return 50

    # 白名单
    for domain, score in CREDIBLE_SOURCES.items():
        if domain in url:
            return score

    # 黑名单降分
    for s in SKETCHY_SOURCES:
        if s in url:
            return 30

    # 默认
    return 50


# ==================== 完整的清洗管道 ====================

class DataCleaner:
    """数据清洗器：整合所有清洗步骤"""

    def __init__(self):
        self.stats = {"total": 0, "passed": 0, "filtered": 0, "issues": []}

    def clean(self, text: str, source_url: str = "", existing_texts: list = None) -> dict:
        """
        完整清洗流程。
        返回: {"text": "清洗后的文本", "quality": {...}, "passed": bool}
        """
        self.stats["total"] += 1

        # 步骤1: 基础清洗
        cleaned = clean_text(text)
        if not cleaned or len(cleaned) < 20:
            self.stats["filtered"] += 1
            return {"text": "", "quality": {"score": 0, "pass": False}, "passed": False}

        # 步骤2: 质量评分
        quality = score_content(cleaned)
        if not quality["pass"]:
            self.stats["filtered"] += 1
            self.stats["issues"].extend(quality["issues"])
            logger.info(f"  [Cleaner] 过滤低质量内容: {quality['issues'][:2]}")
            return {"text": cleaned, "quality": quality, "passed": False}

        # 步骤3: 来源可信度
        source_score = score_source(source_url)
        if source_score < 30:
            logger.info(f"  [Cleaner] 来源可信度低({source_score}): {source_url[:40]}")
            quality["issues"].append(f"来源可信度低({source_score})")

        # 步骤4: 去重检测
        if existing_texts and is_duplicate(cleaned, existing_texts):
            self.stats["filtered"] += 1
            logger.info("  [Cleaner] 重复内容，跳过")
            return {"text": cleaned, "quality": quality, "passed": False, "duplicate": True}

        # 步骤5: 综合调整分数
        quality["source_score"] = source_score
        quality["final_score"] = int(quality["score"] * 0.7 + source_score * 0.3)

        self.stats["passed"] += 1
        return {"text": cleaned, "quality": quality, "passed": True}

    def report(self) -> dict:
        """生成清洗报告"""
        total = self.stats["total"]
        return {
            "total": total,
            "passed": self.stats["passed"],
            "filtered": self.stats["filtered"],
            "pass_rate": f"{self.stats['passed'] / max(total, 1) * 100:.0f}%",
            "common_issues": self.stats["issues"][:10] if self.stats["issues"] else [],
        }

    def reset(self):
        self.stats = {"total": 0, "passed": 0, "filtered": 0, "issues": []}


# ==================== 知识库审计 ====================

def audit_knowledge_base(rag_instance, sample_size=50):
    """审计知识库：扫描已有文档，为每条内容评分"""
    try:
        all_data = rag_instance.collection.get() if hasattr(rag_instance, "collection") else {}
    except:
        all_data = {}
    docs = all_data.get("documents", [])
    metas = all_data.get("metadatas", [])

    if not docs:
        return {"total": 0, "message": "知识库为空"}

    total = len(docs)
    import random
    indices = list(range(total))
    random.shuffle(indices)
    to_check = indices[:min(sample_size, total)]

    high_conf = 0
    low_conf = 0
    results = []

    for idx in to_check:
        doc = docs[idx]
        meta = metas[idx] if idx < len(metas) else {}

        quality = score_content(doc)
        source = meta.get("source", "")
        source_score_val = score_source(source)

        # 共识度
        consensus = 1.0
        if total >= 3:
            try:
                v = verify_by_consensus(doc, rag_instance)
                consensus = v.get("consensus", 1.0)
            except:
                consensus = 1.0

        confidence = int(quality["score"] * 0.3 + source_score_val * 0.2 + consensus * 100 * 0.5)
        confidence = max(0, min(100, confidence))

        if confidence >= 60:
            high_conf += 1
        else:
            low_conf += 1

        results.append({
            "id": str(idx),
            "content": doc[:80],
            "source": source,
            "quality_score": quality["score"],
            "source_score": source_score_val,
            "consensus": round(consensus, 2),
            "confidence": confidence,
        })

    low_conf_docs = [r for r in results if r["confidence"] < 60]

    avg_q = sum(r["quality_score"] for r in results) / max(len(results), 1)
    avg_c = sum(r["consensus"] for r in results) / max(len(results), 1)
    avg_conf = sum(r["confidence"] for r in results) / max(len(results), 1)

    return {
        "total": total,
        "scored": len(to_check),
        "high_confidence": high_conf,
        "low_confidence": low_conf,
        "low_conf_samples": low_conf_docs[:5],
        "stats": {
            "avg_quality": round(avg_q, 1),
            "avg_consensus": round(avg_c, 2),
            "avg_confidence": round(avg_conf, 1),
        },
        "suggestion": "建议审查低置信度文档" if low_conf > 0 else "知识库整体质量良好",
    }


def clean_knowledge_base(rag_instance, threshold=40, dry_run=True):
    """清理知识库：删除置信度低于阈值的文档"""
    try:
        all_data = rag_instance.collection.get() if hasattr(rag_instance, "collection") else {}
    except:
        all_data = {}
    docs = all_data.get("documents", [])
    metas = all_data.get("metadatas", [])
    ids = all_data.get("ids", [])

    if not docs:
        return {"deleted": 0, "message": "知识库为空"}

    to_delete = []
    for idx, doc in enumerate(docs):
        meta = metas[idx] if idx < len(metas) else {}
        quality = score_content(doc)
        source = meta.get("source", "")
        source_score_val = score_source(source)
        confidence = int(quality["score"] * 0.6 + source_score_val * 0.4)

        if confidence < threshold:
            to_delete.append({
                "id": ids[idx] if idx < len(ids) else str(idx),
                "content": doc[:60],
                "confidence": confidence,
                "reason": "quality={}, source={}".format(quality["score"], source_score_val),
            })

    if dry_run:
        return {
            "dry_run": True,
            "would_delete": len(to_delete),
            "samples": to_delete[:5],
            "total_docs": len(docs),
            "message": "预览模式：将删除{}条低质量文档".format(len(to_delete)),
        }

    delete_ids = [d["id"] for d in to_delete]
    if delete_ids:
        rag_instance.collection.delete(ids=delete_ids)
        rag_instance._bm25 = None

    return {
        "dry_run": False,
        "deleted": len(to_delete),
        "total_docs_before": len(docs),
        "total_docs_after": len(docs) - len(to_delete),
        "message": "已删除{}条低质量文档".format(len(to_delete)),
    }


# ==================== 多源交叉验证 ====================

import requests as _requests
from dotenv import load_dotenv as _load_dotenv
from backend.config.config import settings

_LLM_API_KEY = None
_LLM_BASE_URL = None
_LLM_MODEL = None


def _ensure_llm_config():
    global _LLM_API_KEY, _LLM_BASE_URL, _LLM_MODEL
    if _LLM_API_KEY is None:
        _load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
        _LLM_API_KEY = settings.API_KEY
        _LLM_BASE_URL = settings.BASE_URL
        _LLM_MODEL = settings.MODEL


def extract_claims(text: str) -> list:
    """从文本中提取关键陈述/断言，每句一个独立claim"""
    claims = []
    sentences = re.split(r"[。！？.!?\n]", text)
    for s in sentences:
        s = s.strip()
        # 只保留含具体信息的句子（有数字、有断言词、有专业术语）
        if len(s) < 10:
            continue
        has_info = bool(re.search(r"[\d]+", s))  # 含数字
        has_ad_word = any(kw in s for kw in GOOD_KEYWORDS)  # 含广告领域词
        if has_info or has_ad_word:
            claims.append(s)
    return claims[:5]  # 最多5条


def llm_compare_claims(claim_a: str, claim_b: str) -> str:
    """让LLM判断两条陈述是支持、矛盾还是不相关"""
    _ensure_llm_config()
    if not _LLM_API_KEY:
        return "unknown"
    try:
        resp = _requests.post(
            _LLM_BASE_URL + "/chat/completions",
            json={
                "model": _LLM_MODEL,
                "messages": [
                    {"role": "system", "content": "判断两条陈述的关系。只输出一个词：support / contradict / unrelated / same"},
                    {"role": "user", "content": f"陈述A：{claim_a}\n\n陈述B：{claim_b}"}
                ],
                "temperature": 0.1,
                "max_tokens": 20
            },
            headers={"Authorization": "Bearer " + _LLM_API_KEY},
            timeout=10
        )
        if resp.status_code != 200:
            return "unknown"
        reply = resp.json()["choices"][0]["message"]["content"].strip().lower()
        for tag in ["support", "contradict", "unrelated", "same"]:
            if tag in reply:
                return tag
        return "unknown"
    except Exception as e:
        logger.warning(f"[CrossValidate] LLM compare failed: {e}")
        return "unknown"


def verify_by_consensus(text: str, rag_instance, threshold: float = 0.6) -> dict:
    """
    多源交叉验证：提取文本中的关键断言，和知识库已有内容比对。
    
    返回: {
        "passed": bool,
        "consensus": float,  # 一致率(0-1)
        "details": [{"claim": "...", "support": 3, "oppose": 1, "consensus": 0.75}, ...]
    }
    """
    claims = extract_claims(text)
    if not claims:
        return {"passed": True, "consensus": 1.0, "details": [], "reason": "no_claims_to_verify"}
    
    doc_count = rag_instance.collection.count() if rag_instance and hasattr(rag_instance, "collection") else 0
    if doc_count < 3:
        # 知识库内容太少，无法交叉验证，直接放行
        return {"passed": True, "consensus": 1.0, "details": [], "reason": "insufficient_docs_for_cross_validation"}
    
    details = []
    total_consensus = 0
    
    for claim in claims:
        related = rag_instance.search(claim, top_k=8)
        supports = 0
        opposes = 0
        unrelated = 0
        
        for item in related:
            content = item['content']
            meta = item['metadata']
            score = item.get('distance', 0)
            rel = llm_compare_claims(claim, content[:300])
            if rel == "support" or rel == "same":
                supports += 1
            elif rel == "contradict":
                opposes += 1
            else:
                unrelated += 1
        
        total = supports + opposes
        # 如果所有都比较都返回unknown（如LLM不可用），默认通过
        if total == 0 and unrelated > 0:
            c = 1.0  # 无法验证时默认可信
        else:
            c = supports / max(total, 1)
        # 如果反对票多但相关文档本身置信度低，降低权重
        # （避免"用脏数据否决好数据"）
        total_consensus += c
        
        details.append({
            "claim": claim[:50],
            "support": supports,
            "oppose": opposes,
            "unrelated": unrelated,
            "consensus": round(c, 2)
        })
    
    avg_consensus = total_consensus / len(claims)
    
    return {
        "passed": avg_consensus >= threshold,
        "consensus": round(avg_consensus, 2),
        "details": details,
        "reason": "cross_validation_" + ("passed" if avg_consensus >= threshold else "failed"),
    }


# ==================== 集成到 RAG 的便捷函数 ====================

_cleaner = None


def get_cleaner():
    global _cleaner
    if _cleaner is None:
        _cleaner = DataCleaner()
    return _cleaner


def clean_and_add(rag_instance, text: str, metadata: dict = None, existing_texts: list = None, cross_validate: bool = True):
    """清洗文本后存入 RAG（一步到位）。cross_validate=True时会和知识库已有内容交叉验证"""
    cleaner = get_cleaner()
    source_url = (metadata or {}).get("source", "")
    result = cleaner.clean(text, source_url, existing_texts)

    if not result["passed"]:
        logger.info(f"  [Cleaner] 跳过低质量内容: {text[:50]}...")
        return False

    # 多源交叉验证
    if cross_validate and rag_instance:
        v_result = verify_by_consensus(result["text"], rag_instance)
        if not v_result["passed"]:
            logger.info(f"  [CrossValidate] 交叉验证失败: consensus={v_result['consensus']}, 可能含与现有知识矛盾的内容")
            return False
        if v_result.get("details"):
            logger.info(f"  [CrossValidate] 一致率: {v_result['consensus']}, 验证了{len(v_result['details'])}条断言")

    rag_instance.add_text(
        result["text"],
        metadata=metadata or {},
        auto_chunk=True
    )
    return True
