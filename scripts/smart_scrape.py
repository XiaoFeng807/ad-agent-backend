import sys, os, re, hashlib, time
sys.path.insert(0, "E:\\codex++\\文件\\ad_agent_backend")

# 只用标准库和requests
try:
    import requests
except:
    print("先装依赖: pip install requests")
    sys.exit(1)

from backend.rag.rag_knowledge import RAGKnowledge

def smart_fetch(url):
    """加强版爬虫，带更多伪装"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.baidu.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.encoding = "utf-8"
        # 用正则提取正文（百度百科的文字在 meta description 和 class="para" 里）
        text = resp.text
        
        # 方法1：找 meta description
        m = re.search(r'<meta[^>]*name="description"[^>]*content="([^"]+)"', text)
        if m:
            return m.group(1)
        
        # 方法2：找 class="para" 段落
        paras = re.findall(r'<div[^>]*class="para"[^>]*>(.*?)</div>', text, re.DOTALL)
        if paras:
            import html
            clean = []
            for p in paras:
                p = re.sub(r'<[^>]+>', '', p)  # 去标签
                p = html.unescape(p)
                p = p.strip()
                if len(p) > 20:
                    clean.append(p)
            return "\n".join(clean)
        
        # 方法3：直接拿所有文本
        from html.parser import HTMLParser
        class TextParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.texts = []
                self.skip = False
            def handle_starttag(self, tag, attrs):
                if tag in ("script","style","nav","footer"):
                    self.skip = True
            def handle_endtag(self, tag):
                if tag in ("script","style","nav","footer"):
                    self.skip = False
            def handle_data(self, data):
                if not self.skip:
                    t = data.strip()
                    if t and len(t) > 15:
                        self.texts.append(t)
        p = TextParser()
        p.feed(text)
        return "\n".join(p.texts[:30]) if p.texts else None
    except Exception as e:
        print(f"  抓取失败: {e}")
        return None

def chunk_text(text, chunk_size=300, overlap=50):
    paragraphs = [p.strip() for p in text.split("\n") if p.strip() and len(p.strip()) > 10]
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) < chunk_size:
            current += p + "\n"
        else:
            if current:
                chunks.append(current.strip())
            overlap_text = current[-overlap:] if len(current) > overlap else current
            current = overlap_text + "\n" + p + "\n"
    if current:
        chunks.append(current.strip())
    return chunks

# ===== 要爬的 URL 列表 =====
urls = [
    ("https://baike.baidu.com/item/ROAS", "广告指标"),
    ("https://baike.baidu.com/item/点击率", "广告指标"),
    ("https://baike.baidu.com/item/转化率", "广告指标"),
    ("https://baike.baidu.com/item/信息流广告", "平台知识"),
    ("https://baike.baidu.com/item/搜索引擎营销", "平台知识"),
    ("https://baike.baidu.com/item/谷歌广告", "平台知识"),
    ("https://baike.baidu.com/item/抖音广告", "平台知识"),
]

rag = RAGKnowledge()
total = 0

for url, category in urls:
    print(f"\n🌐 正在抓取: {url}")
    text = smart_fetch(url)
    if not text or len(text) < 30:
        print(f"  ⚠️ 内容太少，跳过")
        continue
    
    chunks = chunk_text(text)
    print(f"  📄 共 {len(chunks)} 个片段")
    
    source = url.replace("https://baike.baidu.com/item/", "百度百科-")
    for chunk in chunks:
        doc_id = hashlib.md5(chunk.encode()).hexdigest()[:12]
        rag.add_text(chunk, {"source": source, "category": category, "url": url}, doc_id)
        total += 1
    
    time.sleep(1)  # 礼貌延迟，防止被封

print(f"\n✅ 全部完成！共新增 {total} 条知识")
print(f"📊 知识库当前文档数: {rag.collection.count()}")
