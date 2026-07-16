"""
company_research.py 的等效 CLI，不開網頁也能用。

用法：
    python3 research_company_cli.py --name "某公司" --url https://example.com --slug some-co
    python3 research_company_cli.py --name "某公司"   # 沒有網址，退回 web_search
"""
from __future__ import annotations

import argparse

import company_research


def main() -> None:
    parser = argparse.ArgumentParser(description="公司背景自動調查 CLI")
    parser.add_argument("--name", required=True)
    parser.add_argument("--url", action="append", default=[], help="可重複指定多個網址")
    parser.add_argument("--slug", default=None)
    args = parser.parse_args()

    slug = args.slug or "".join(c if c.isalnum() else "-" for c in args.name).strip("-").lower()[:24] or "company"
    result = company_research.research_company(args.name, args.url)
    path = company_research.write_company_profile(slug, result["markdown"])

    print(f"寫入：{path}")
    print()
    print("=== 依據素材 ===")
    for src in result["sources"]:
        print(f"- {src}")
    print()
    print("=== 產生內容 ===")
    print(result["markdown"])


if __name__ == "__main__":
    main()
