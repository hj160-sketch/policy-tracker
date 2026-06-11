# -*- coding: utf-8 -*-
"""主流程:抓取 → 去重 → DeepSeek 分析新条目 → 生成今日要点 → 写入 docs/data.json"""
import os
import json
import datetime
import pathlib

import fetch
import analyze

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "docs" / "data.json"
MAX_ITEMS = 600            # data.json 最多保留条数
MAX_ANALYZE = int(os.environ.get("MAX_ANALYZE", "40"))  # 单次最多分析条数(控制 API 费用)


def load_existing():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": [], "brief": None, "generated_at": None}


def main():
    today = datetime.date.today().isoformat()
    data = load_existing()
    known = {it["url"]: it for it in data.get("items", [])}

    fetched, errors = fetch.fetch_all()

    new_items = [it for it in fetched if it["url"] not in known]
    print(f"[main] fetched={len(fetched)} new={len(new_items)}")

    # AI 分析(只分析新条目;没有 key 时跳过,前端会显示原始信息)
    if analyze.available():
        for i, it in enumerate(new_items):
            if i >= MAX_ANALYZE:
                print(f"[main] reached MAX_ANALYZE={MAX_ANALYZE}, rest left unanalyzed")
                break
            it["ai"] = analyze.analyze_item(it)
    else:
        print("[main] DEEPSEEK_API_KEY not set, skipping AI analysis")

    # 合并:新条目 + 已有条目(保留旧的 AI 分析)
    for it in new_items:
        known[it["url"]] = it
    items = sorted(known.values(), key=lambda x: x.get("date") or "", reverse=True)
    items = items[:MAX_ITEMS]

    # 今日要点:有新条目且可用 AI 时重新生成,否则保留旧的
    brief = data.get("brief")
    if new_items and analyze.available():
        cutoff = (datetime.date.today() - datetime.timedelta(days=4)).isoformat()
        recent = [it for it in items if (it.get("date") or "") >= cutoff]
        new_brief = analyze.daily_brief(recent or items[:20], today)
        if new_brief:
            brief = {"text": new_brief, "date": today}

    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%d %H:%M UTC"),
        "brief": brief,
        "errors": errors,
        "counts": {c: sum(1 for it in items if it["country"] == c)
                   for c in ("cn", "us", "jp")},
        "items": items,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    print(f"[main] wrote {DATA_FILE} ({len(items)} items)")


if __name__ == "__main__":
    main()
