"""HONEY 기능 카탈로그·공개 API·챗봇 기능 질의 회귀 테스트.

실행: python tests/test_help_features.py
"""
from __future__ import annotations

import json
import gzip
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SERVER))

TMP = Path(tempfile.mkdtemp(prefix="help_features_test_"))
os.environ["REPORT_DB_PATH"] = str(TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["PRODUCT_INFO_DB_PATH"] = str(TMP / "product_info.db")

from flask import Flask  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402

import help_catalog  # noqa: E402
from chatbot import agent, planner  # noqa: E402
from public_api import URL_PREFIX, register_public_api  # noqa: E402
from report.static_pages import send_html_gzip  # noqa: E402


class HelpHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.anchor_links = []
        self.image_urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        href = values.get("href", "")
        if href.startswith("#"):
            self.anchor_links.append(href[1:])
        if tag == "img" and values.get("src"):
            self.image_urls.append(values["src"])
        if values.get("data-lightbox"):
            self.image_urls.append(values["data-lightbox"])


def check(label, condition):
    assert condition, f"FAIL: {label}"
    print(f"  ok  {label}")


def api_client():
    app = Flask(__name__)
    register_public_api(app)
    return app.test_client()


def test_catalog():
    print("(a) catalog schema")
    check("catalog validation", help_catalog.validate_catalog() == [])
    check("json serializable", bool(json.dumps(help_catalog.FEATURES, ensure_ascii=False)))
    check("status values", {f["status"] for f in help_catalog.FEATURES}
          <= help_catalog.STATUSES)

    html = (SERVER / "report" / "help.html").read_text(encoding="utf-8")
    anchors = set(re.findall(r'\bid="([^"]+)"', html))
    missing = {f["help_anchor"] for f in help_catalog.FEATURES} - anchors
    check("all help anchors exist", not missing)

    exposed_text = help_catalog.normalize(
        json.dumps(help_catalog.FEATURES, ensure_ascii=False) + " " + html)
    forbidden = ("eval" + "analyzer", "ai" + "comment", "db" + "input",
                 "issue" + "signature", "signature", "시그니처")
    check("excluded features absent",
          all(term not in exposed_text for term in forbidden))


def test_help_page():
    print("(b) help page")
    help_path = SERVER / "report" / "help.html"
    html = help_path.read_text(encoding="utf-8")
    parser = HelpHTMLParser()
    parser.feed(html)

    missing_anchors = set(parser.anchor_links) - parser.ids
    check("all internal links resolve", not missing_anchors)

    asset_prefix = "/pe/report/static/help_assets/"
    invalid_urls = {url for url in parser.image_urls if not url.startswith(asset_prefix)}
    missing_assets = {
        url for url in parser.image_urls
        if url.startswith(asset_prefix)
        and not (SERVER / "report" / "static" / "help_assets"
                 / url.removeprefix(asset_prefix)).is_file()
    }
    check("image URLs use the help asset route", not invalid_urls)
    check("all referenced images exist", not missing_assets)
    check("only anonymized current screenshot is exposed",
          set(parser.image_urls) == {asset_prefix + "00b_options.png"})

    app = Flask(__name__)

    @app.get("/pe/report/help")
    def help_page():
        return send_html_gzip(help_path)

    client = app.test_client()
    response = client.get("/pe/report/help", headers={"Accept-Encoding": "gzip"})
    etag = response.headers.get("ETag")
    check("help gzip response", response.status_code == 200
          and response.headers.get("Content-Encoding") == "gzip"
          and gzip.decompress(response.data).startswith(b"<!doctype html>"))
    check("help ETag response", bool(etag)
          and client.get("/pe/report/help", headers={"If-None-Match": etag}).status_code == 304)


def test_api():
    print("(c) public API")
    client = api_client()
    base = f"{URL_PREFIX}/help/features"

    response = client.get(base)
    body = response.get_json()
    check("anonymous full list", response.status_code == 200
          and body["count"] == len(help_catalog.FEATURES))
    check("envelope", set(("schema_version", "catalog_version", "count", "features"))
          <= set(body))
    check("feature schema", set(help_catalog.FEATURE_FIELDS) == set(body["features"][0]))

    cases = (
        ("Temperature 모드 있어?", "temperature-mode"),
        ("Rawdata에서 DUT 제외 가능해?", "rawdata-options"),
        ("Compare에서 Before 여러 개 가능해?", "compare-mode"),
        ("Shmoo 사용할 수 있어?", "characteristic-coming-soon"),
    )
    for query, expected in cases:
        found = client.get(base, query_string={"q": query}).get_json()["features"]
        check(f"search {expected}", bool(found) and found[0]["id"] == expected)

    filtered = client.get(base, query_string={
        "category": "report_tabs", "surface": "web_report", "status": "coming_soon",
    }).get_json()["features"]
    check("combined filters", filtered and all(
        f["category"] == "report_tabs" and "web_report" in f["surfaces"]
        and f["status"] == "coming_soon" for f in filtered))

    empty = client.get(base, query_string={"q": "양자얽힘 기능 있어?"}).get_json()
    check("unknown search", empty["count"] == 0 and empty["help_url"] == "/pe/report/help")
    check("invalid filter 400",
          client.get(base, query_string={"status": "disabled"}).status_code == 400)
    check("long query 400",
          client.get(base, query_string={"q": "가" * 201}).status_code == 400)
    check("GET only", client.post(base).status_code == 405)

    detail = client.get(f"{base}/temperature-mode")
    check("detail", detail.status_code == 200
          and detail.get_json()["features"][0]["id"] == "temperature-mode")
    missing = client.get(f"{base}/not-a-feature")
    check("detail 404", missing.status_code == 404
          and missing.get_json() == {"error": "not_found"})


def test_chatbot():
    print("(d) chatbot feature intent")
    cases = (
        ("Temperature 모드 있어?", "조건부 사용 가능", "temperature-mode"),
        ("Rawdata에서 DUT 제외 가능해?", "원본", "rawdata-options"),
        ("Compare에서 Before 여러 개 가능해?", "source 2개 이상", "compare-mode"),
        ("Shmoo 사용할 수 있어?", "준비 중", "characteristic-coming-soon"),
    )
    for question, phrase, feature_id in cases:
        plan = planner.rule_plan(question)
        check(f"intent {feature_id}", plan.intent == "feature_help")
        result = agent.answer_web(question, viewer="master", use_llm=False)
        check(f"tool isolation {feature_id}",
              [step["tool"] for step in result["steps"]] == ["search_help_features"])
        check(f"answer {feature_id}", phrase in result["text"])
        check(f"help link {feature_id}", result["web"]["links"]
              and result["web"]["links"][0]["url"].startswith("/pe/report/help#"))

    # LLM이 켜진 운영 설정에서도 feature_help는 규칙으로 확정되어 외부 호출하지 않는다.
    with patch.object(planner, "llm_enabled", return_value=True), \
            patch.object(planner, "_call_llm",
                         side_effect=AssertionError("feature query called LLM")):
        isolated = agent.answer_web(cases[0][0], viewer="master", use_llm=True)
    check("feature intent bypasses LLM", isolated["plan"]["intent"] == "feature_help"
          and [step["tool"] for step in isolated["steps"]] == ["search_help_features"])

    unknown = agent.answer_web("양자얽힘 기능 있어?", viewer="master", use_llm=False)
    check("unknown catalog answer", "공개 기능 카탈로그에서 확인되지 않습니다" in unknown["text"])
    check("unknown uses help only",
          [step["tool"] for step in unknown["steps"]] == ["search_help_features"])

    # 기존 이력·화면 이동 질문을 기능 도움으로 빼앗지 않는다.
    check("item query preserved",
          planner.rule_plan("PMIC SOC 에 무슨 Item 있어").intent == "item_search")
    check("map jump preserved", planner.rule_plan("맵 열어줘").intent == "page_jump")

    # 기능 의도 추가와 무관하게 웹 챗 API의 실효 권한 경계는 관리자 404다.
    from report import routes_chat
    guard_app = Flask(__name__)
    with guard_app.test_request_context("/pe/report/api/chat", method="POST",
                                        json={"question": cases[0][0]}), \
            patch.object(routes_chat, "_is_master", return_value=False):
        try:
            routes_chat.api_chat()
            guard_status = None
        except HTTPException as exc:
            guard_status = exc.code
    check("non-admin chat remains 404", guard_status == 404)


def main():
    test_catalog()
    test_help_page()
    test_api()
    test_chatbot()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
