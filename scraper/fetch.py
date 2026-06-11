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
BROWSER_HEADERS = {
    **UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.gov.cn/",
    "Origin": "https://www.gov.cn",
}
TIMEOUT = 30


def _clean(text):
    return re.sub(r"<[^>]+>", "", text or "").replace("　", " ").strip()


def gov_cn_session():
    """带 cookie 预热的会话(应对 gov.cn 对数据中心 IP 的软封锁)"""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    for warm in ("https://www.gov.cn/zhengce/zuixin/",
                 "https://sousuo.www.gov.cn/zcwjk/policyRetrieve"):
        try:
            s.get(warm, timeout=15)
        except Exception:
            pass
    return s


def gov_cn_query(session, page=1, n=15):
    """查询政策文件库一页,返回 catMap;空结果时重试一次"""
    import time as _t
    params = {
        "t": "zhengcelibrary", "q": "", "timetype": "timeqb",
        "mintime": "", "maxtime": "", "sort": "pubtime", "sortType": 1,
        "searchfield": "title", "p": page, "n": n,
    }
    for attempt in range(3):
        try:
            r = session.get("https://sousuo.www.gov.cn/search-gov/data",
                            params=params, timeout=TIMEOUT)
            r.raise_for_status()
            cat_map = (r.json().get("catMap") or {})
            if ((cat_map.get("gongwen") or {}).get("listVO")):
                return cat_map
            print(f"[cn] empty catMap (attempt {attempt+1}), retrying...")
        except Exception as e:
            print(f"[cn] query failed (attempt {attempt+1}): {e}")
        _t.sleep(5 * (attempt + 1))
    return {}


def fetch_china_rss(n=15):
    """备用源: RSSHub 公共实例的国务院政策文件库路由"""
    items = []
    for route, label in [("gov/zhengce/zhengceku", "中国政府网·政策文件库(RSS)"),
                         ("gov/zhengce/zuixin", "中国政府网·最新政策(RSS)")]:
        for host in ("https://rsshub.app", "https://rsshub.rssforever.com"):
            try:
                r = requests.get(f"{host}/{route}", headers=UA, timeout=TIMEOUT)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "xml")
                for it in soup.find_all("item")[:n]:
                    link = (it.link and it.link.get_text(strip=True)) or ""
                    if not link:
                        continue
                    pub = it.pubDate.get_text(strip=True) if it.pubDate else ""
                    date = ""
                    try:
                        from email.utils import parsedate_to_datetime
                        date = parsedate_to_datetime(pub).strftime("%Y-%m-%d")
                    except Exception:
                        pass
                    items.append({
                        "country": "cn",
                        "title": _clean(it.title.get_text(strip=True) if it.title else ""),
                        "url": link, "date": date, "org": "", "doc_no": "",
                        "excerpt": _clean(it.description.get_text(strip=True) if it.description else "")[:300],
                        "source": label,
                    })
                if items:
                    return items
            except Exception as e:
                print(f"[cn-rss] {host}/{route} failed: {e}")
    return items


def fetch_china(n=15):
    """中国政府网·国务院政策文件库 JSON 接口(国务院文件 + 部门文件);失败时回退 RSS"""
    s = gov_cn_session()
    cat_map = gov_cn_query(s, page=1, n=n)
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
    if not items:
        print("[cn] primary source empty, falling back to RSS")
        items = fetch_china_rss(n)
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
