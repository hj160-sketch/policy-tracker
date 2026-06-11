# -*- coding: utf-8 -*-
"""回填 2025-07 以来的三国重大政策 → docs/major.json
用法: python scraper/backfill.py  (需 DEEPSEEK_API_KEY)
流程: 抓取候选 → DeepSeek 批量打分筛选+打主题标签 → 重要条目生成摘要分析 → 写 major.json
"""
import os
import re
import json
import time
import datetime
import pathlib
import requests
from bs4 import BeautifulSoup

import analyze
from fetch import UA, TIMEOUT, _clean

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAJOR_FILE = ROOT / "docs" / "major.json"
SINCE = os.environ.get("BACKFILL_SINCE", "2025-07-01")
MIN_IMPORTANCE = int(os.environ.get("MIN_IMPORTANCE", "3"))  # 1-5,保留 >= 此分

TOPICS = ["关税与贸易", "出口管制与制裁", "半导体与芯片", "人工智能", "能源与气候",
          "金融监管", "财政与税收", "产业政策", "供应链", "国防安全", "外交与同盟",
          "移民与边境", "医疗卫生", "劳动就业", "农业粮食", "科技监管与数据",
          "基础设施", "社会民生", "法治与行政改革", "其他"]


# ---------- 抓取候选 ----------

def fetch_cn_history():
    """gov.cn 国务院文件(gongwen),翻页直到 SINCE(带 cookie 预热与重试)"""
    import fetch as F
    session = F.gov_cn_session()
    items, page = [], 1
    while page <= 40:
        cat_map = F.gov_cn_query(session, page=page, n=50)
        lst = ((cat_map.get("gongwen") or {}).get("listVO") or [])
        if not lst:
            print(f"[cn] page {page} empty, stop")
            break
        stop = False
        for it in lst:
            date = (it.get("pubtimeStr") or "").replace(".", "-")
            if date and date < SINCE:
                stop = True
                continue
            if not it.get("url"):
                continue
            items.append({"country": "cn", "title": _clean(it.get("title")),
                          "url": it["url"], "date": date, "org": _clean(it.get("puborg")),
                          "doc_no": it.get("pcode") or "",
                          "excerpt": _clean(it.get("summary"))[:200],
                          "source": "中国政府网·国务院文件"})
        if stop:
            break
        page += 1
        time.sleep(1)
    print(f"[cn] {len(items)} candidates")
    return items


def fetch_us_history():
    """Federal Register: 总统文件 + 重要规章,日期范围翻页"""
    items = []
    for cond, label in [({"conditions[type][]": "PRESDOCU"}, "Federal Register·总统文件"),
                        ({"conditions[type][]": "RULE", "conditions[significant]": "1"},
                         "Federal Register·重要规章")]:
        page = 1
        while page <= 10:
            params = {"per_page": 100, "page": page, "order": "newest",
                      "conditions[publication_date][gte]": SINCE, **cond}
            try:
                r = requests.get("https://www.federalregister.gov/api/v1/documents.json",
                                 params=params, headers=UA, timeout=TIMEOUT)
                r.raise_for_status()
                res = r.json().get("results", [])
            except Exception as e:
                print(f"[us] {label} p{page} failed: {e}"); break
            if not res:
                break
            for it in res:
                if not it.get("html_url"):
                    continue
                items.append({"country": "us", "title": it.get("title", ""),
                              "url": it["html_url"], "date": it.get("publication_date", ""),
                              "org": ", ".join(a.get("name") or "" for a in (it.get("agencies") or [])
                                               if isinstance(a, dict) and a.get("name")),
                              "doc_no": it.get("document_number", ""),
                              "excerpt": (it.get("abstract") or "")[:200],
                              "pdf": it.get("pdf_url") or "", "source": label})
            if len(res) < 100:
                break
            page += 1
            time.sleep(1)
    print(f"[us] {len(items)} candidates")
    return items


