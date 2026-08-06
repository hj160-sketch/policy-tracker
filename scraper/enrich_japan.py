# -*- coding: utf-8 -*-
"""Run resumable DeepSeek enrichment for Japan items in docs/data.json."""
from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import re
import tempfile
import time
from typing import Any

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

import analyze


ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "docs" / "data.json"
USER_AGENT = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; PolicyTracker/1.0; "
        "+https://github.com/)"
    )
}
FETCH_TIMEOUT = int(os.environ.get("JP_FETCH_TIMEOUT", "25"))
PAGE_CHAR_LIMIT = int(os.environ.get("JP_PAGE_CHAR_LIMIT", "6500"))
MAX_RESPONSE_BYTES = int(os.environ.get("JP_MAX_RESPONSE_BYTES", "12000000"))
PDF_PAGE_LIMIT = int(os.environ.get("JP_PDF_PAGE_LIMIT", "40"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("JP_ENRICH_BATCH_SIZE", "5")),
        help="Atomically checkpoint data.json after this many attempted items.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=int(os.environ.get("JP_ENRICH_MAX_ITEMS", "0")),
        help="Maximum pending items to attempt; 0 means every pending Japan item.",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=float(os.environ.get("JP_ENRICH_DELAY", "0.4")),
        help="Polite delay in seconds between official-page requests.",
    )
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.max_items < 0:
        parser.error("--max-items cannot be negative")
    if args.request_delay < 0:
        parser.error("--request-delay cannot be negative")
    return args


def load_data(path: pathlib.Path | None = None) -> dict[str, Any]:
    path = path or DATA_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError(f"{path} must contain an object with an items array")
    return data


def atomic_write_json(
    data: dict[str, Any], path: pathlib.Path | None = None
) -> None:
    """Write a complete sibling temp file, fsync it, then atomically replace."""
    path = path or DATA_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _clean_page_text(value: str) -> str:
    value = re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_official_page_text(
    session: requests.Session, item: dict[str, Any]
) -> tuple[str, str]:
    """Return conservative official-page context and a diagnostic source label."""
    url = str(item.get("url") or "").strip()
    if not url.startswith(("https://", "http://")):
        return "", "invalid-url"
    try:
        with session.get(
            url,
            headers=USER_AGENT,
            timeout=FETCH_TIMEOUT,
            allow_redirects=True,
            stream=True,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                remaining = MAX_RESPONSE_BYTES - total
                if remaining <= 0:
                    break
                chunks.append(chunk[:remaining])
                total += len(chunks[-1])
                if total >= MAX_RESPONSE_BYTES:
                    break
            raw = b"".join(chunks)
    except requests.RequestException as exc:
        return "", f"fetch-error:{type(exc).__name__}"

    if "pdf" in content_type or raw.startswith(b"%PDF"):
        try:
            reader = PdfReader(io.BytesIO(raw))
            parts = []
            for page in reader.pages[:PDF_PAGE_LIMIT]:
                text = _clean_page_text(page.extract_text() or "")
                if text:
                    parts.append(text)
                if sum(len(part) for part in parts) >= PAGE_CHAR_LIMIT:
                    break
            pdf_text = _clean_page_text(" ".join(parts))[:PAGE_CHAR_LIMIT]
            return (
                (pdf_text, "official-pdf")
                if pdf_text
                else ("", "empty-pdf-text")
            )
        except Exception as exc:
            return "", f"pdf-parse-error:{type(exc).__name__}"
    if content_type and not any(
        kind in content_type for kind in ("html", "xml", "text/plain")
    ):
        return "", f"unsupported:{content_type.split(';', 1)[0]}"
    if not raw:
        return "", "empty-response"

    try:
        soup = BeautifulSoup(raw, "lxml")
        for node in soup.select(
            "script, style, noscript, nav, footer, header, form, svg, iframe"
        ):
            node.decompose()
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find(id=re.compile(r"^(main|content|article)", re.I))
            or soup.body
            or soup
        )
        parts: list[str] = []
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description = soup.find("meta", attrs={"name": re.compile("description", re.I)})
        if title:
            parts.append(title)
        if description and description.get("content"):
            parts.append(str(description["content"]))
        parts.append(main.get_text(" ", strip=True))
        text = _clean_page_text(" ".join(parts))
        return text[:PAGE_CHAR_LIMIT], "official-html" if text else "empty-html"
    except Exception as exc:
        return "", f"parse-error:{type(exc).__name__}"


def merge_enrichment(item: dict[str, Any], result: dict[str, str]) -> None:
    ai = item.get("ai")
    if not isinstance(ai, dict):
        ai = {}
    item["ai"] = {**ai, **result}


def main() -> int:
    args = parse_args()
    if not analyze.available():
        raise SystemExit("DEEPSEEK_API_KEY is required")

    data = load_data()
    japan = [item for item in data["items"] if item.get("country") == "jp"]
    all_pending = [
        item for item in japan if not analyze.japan_enrichment_is_compliant(item)
    ]
    pending = all_pending
    if args.max_items:
        pending = pending[: args.max_items]
    print(
        f"[jp-enrich] total={len(japan)} compliant={len(japan) - len(all_pending)} "
        f"attempting={len(pending)} version={analyze.JP_ENRICHMENT_VERSION}"
    )
    if not pending:
        return 0

    session = requests.Session()
    succeeded = failed = attempted_since_checkpoint = 0
    for index, item in enumerate(pending, start=1):
        page_text, page_status = fetch_official_page_text(session, item)
        try:
            result = analyze.analyze_japan_detail(item, page_text=page_text)
            if not result:
                raise ValueError("model output remained non-compliant after retries")
            candidate = dict(item)
            merge_enrichment(candidate, result)
            if not analyze.japan_enrichment_is_compliant(candidate):
                raise ValueError("generated item failed final compliance validation")
            merge_enrichment(item, result)
            succeeded += 1
            print(
                f"[jp-enrich] {index}/{len(pending)} ok "
                f"explanation={len(result['explanation'])} impact={len(result['impact'])} "
                f"page={page_status} url={str(item.get('url') or '')[:90]}"
            )
        except Exception as exc:
            failed += 1
            print(
                f"[jp-enrich] {index}/{len(pending)} FAILED page={page_status} "
                f"url={str(item.get('url') or '')[:90]} error={exc}"
            )
        attempted_since_checkpoint += 1

        if attempted_since_checkpoint >= args.batch_size:
            atomic_write_json(data)
            attempted_since_checkpoint = 0
            print(f"[jp-enrich] checkpoint saved after item {index}")
        if index < len(pending) and args.request_delay:
            time.sleep(args.request_delay)

    if attempted_since_checkpoint:
        atomic_write_json(data)
        print("[jp-enrich] final checkpoint saved")

    remaining = sum(
        1
        for item in data["items"]
        if item.get("country") == "jp"
        and not analyze.japan_enrichment_is_compliant(item)
    )
    print(
        f"[jp-enrich] finished succeeded={succeeded} failed={failed} "
        f"remaining={remaining}"
    )
    # Failures remain unmarked for the next run. The workflow still commits prior
    # checkpoints before surfacing this non-zero status.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
