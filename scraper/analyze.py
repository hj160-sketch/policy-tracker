# -*- coding: utf-8 -*-
"""调用 DeepSeek API 生成中文摘要与分析"""
import os
import re
import json
import requests

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

COUNTRY_NAME = {"cn": "中国", "us": "美国", "jp": "日本"}
CATEGORIES = ["经济金融", "科技产业", "外交安全", "民生社会", "能源环境", "法律监管", "其他"]


def available():
    return bool(API_KEY)


def _chat(messages, max_tokens=700):
    r = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages,
              "temperature": 0.3, "max_tokens": max_tokens},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def analyze_item(item):
    """对单条政策生成:中文标题、一句话摘要、影响分析、分类。失败返回 None。"""
    prompt = f"""你是一名政策分析师。以下是一条{COUNTRY_NAME.get(item['country'], '')}政府官方发布的信息,请用中文分析。

标题: {item['title']}
发布机构: {item.get('org') or '未知'}
日期: {item.get('date')}
文号: {item.get('doc_no') or '无'}
摘要片段: {item.get('excerpt') or '无'}

请输出 JSON(不要其他内容),字段:
- "title_zh": 中文标题(原文已是中文则原样保留,英文/日文则翻译)
- "summary": 一句话说明这条政策/动态是什么(40字以内)
- "analysis": 影响分析,2-3句话:针对谁、可能产生什么影响、值得关注的点(120字以内)
- "category": 从 {CATEGORIES} 中选一个最贴切的"""
    try:
        content = _chat([{"role": "user", "content": prompt}])
        data = _extract_json(content)
        if not data:
            return None
        return {
            "title_zh": str(data.get("title_zh") or item["title"])[:120],
            "summary": str(data.get("summary") or "")[:100],
            "analysis": str(data.get("analysis") or "")[:300],
            "category": data.get("category") if data.get("category") in CATEGORIES else "其他",
        }
    except Exception as e:
        print(f"[analyze] failed for {item['url'][:60]}: {e}")
        return None


def daily_brief(recent_items, today):
    """根据近几日条目生成「今日要点」综述。失败返回 None。"""
    if not recent_items:
        return None
    lines = []
    for it in recent_items[:25]:
        a = it.get("ai") or {}
        lines.append(f"[{COUNTRY_NAME.get(it['country'])}] {it['date']} "
                     f"{a.get('title_zh') or it['title']} — {a.get('summary') or ''}")
    prompt = f"""你是一名政策分析师。今天是{today}。以下是中国、美国、日本最近发布的政策动态清单:

{chr(10).join(lines)}

请用中文写一段150-250字的「今日要点」综述:概括三国最值得关注的政策动向,如有跨国关联(如贸易、科技竞争)请点出。直接输出正文,不要标题、不要列表。"""
    try:
        return _chat([{"role": "user", "content": prompt}], max_tokens=500).strip()
    except Exception as e:
        print(f"[analyze] daily brief failed: {e}")
        return None
