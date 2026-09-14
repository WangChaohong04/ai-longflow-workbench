#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Live Baidu webpage search probe (no key; may return zero hits).

Usage: python tools/smoke_baidu.py "家用车 对比"
This is an explicit network probe, not an offline acceptance test.
"""
import argparse
from pathlib import Path
import sys

# Direct script execution adds tools/, not the repository root, to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", default="10万以内家用车 对比")
    args = parser.parse_args()
    import httpx
    from longflow.search_provider import BaiduSearchProvider

    with httpx.Client(timeout=20) as client:
        hits = BaiduSearchProvider(client, fetch_content=False).search(args.query, limit=5)
    print(f"hits = {len(hits)}")
    for r in hits:
        print(f"- {r.title}\n    {r.url}\n    {r.snippet[:80]}")
    if not hits:
        raise SystemExit("No results: possible bot challenge, rate limit or network failure; probe not verified.")


if __name__ == "__main__":
    main()
