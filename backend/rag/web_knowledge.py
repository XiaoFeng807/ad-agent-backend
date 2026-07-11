"""Web Knowledge Scraper - 搜索+爬取+清洗+自动存入RAG"""
import re, time, logging, requests
from bs4 import BeautifulSoup
from urllib.parse import quote
from datetime import datetime

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SKIP_TAGS = ["script","style","nav","footer","header","aside","noscript","iframe","form"]
SKIP_CLASSES = ["nav","footer","sidebar","comment","ad","advertisement","recommend"]

# ===== 已知可爬取的广告知识源 =====
PRESET_URLS = [
    {"url": "https://www.10100.com/article/1966776",
     "category": "行业知识",
     "title": "ROAS怎么算、多少才算赚钱"},
    {"url": "https://www.10100.com/article/1968266",
     "category": "优化策略",
     "title": "Google Ads广告优化技巧"},
    {"url": "https://www.10100.com/article/1966083",
     "category": "行业知识",
     "title": "跨境电商广告投放指南"},
]


def clean_html(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "lxml")
    for t in SKIP_TAGS:
        for el in soup.find_all(t): el.decompose()
    for el in soup.find_all(class_=lambda c: c and any(cls in str(c).lower() for cls in SKIP_CLASSES)):
        el.decompose()
    main = soup.find("article") or soup.find("main") or soup.find(class_=re.compile(r"(content|article|post|main)", re.I))
    main = main or soup.find("body") or soup
    text = main.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def search_bing(keyword: str, num: int = 5) -> list[dict]:
    try:
        resp = SESSION.get(f"https://www.bing.com/search?q={quote(keyword)}&count={num}", timeout=15)
        soup = BeautifulSoup(resp.text, "lxml")
        results = []
        for item in soup.select("li.b_algo"):
            h2 = item.select_one("h2 a")
            if h2:
                results.append({
                    "title": h2.get_text(strip=True),
                    "url": h2.get("href", ""),
                    "snippet": (item.select_one(".b_caption p") or BeautifulSoup("","lxml")).get_text(strip=True) if item.select_one(".b_caption p") else "",
                })
        return results[:num]
    except Exception as e:
        logger.warning(f"[Web] Bing search failed: {e}")
        return []


def guess_category(url: str, title: str = "") -> str:
    c = (url + " " + title).lower()
    if any(k in c for k in ["baike","wiki","what is","definition","glossary"]): return "行业知识"
    if any(k in c for k in ["google","meta","facebook","tiktok","ads","投放"]): return "平台知识"
    if any(k in c for k in ["optimize","optimization","策略","预算","出价","定向","转化","提高"]): return "优化策略"
    if any(k in c for k in ["trend","report","数据","行业","市场","分析"]): return "行业趋势"
    return "通用知识"


class WebKnowledge:
    def __init__(self, delay: float = 1.5):
        self.delay = delay

    def _wait(self):
        time.sleep(self.delay)

    def search(self, keyword: str, num: int = 5) -> list[dict]:
        self._wait()
        return search_bing(keyword, num)

    def scrape_url(self, url: str) -> str | None:
        self._wait()
        try:
            resp = SESSION.get(url, timeout=20)
            if resp.status_code != 200: return None
            resp.encoding = resp.apparent_encoding or "utf-8"
            text = clean_html(resp.text)
            return text if len(text) >= 50 else None
        except Exception as e:
            logger.warning(f"[Web] Scrape failed {url[:50]}: {str(e)[:60]}")
            return None

    def scrape_and_store(self, url: str, rag, category: str = None, title: str = "") -> bool:
        text = self.scrape_url(url)
        if not text: return False
        rag.add_text(text, {
            "source": url,
            "category": category or guess_category(url, title),
            "title": title or url,
            "scrape_date": datetime.now().strftime("%Y-%m-%d"),
        })
        return True

    def search_and_store(self, keyword: str, rag, max_urls: int = 3) -> int:
        results = self.search(keyword, max_urls)
        stored = 0
        for item in results:
            if self.scrape_and_store(item["url"], rag, title=item.get("title","")):
                stored += 1
        logger.info(f"[Web] '{keyword}': {len(results)} found, {stored} stored")
        return stored

    def batch_search(self, keywords: list, rag) -> dict:
        return {kw: self.search_and_store(kw, rag) for kw in keywords}

    def scrape_presets(self, rag) -> int:
        """爬取预设的已知可用知识源"""
        stored = 0
        for src in PRESET_URLS:
            if self.scrape_and_store(src["url"], rag, category=src["category"], title=src["title"]):
                stored += 1
        logger.info(f"[Web] Presets: {stored}/{len(PRESET_URLS)} stored")
        return stored


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from backend.rag.rag_knowledge import RAGKnowledge
    rag = RAGKnowledge()
    web = WebKnowledge(delay=0.5)
    n = web.scrape_presets(rag)
    print(f"Presets stored: {n}, Total: {rag.collection.count()}")