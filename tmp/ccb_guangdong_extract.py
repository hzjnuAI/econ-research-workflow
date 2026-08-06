#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Temporary extractor: CCB business outlets in Guangdong, excluding Shenzhen.

Source scope: Cngold public branch-only directory. The script validates each city's
parsed row count against the directory's own advertised total.
"""

from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CITIES = {
    326: "广州市",
    # 327: 深圳市（明确排除）
    328: "珠海市",
    329: "汕头市",
    330: "东莞市",
    331: "中山市",
    332: "佛山市",
    333: "韶关市",
    334: "江门市",
    335: "湛江市",
    336: "茂名市",
    337: "肇庆市",
    338: "惠州市",
    339: "梅州市",
    340: "汕尾市",
    341: "河源市",
    342: "阳江市",
    343: "清远市",
    344: "潮州市",
    345: "揭阳市",
    346: "云浮市",
}

BASE = "https://bank.cngold.org/yhwd/bank_9_20_{city_id}_0{suffix}.html"
OUT = Path("ccb_output")
OUT.mkdir(exist_ok=True)

session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def page_url(city_id: int, page: int) -> str:
    suffix = "" if page == 1 else f"_{page}"
    return BASE.format(city_id=city_id, suffix=suffix)


def fetch(url: str, attempts: int = 5) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "utf-8"
            if "网点地址" not in response.text and "营业厅网点" not in response.text:
                raise RuntimeError("response does not look like a branch directory page")
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def parse_meta(html: str) -> tuple[int, int]:
    soup = BeautifulSoup(html, "html.parser")
    text = clean(soup.get_text(" ", strip=True))
    match = re.search(r"共\s*(\d+)\s*页\s*(\d+)\s*条数据", text)
    if not match:
        count_match = re.search(r"营业厅网点[（(]\s*(\d+)\s*[）)]", text)
        if not count_match:
            raise RuntimeError("could not parse page count / branch count")
        expected = int(count_match.group(1))
        pages = max(1, (expected + 14) // 15)
        return pages, expected
    return int(match.group(1)), int(match.group(2))


def parse_page(html: str, city: str, url: str, page: int) -> list[dict[str, str | int]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, str | int]] = []

    for h3 in soup.find_all("h3"):
        name = clean(h3.get_text(" ", strip=True))
        if not name.startswith("建设银行"):
            continue
        if "ATM" in name.upper() or "自动取款" in name or "网点查询" in name:
            continue

        container = None
        for parent in h3.parents:
            if getattr(parent, "name", None) in {"li", "div", "dd", "section"}:
                parent_text = clean(parent.get_text(" ", strip=True))
                if "网点地址：" in parent_text and len(parent_text) <= 1200:
                    container = parent
                    break
        if container is None:
            continue

        item_text = clean(container.get_text(" ", strip=True))
        address_match = re.search(r"网点地址：\s*(.*)$", item_text)
        if not address_match:
            continue
        address = clean(address_match.group(1))
        # Remove any accidental pagination / neighboring-section tail.
        address = re.split(r"\s+(?:首页|上一页|下一页|末页|广东省.+附近中国建设银行网点)\b", address)[0]

        phone_match = re.search(r"电话：\s*(.*?)\s*(?=建设银行)", item_text)
        phone = clean(phone_match.group(1)) if phone_match else ""
        if phone in {"", "暂无"}:
            phone = "暂无"

        rows.append(
            {
                "省份": "广东省",
                "地市": city,
                "网点名称": name,
                "地址": address,
                "电话": phone,
                "网点类型": "营业厅",
                "来源页面": url,
                "页码": page,
            }
        )

    # Fallback: text-stream parser if HTML nesting changes.
    if not rows:
        tokens = [clean(x) for x in soup.stripped_strings]
        for i, token in enumerate(tokens):
            if not token.startswith("建设银行") or "ATM" in token.upper() or "网点查询" in token:
                continue
            address = ""
            phone = "暂无"
            for j in range(max(0, i - 4), min(len(tokens), i + 12)):
                if tokens[j].startswith("电话："):
                    phone = clean(tokens[j].replace("电话：", "", 1)) or "暂无"
                if tokens[j].startswith("网点地址："):
                    address = clean(tokens[j].replace("网点地址：", "", 1))
                    break
            if address:
                rows.append(
                    {
                        "省份": "广东省",
                        "地市": city,
                        "网点名称": token,
                        "地址": address,
                        "电话": phone,
                        "网点类型": "营业厅",
                        "来源页面": url,
                        "页码": page,
                    }
                )

    # Stable de-duplication within a page.
    deduped: list[dict[str, str | int]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row["网点名称"]), str(row["地址"]))
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    return deduped


def main() -> None:
    all_rows: list[dict[str, str | int]] = []
    summary: list[dict[str, object]] = []
    errors: list[str] = []

    for city_id, city in CITIES.items():
        first_url = page_url(city_id, 1)
        try:
            first_html = fetch(first_url)
            pages, expected = parse_meta(first_html)
            city_rows = parse_page(first_html, city, first_url, 1)

            for page in range(2, pages + 1):
                url = page_url(city_id, page)
                html = fetch(url)
                city_rows.extend(parse_page(html, city, url, page))
                time.sleep(0.25)

            # City-level stable de-duplication.
            unique_rows: list[dict[str, str | int]] = []
            seen: set[tuple[str, str]] = set()
            for row in city_rows:
                key = (str(row["网点名称"]), str(row["地址"]))
                if key not in seen:
                    seen.add(key)
                    unique_rows.append(row)

            actual = len(unique_rows)
            ok = actual == expected
            summary.append(
                {
                    "城市ID": city_id,
                    "地市": city,
                    "应有条数": expected,
                    "实际条数": actual,
                    "页数": pages,
                    "覆盖率": round(actual / expected, 6) if expected else 1.0,
                    "校验结果": "PASS" if ok else "FAIL",
                }
            )
            all_rows.extend(unique_rows)
            print(f"{city}: {actual}/{expected}, pages={pages}, {'PASS' if ok else 'FAIL'}")
            if not ok:
                errors.append(f"{city}: expected {expected}, parsed {actual}")
        except Exception as exc:  # noqa: BLE001
            message = f"{city}: {type(exc).__name__}: {exc}"
            errors.append(message)
            summary.append(
                {
                    "城市ID": city_id,
                    "地市": city,
                    "应有条数": None,
                    "实际条数": 0,
                    "页数": None,
                    "覆盖率": 0,
                    "校验结果": "ERROR",
                    "错误": message,
                }
            )
            print(message)

    fieldnames = ["省份", "地市", "网点名称", "地址", "电话", "网点类型", "来源页面", "页码"]
    with (OUT / "ccb_guangdong_branches_raw.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    payload = {
        "source": "https://bank.cngold.org/yhwd/",
        "scope": "CCB business outlets in Guangdong province, excluding Shenzhen",
        "cities": summary,
        "total_expected": sum(x["应有条数"] or 0 for x in summary),
        "total_actual": len(all_rows),
        "all_pass": not errors and all(x["校验结果"] == "PASS" for x in summary),
        "errors": errors,
    }
    (OUT / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
