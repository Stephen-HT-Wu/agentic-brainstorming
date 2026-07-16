"""
stage14-signals 新增：設定階段的一次性公司背景調查（不是圖節點——使用者
原話「可以在設定的時候」暗示這是開會前、無狀態、不需要 checkpoint/HITL
的單次動作，見 server.py 的 /api/company-research 端點，同步呼叫即可）。

目的：取代人工填寫 company.md 的麻煩——給 1-n 個網址或公司名稱，自動抓取
內容＋（抓不到內容時退回）web_search，一次 LLM 呼叫整理成 company.md
風格的公司定位描述，寫成 practice/{slug}-company.md（gitignore，見根目錄
.gitignore 的 `*-company.md`）。

真實跑測踩過的坑（web_search()/is_usable_search_result() 只回傳搜尋結果
的 title/url/snippet，這個專案裡沒有任何函式會真的把一個網址的完整可讀
內容抓下來，requirements.txt 也沒有 requests/bs4/trafilatura 這類依賴）：
`fetch_url_text()` 刻意分兩層——有 TAVILY_API_KEY 時用 Tavily 的
`/extract` 端點（跟 graph.py `_search_tavily()` 完全一樣的 urllib.request
POST 手法）；沒有 key 或抓失敗時，退回純標準庫的 `urllib.request.urlopen()`
+ 一個繼承 `html.parser.HTMLParser` 的簡易標籤剝除器，不新增任何依賴。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional

import graph as sg


class _TextStripper(HTMLParser):
    """只保留可讀文字，跳過 <script>/<style> 內容——這個專案刻意不加
    bs4/trafilatura 這類依賴，純標準庫夠用（公司官網文字不需要精準的
    DOM 結構，只需要粗略的可讀內容給 LLM 整理）。"""

    _SKIP_TAGS = {"script", "style", "noscript"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.chunks: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.chunks.append(text)


def _fetch_url_stdlib(url: str, max_chars: int) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(max_chars * 4).decode("utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, ValueError, UnicodeDecodeError):
        return ""
    stripper = _TextStripper()
    try:
        stripper.feed(raw)
    except Exception:  # noqa: BLE001 - 格式不良的 HTML 不該讓整個調查流程崩潰
        return ""
    text = re.sub(r"\s+", " ", " ".join(stripper.chunks)).strip()
    return text[:max_chars]


def _tavily_extract(url: str, api_key: str, max_chars: int) -> str:
    payload = json.dumps({"api_key": api_key, "urls": [url]}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/extract",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return ""
    results = data.get("results") or []
    if not results:
        return ""
    return (results[0].get("raw_content") or "")[:max_chars]


def fetch_url_text(url: str, max_chars: int = 6000) -> str:
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        text = _tavily_extract(url, tavily_key, max_chars)
        if text:
            return text
    return _fetch_url_stdlib(url, max_chars)


COMPANY_RESEARCH_SYSTEM_PROMPT = (
    "你是企業研究員，根據提供的素材（網頁內容摘錄或搜尋結果），整理成一份"
    "200-500字的公司定位描述，寫作風格比照內部 company.md 設定檔——目的是"
    "讓後續的腦力激盪流程能從這份描述衍生出這家公司實際具備、彼此不同的"
    "職能（部門/技術/內容素材/通路/既有業務關係），所以內容要求具體、"
    "可操作，嚴禁空泛的行銷詞（例如「致力於提供最好的服務」這種話一律"
    "不要）。至少要包含：這家公司實際的業務範圍、具備的技術/內容/資產、"
    "主要通路、已知的合作夥伴或業務關係。直接輸出純文字內容，不要"
    "markdown 標題、不要 JSON、不要任何前言後語。"
)


def research_company(name: str, urls: Optional[List[str]] = None) -> dict:
    """回傳 {"markdown": str, "sources": [{"url"/"query": ..., "used": bool}]}——
    sources 讓呼叫端知道這份描述實際依據了什麼素材，不是黑盒子生成的。"""
    urls = [u.strip() for u in (urls or []) if u.strip()]
    materials: List[str] = []
    sources: List[dict] = []

    for url in urls:
        text = fetch_url_text(url)
        if text:
            materials.append(f"[網頁內容：{url}]\n{text}")
            sources.append({"url": url, "method": "fetch", "used": True})
        else:
            # 抓不到內容就退回把網域當查詢字串丟給既有的 web_search()——
            # 跟 desk_research_hypothesize_jobs() 已有的做法一致。
            hits = sg.web_search(f"{name} {url}", max_results=3)
            usable = [h for h in hits if sg.is_usable_search_result(h)]
            if usable:
                materials.append(
                    f"[搜尋結果（{url} 抓取失敗，改用搜尋）]\n"
                    + "\n".join(f"- {h['title']}：{h['snippet']}" for h in usable)
                )
            sources.append({"url": url, "method": "search_fallback", "used": bool(usable)})

    if not urls:
        query = f"{name} 公司 官網 服務內容"
        hits = sg.web_search(query, max_results=5)
        usable = [h for h in hits if sg.is_usable_search_result(h)]
        if usable:
            materials.append(
                "[搜尋結果]\n" + "\n".join(f"- {h['title']}：{h['snippet']}（{h['url']}）" for h in usable)
            )
        sources.append({"query": query, "method": "search", "used": bool(usable)})

    materials_block = "\n\n".join(materials) if materials else "（沒有抓到任何素材，請根據公司名稱本身合理推測，並在內容中誠實標註這是推測。）"
    user = f"公司名稱：{name}\n\n素材：\n{materials_block}"
    markdown = sg.call_llm(sg.SMART_MODEL, COMPANY_RESEARCH_SYSTEM_PROMPT, user, max_tokens=1500).strip()

    return {"markdown": markdown, "sources": sources}


def write_company_profile(slug: str, markdown: str) -> Path:
    path = sg.PRACTICE_DIR / f"{slug}-company.md"
    path.write_text(markdown, encoding="utf-8")
    return path
