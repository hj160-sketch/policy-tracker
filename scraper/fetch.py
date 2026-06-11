# -*- coding: utf-8 -*-
"""抓取中国/美国/日本官方政策数据源"""
import re
import requests
from bs4 import BeautifulSoup

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
TIMEOUT = 30


def _clean(text):
    return re.sub(r"<[^>]+>", "", text or "").replace("　", " ").strip()


def fetch_china(n=15):
    """中国政府网·国务院政策文件库 JSON 接口(国务院文件 + 部门文件)"""
    url = "https://sousuo.www.gov.cn/search-gov/data"
    params = {
        "t": "zhengcelibrary", "q": "", "timetype": "timeqb",
        "mintime": "", "maxtime": "", "sort": "pubtime", "sortType": 1,
        "searchfield": "title", "p": 1, "n": n,
    }
    r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    cat_map = (r.json().get("catMap") or {})
    labels = {"gongwen": "中国政府网·国务院文件", "bumenfile": "中国政府网·部门文件"}
    items = []
    for cat, label in labels.items():
        for it in ((cat_map.get(cat) or {}).get("listVO") or []):
            if not it.get("url"):
                continue
            items.append({
                "country": "cn",
                "title": _clean(it.get("title")),
                "url": it["url"],
                "date": (it.get("pubtimeStr") or "").replace(".", "-"),
                "org": _clean(it.get("puborg")),
                "doc_no": it.get("pcode") or "",
                "excerpt": _clean(it.get("summary"))[:300],
                "source": label,
            })
    return items


def fetch_us(n=15):
    """Federal Register API:总统文件(行政令等) + 重要规章"""
    base = "https://www.federalregister.gov/api/v1/documents.json"
    queries = [
        ({"per_page": n, "order": "newest",
          "conditions[type][]": "PRESDOCU"}, "Federal Register·总统文件"),
        ({"per_page": n, "order": "newest", "conditions[type][]": "RULE",
          "conditions[significant]": "1"}, "Federal Register·重要规章"),
    ]
    items = []
    for params, label in queries:
        r = requests.get(base, params=params, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        for it in r.json().get("results", []):
            if not it.get("html_url"):
                continue
            agencies = ", ".join(
                a.get("name") or "" for a in (it.get("agencies") or [])
                if isinstance(a, dict) and a.get("name")
            )
            items.append({
                "country": "us",
                "title": it.get("title", ""),
                "url": it["html_url"],
                "date": it.get("publication_date", ""),
                "org": agencies,
                "doc_no": it.get("document_number", ""),
                "excerpt": (it.get("abstract") or "")[:300],
                "pdf": it.get("pdf_url") or "",
                "source": label,
            })
    return items


def fetch_japan(n=20):
    """首相官邸·新着情報(HTML 解析,条目格式:令和X年X月X日 分类 标题)"""
    r = requests.get("https://www.kantei.go.jp/jp/news/index.html",
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    items, seen = [], set()
    pat = re.compile(r"令和(\d+)年(\d+)月(\d+)日\s+(\S+)\s+(.+)")
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        m = pat.match(text)
        if not m:
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.kantei.go.jp" + href
        if not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        year = 2018 + int(m.group(1))  # 令和元年 = 2019
        items.append({
            "country": "jp",
            "title": m.group(5).strip(),
            "url": href,
            "date": f"{year:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
            "org": m.group(4),  # 分类:総理の一日/長官会見/総理の発表 等
            "doc_no": "",
            "excerpt": "",
            "source": "首相官邸·新着情報",
        })
        if len(items) >= n:
            break
    return items


def fetch_all():
    result, errors = [], []
    for name, fn in [("china", fetch_china), ("us", fetch_us), ("japan", fetch_japan)]:
        try:
            got = fn()
            print(f"[fetch] {name}: {len(got)} items")
            result.extend(got)
        except Exception as e:  # 单个源失败不影响其他源
            print(f"[fetch] {name} FAILED: {e}")
            errors.append(f"{name}: {e}")
    return result, errors
