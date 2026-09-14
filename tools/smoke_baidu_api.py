#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Explicit live Baidu-compatible API probe; requires a real endpoint and key.

Set LONGFLOW_SEARCH_BAIDU_KEY and LONGFLOW_SEARCH_ENDPOINT (or
LONGFLOW_SEARCH_BAIDU_ENDPOINT), optionally LONGFLOW_SEARCH_BAIDU_HEADER.
Usage: python tools/smoke_baidu_api.py "家用车 对比"
No endpoint is invented and zero hits do not count as a successful probe.
"""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", default="10万以内家用车 对比")
    args = parser.parse_args()
    key = os.environ.get("LONGFLOW_SEARCH_BAIDU_KEY", "")
    endpoint = os.environ.get("LONGFLOW_SEARCH_ENDPOINT") or os.environ.get("LONGFLOW_SEARCH_BAIDU_ENDPOINT", "")
    if not key or not endpoint:
        raise SystemExit("Set LONGFLOW_SEARCH_BAIDU_KEY and a real LONGFLOW_SEARCH_ENDPOINT before running this live probe.")
    import httpx
    from longflow.search_provider import BaiduApiSearchProvider

    with httpx.Client(timeout=20) as client:
        prov = BaiduApiSearchProvider(client, key, endpoint,
                                     auth_header=os.environ.get("LONGFLOW_SEARCH_BAIDU_HEADER", "Authorization"),
                                     fetch_content=False)
        hits = prov.search(args.query, limit=5)
    print(f"hits = {len(hits)}")
    for r in hits:
        print(f"- {r.title}  ({r.published_at or '?'})\n    {r.url}\n    {r.snippet[:80]}")
    if not hits:
        raise SystemExit("No results: check endpoint, credentials, quota and response format; probe not verified.")


if __name__ == "__main__":
    main()
