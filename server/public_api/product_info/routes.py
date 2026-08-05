"""기준정보(product_info) 조회 라우트 — /pe/api/v1/product-info.

읽기 전용 GET 만 둔다. 서버의 product_info.db 를 읽는 로더에 위임할 뿐이라 부하는
메모리 dict 조회 수준이다.

`from product_info import ...` 는 이 패키지(public_api.product_info)가 아니라
**server/product_info.py 최상위 모듈**을 가리킨다 (server/ 가 sys.path 루트, 절대 import).

에러 규약(공개 API 공통):
  400 {"error": "bad_request", "message": ...}
  404 {"error": "not_found"}
  500 은 여기서 잡지 않는다 — ops.init_ops 의 전역 핸들러가 처리한다.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from product_info import lookup as product_info_lookup, list_search_candidates

product_info_bp = Blueprint("public_api_product_info", __name__)


def _bad_request(message):
    return jsonify({"error": "bad_request", "message": message}), 400


def _required(name):
    """쿼리 파라미터 필수값 → (값, None) 또는 (None, 400 응답)."""
    value = (request.args.get(name) or "").strip()
    if not value:
        return None, _bad_request(f"{name} is required")
    return value, None


@product_info_bp.get("/candidates")
def candidates():
    """기준정보 검색 후보(part_id + sub_part_id flatten). DB 부재 시 빈 목록 200."""
    items = list_search_candidates()
    return jsonify({"candidates": items, "count": len(items)})


@product_info_bp.get("/lookup")
def lookup():
    """part_id/sub_part_id → 기준정보 14컬럼. 미매칭이면 404."""
    part_id, err = _required("part_id")
    if err:
        return err
    info = product_info_lookup(part_id)
    if not info:
        return jsonify({"error": "not_found"}), 404
    return jsonify(info)
