"""
爬取网页内容 → 自动分块 → 存入 RAG 知识库
"""
import sys, os, re, json, hashlib, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 尝试导入依赖
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("请先安装依赖：pip install requests beautifulsoup4")
    sys.exit(1)

from backend.rag.rag_knowledge import RAGKnowledge


def fetch_text(url):
    """抓取网页正文内容"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        # 去掉 script/style 标签
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        # 清理多余空白
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return "\n".join(lines)
    except Exception as e:
        print(f"  ❌ 抓取失败: {e}")
        return None


def chunk_text(text, chunk_size=300, overlap=50):
    """把长文本切成小块"""
    # 先按段落切
    paragraphs = [p.strip() for p in text.split("\n") if p.strip() and len(p.strip()) > 10]
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) < chunk_size:
            current += p + "\n"
        else:
            if current:
                chunks.append(current.strip())
            # 保留部分内容作为重叠
            overlap_text = current[-overlap:] if len(current) > overlap else current
            current = overlap_text + "\n" + p + "\n"
    if current:
        chunks.append(current.strip())
    return chunks


def add_url_to_knowledge(url, category="网页资料", source=None):
    """把一个网页的内容加到知识库"""
    print(f"\n🌐 正在抓取: {url}")
    text = fetch_text(url)
    if not text or len(text) < 50:
        print(f"  ⚠️ 内容太少，跳过")
        return 0

    source = source or url.split("/")[2]  # 用域名当来源
    chunks = chunk_text(text)
    print(f"  📄 共 {len(chunks)} 个片段")

    rag = RAGKnowledge()
    count = 0
    for i, chunk in enumerate(chunks):
        doc_id = hashlib.md5(chunk.encode()).hexdigest()[:12]
        rag.add_text(chunk, {
            "source": source,
            "category": category,
            "url": url
        }, doc_id)
        count += 1

    print(f"  ✅ 已存入 {count} 条知识")
    return count


def add_text_direct(text, source, category="手动输入"):
    """直接添加一段文本到知识库"""
    rag = RAGKnowledge()
    chunks = chunk_text(text)
    count = 0
    for chunk in chunks:
        doc_id = hashlib.md5(chunk.encode()).hexdigest()[:12]
        rag.add_text(chunk, {"source": source, "category": category}, doc_id)
        count += 1
    print(f"  ✅ 已存入 {count} 条知识（来源: {source}）")
    return count


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法：")
        print("  python scrape_knowledge.py url [类别]    # 爬网页")
        print("  python scrape_knowledge.py --text \"内容\" --source \"来源\"  # 直接加文本")
        sys.exit(1)

    if sys.argv[1] == "--text":
        text = sys.argv[2]
        source = sys.argv[4] if len(sys.argv) > 3 else "手动输入"
        add_text_direct(text, source)
    else:
        url = sys.argv[1]
        category = sys.argv[2] if len(sys.argv) > 2 else "网页资料"
        add_url_to_knowledge(url, category)