def fetch_jp_history():
    """首相官邸 総理の一日 月度归档(103/104/105 届内阁)"""
    items, seen = [], set()
    today = datetime.date.today()
    months, d = [], datetime.date.fromisoformat(SINCE).replace(day=1)
    while d <= today:
        months.append(d.strftime("%Y%m"))
        d = (d.replace(day=28) + datetime.timedelta(days=5)).replace(day=1)
    for cab in ("103", "104", "105"):
        for ym in months:
            url = f"https://www.kantei.go.jp/jp/{cab}/actions/{ym}/index.html"
            try:
                r = requests.get(url, headers=UA, timeout=TIMEOUT)
                if r.status_code != 200:
                    continue
                r.encoding = "utf-8"
                soup = BeautifulSoup(r.text, "html.parser")
            except Exception:
                continue
            for a in soup.find_all("a", href=re.compile(rf"/jp/{cab}/actions/{ym}/")):
                title = a.get_text(" ", strip=True)
                href = a["href"]
                if not title or len(title) < 4:
                    continue
                if href.startswith("/"):
                    href = "https://www.kantei.go.jp" + href
                if href in seen:
                    continue
                seen.add(href)
                # 更新日 在相邻文本中,从 li 容器找日期
                ctx = a.find_parent("li")
                m = ctx and re.search(r"令和(\d+)年(\d+)月(\d+)日", ctx.get_text())
                if m:
                    date = f"{2018+int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                else:
                    date = f"{ym[:4]}-{ym[4:]}-15"
                desc = ""
                if ctx:
                    ps = ctx.get_text(" ", strip=True)
                    desc = ps[:150]
                items.append({"country": "jp", "title": title, "url": href, "date": date,
                              "org": "総理の一日", "doc_no": "", "excerpt": desc,
                              "source": "首相官邸·総理の一日"})
            time.sleep(0.5)
    print(f"[jp] {len(items)} candidates")
    return items


# ---------- AI 筛选 + 打标 ----------

def batch_screen(items):
    """批量: 重要性评分(1-5) + 主题标签 + 中文标题。返回筛选后的列表。"""
    kept = []
    B = 15
    for i in range(0, len(items), B):
        batch = items[i:i+B]
        lines = [f"{j}. [{it['country']}] {it['date']} {it['title']} ({it.get('org','')})"
                 for j, it in enumerate(batch)]
        prompt = f"""你是政策分析师。对下列政策/政府动态逐条评估,输出 JSON 数组(长度={len(batch)},顺序对应,不要其他内容):
每个元素: {{"i": 序号, "imp": 重要性1-5, "topics": [1-2个主题], "title_zh": "中文标题(已是中文则原样)"}}

重要性标准: 5=重大战略政策/法律/行政令(全国性影响); 4=重要行业政策或重大外交动作; 3=有实质内容的具体政策; 2=程序性/礼仪性/人事; 1=无政策含义(赠礼、慰灵、表彰等)。
主题从此列表选: {TOPICS}

{chr(10).join(lines)}"""
        try:
            content = analyze._chat([{"role": "user", "content": prompt}], max_tokens=2000)
            m = re.search(r"\[.*\]", content, re.S)
            arr = json.loads(m.group(0)) if m else []
            for ent in arr:
                idx = ent.get("i")
                if not isinstance(idx, int) or idx >= len(batch):
                    continue
                it = batch[idx]
                imp = int(ent.get("imp") or 0)
                if imp >= MIN_IMPORTANCE:
                    it["importance"] = imp
                    it["topics"] = [t for t in (ent.get("topics") or []) if t in TOPICS][:2] or ["其他"]
                    it["title_zh"] = str(ent.get("title_zh") or it["title"])[:120]
                    kept.append(it)
        except Exception as e:
            print(f"[screen] batch {i//B} failed: {e}")
        print(f"[screen] {min(i+B,len(items))}/{len(items)} kept={len(kept)}")
    return kept


def enrich(items):
    """为保留条目生成一句话摘要(用于图谱节点详情)"""
    for n, it in enumerate(items):
        a = analyze.analyze_item(it)
        if a:
            it["summary"] = a["summary"]
            it["analysis"] = a["analysis"]
            it["title_zh"] = a["title_zh"] or it.get("title_zh") or it["title"]
        if n % 20 == 0:
            print(f"[enrich] {n}/{len(items)}")
    return items


def main():
    if not analyze.available():
        raise SystemExit("需要 DEEPSEEK_API_KEY")
    cands = fetch_cn_history() + fetch_us_history() + fetch_jp_history()
    # 去重
    seen, uniq = set(), []
    for it in cands:
        if it["url"] not in seen:
            seen.add(it["url"]); uniq.append(it)
    print(f"[main] candidates={len(uniq)}")
    kept = batch_screen(uniq)
    print(f"[main] kept(imp>={MIN_IMPORTANCE})={len(kept)}")
    kept = enrich(kept)
    kept.sort(key=lambda x: x.get("date") or "", reverse=True)
    out = {"generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "since": SINCE, "topics": TOPICS, "items": kept}
    MAJOR_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[main] wrote {MAJOR_FILE} ({len(kept)} items)")


if __name__ == "__main__":
    main()
