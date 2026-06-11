# -*- coding: utf-8 -*-
"""major.json → docs/graph.json (政策关系图数据)
节点: 政策 + 主题枢纽; 边: 政策-主题 归属边 + DeepSeek 识别的政策间直接关联边
"""
import re
import json
import datetime
import pathlib

import analyze

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAJOR_FILE = ROOT / "docs" / "major.json"
GRAPH_FILE = ROOT / "docs" / "graph.json"
MAX_PER_TOPIC = 40  # 单主题送入关联识别的最大条数(按重要性取前N)


def find_relations(topic, items):
    """对同主题政策,让 DeepSeek 找出两两直接关联(回应/反制/配套/递进)"""
    if len(items) < 2:
        return []
    lines = [f"{j}. [{ {'cn':'中国','us':'美国','jp':'日本'}[it['country']] }] {it['date']} {it.get('title_zh') or it['title']}"
             for j, it in enumerate(items)]
    prompt = f"""以下是「{topic}」主题下中美日的政策清单。找出存在**直接关系**的政策对(如: 一国反制/回应另一国、同一国配套或递进措施、明确针对同一事件)。宁缺毋滥,只列把握大的。
输出 JSON 数组(可为空,不要其他内容),每个元素: {{"a": 序号, "b": 序号, "rel": "关系简述(8字以内,如'关税反制''配套措施''回应出口管制')"}}

{chr(10).join(lines)}"""
    try:
        content = analyze._chat([{"role": "user", "content": prompt}], max_tokens=1500)
        m = re.search(r"\[.*\]", content, re.S)
        arr = json.loads(m.group(0)) if m else []
        out = []
        for e in arr:
            a, b = e.get("a"), e.get("b")
            if isinstance(a, int) and isinstance(b, int) and a != b \
                    and 0 <= a < len(items) and 0 <= b < len(items):
                out.append((items[a]["url"], items[b]["url"], str(e.get("rel") or "关联")[:12]))
        return out
    except Exception as ex:
        print(f"[rel] {topic} failed: {ex}")
        return []


def main():
    data = json.loads(MAJOR_FILE.read_text(encoding="utf-8"))
    items = data["items"]
    topics = data.get("topics") or []

    nodes, edges = [], []
    used_topics = set()

    for it in items:
        nodes.append({
            "id": it["url"], "type": "policy", "country": it["country"],
            "label": (it.get("title_zh") or it["title"])[:40],
            "title_zh": it.get("title_zh") or it["title"], "title": it["title"],
            "date": it.get("date"), "org": it.get("org"), "doc_no": it.get("doc_no"),
            "summary": it.get("summary") or "", "analysis": it.get("analysis") or "",
            "topics": it.get("topics") or ["其他"],
            "importance": it.get("importance", 3), "url": it["url"],
        })
        for t in (it.get("topics") or ["其他"]):
            used_topics.add(t)
            edges.append({"from": it["url"], "to": f"topic::{t}", "kind": "topic"})

    for t in sorted(used_topics):
        cnt = sum(1 for it in items if t in (it.get("topics") or []))
        nodes.append({"id": f"topic::{t}", "type": "topic", "label": t, "count": cnt})

    # 关联边(按主题分组识别)
    if analyze.available():
        for t in sorted(used_topics):
            group = [it for it in items if t in (it.get("topics") or [])]
            group.sort(key=lambda x: (-x.get("importance", 3), x.get("date") or ""))
            group = group[:MAX_PER_TOPIC]
            rels = find_relations(t, group)
            for a, b, rel in rels:
                edges.append({"from": a, "to": b, "kind": "relation", "label": rel})
            print(f"[rel] {t}: {len(group)} items, {len(rels)} relations")

    # 去重关联边
    seen, dedup = set(), []
    for e in edges:
        key = (e["kind"], tuple(sorted([e["from"], e["to"]])))
        if key in seen:
            continue
        seen.add(key); dedup.append(e)

    out = {"generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "topics": sorted(used_topics), "nodes": nodes, "edges": dedup}
    GRAPH_FILE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"[graph] {len(nodes)} nodes, {len(dedup)} edges -> {GRAPH_FILE}")


if __name__ == "__main__":
    main()
