# -*- coding: utf-8 -*-
"""Validate locally generated Japan enrichment parts and merge them into data.json."""
from __future__ import annotations

import json
import pathlib
import re
from collections import Counter
from difflib import SequenceMatcher

import analyze
from enrich_japan import DATA_FILE, atomic_write_json, load_data


ROOT = pathlib.Path(__file__).resolve().parent.parent
PARTS_DIR = ROOT / "japan_enrichment_parts"
ORIGINAL_TITLE_CORRECTIONS = {
    "https://www.kantei.go.jp/jp/105/statement/2026/0626kaiken.html": (
        "山梨県東部・富士五湖を震源とする地震についての会見"
    ),
}
TITLE_ZH_OVERRIDES = {
    "https://www.kantei.go.jp/jp/105/actions/202606/25bura.html": (
        "高市首相就岩手县近海地震举行记者会（活动记录）"
    ),
    "https://www.kantei.go.jp/jp/105/statement/2026/0625kaiken.html": (
        "高市首相就岩手县近海地震举行记者会（发言全文）"
    ),
    "https://www.kantei.go.jp/jp/105/discourse/202606message.html": (
        "高市首相向日本物理治疗师协会第55次定期总会发表视频致辞"
    ),
}


def one_line(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def main() -> int:
    data = load_data(DATA_FILE)
    japan = [item for item in data["items"] if item.get("country") == "jp"]
    expected_urls = {item["url"] for item in japan}
    records: dict[str, dict] = {}

    part_files = sorted(PARTS_DIR.glob("part*.json"))
    if not part_files:
        raise SystemExit(f"No part files found in {PARTS_DIR}")

    for path in part_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{path} must contain a JSON array")
        for raw in payload:
            if not isinstance(raw, dict):
                raise ValueError(f"{path} contains a non-object record")
            url = str(raw.get("url") or "")
            if not url or url in records:
                raise ValueError(f"Missing or duplicate URL: {url!r}")
            record = {
                "title_zh": one_line(raw.get("title_zh")),
                "org_zh": one_line(raw.get("org_zh")),
                "explanation": one_line(raw.get("explanation")),
                "impact": one_line(raw.get("impact")),
                "jp_enrichment_version": analyze.JP_ENRICHMENT_VERSION,
            }
            if raw.get("detail_version") != "jp-v2-400":
                raise ValueError(f"{url} has an unexpected detail_version")
            candidate = {"ai": record}
            if not analyze.japan_enrichment_is_compliant(candidate):
                raise ValueError(
                    f"{url} failed compliance: explanation={len(record['explanation'])}, "
                    f"impact={len(record['impact'])}"
                )
            chinese_display_text = "".join(
                record[field]
                for field in ("title_zh", "org_zh", "explanation", "impact")
            )
            if analyze.JAPANESE_KANA_RE.search(chinese_display_text):
                raise ValueError(f"{url} still contains Japanese kana in Chinese display fields")
            records[url] = record

    actual_urls = set(records)
    missing = expected_urls - actual_urls
    extra = actual_urls - expected_urls
    if missing or extra:
        raise ValueError(
            f"Part coverage mismatch: expected={len(expected_urls)} actual={len(actual_urls)} "
            f"missing={len(missing)} extra={len(extra)}"
        )

    explanation_seen: dict[str, str] = {}
    impact_seen: dict[str, str] = {}
    for url, record in records.items():
        for field, seen in (("explanation", explanation_seen), ("impact", impact_seen)):
            value = record[field]
            if value in seen:
                raise ValueError(f"Exact duplicate {field}: {seen[value]} and {url}")
            seen[value] = url

    ordered_records = list(records.items())
    for index, (left_url, left) in enumerate(ordered_records):
        for right_url, right in ordered_records[index + 1:]:
            for field in ("explanation", "impact"):
                similarity = SequenceMatcher(
                    None, left[field], right[field], autojunk=False
                ).ratio()
                if similarity >= 0.90:
                    raise ValueError(
                        f"Near-duplicate {field} ({similarity:.3f}): "
                        f"{left_url} and {right_url}"
                    )

    for field in ("explanation", "impact"):
        sentences_by_url = {
            url: [
                sentence.strip()
                for sentence in re.split(r"[。！？]", record[field])
                if len(sentence.strip()) >= 18
            ]
            for url, record in ordered_records
        }
        sentence_counts = Counter(
            sentence
            for sentences in sentences_by_url.values()
            for sentence in set(sentences)
        )
        for url, sentences in sentences_by_url.items():
            repeated_chars = sum(
                len(sentence)
                for sentence in sentences
                if sentence_counts[sentence] >= 5
            )
            ratio = repeated_chars / max(1, len(records[url][field]))
            if ratio >= 0.40:
                raise ValueError(
                    f"Repeated-sentence padding in {field} ({ratio:.1%}): {url}"
                )

    for item in japan:
        ai = item.get("ai") if isinstance(item.get("ai"), dict) else {}
        ai = {
            key: value
            for key, value in ai.items()
            if key not in {"summary", "analysis"}
        }
        record = dict(records[item["url"]])
        record["org_zh"] = analyze.normalized_japan_org_zh(
            item, record["org_zh"]
        )
        if item["url"] in TITLE_ZH_OVERRIDES:
            record["title_zh"] = TITLE_ZH_OVERRIDES[item["url"]]
        if item["url"] in ORIGINAL_TITLE_CORRECTIONS:
            item["title"] = ORIGINAL_TITLE_CORRECTIONS[item["url"]]
        item["ai"] = {**ai, **record}

    atomic_write_json(data, DATA_FILE)

    print(
        f"Merged {len(records)} Japan enrichments from {len(part_files)} parts "
        f"into {DATA_FILE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
