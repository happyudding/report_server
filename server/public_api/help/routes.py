"""HONEY 기능 카탈로그 공개 조회 — /pe/api/v1/help/features."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import help_catalog

help_bp = Blueprint("public_api_help", __name__)


def _bad_request(message):
    return jsonify({"error": "bad_request", "message": message}), 400


def _envelope(features):
    return {
        "schema_version": help_catalog.SCHEMA_VERSION,
        "catalog_version": help_catalog.CATALOG_VERSION,
        "count": len(features),
        "features": features,
    }


def _filter(name, allowed):
    value = (request.args.get(name) or "").strip()
    if value and value not in allowed:
        return None, _bad_request(
            f"unknown {name}: {value} (allowed: {', '.join(sorted(allowed))})")
    return value or None, None


@help_bp.get("/features")
def features():
    query = (request.args.get("q") or "").strip()
    if len(query) > 200:
        return _bad_request("q is too long (max 200)")
    categories = {feature["category"] for feature in help_catalog.FEATURES}
    surfaces = {surface for feature in help_catalog.FEATURES for surface in feature["surfaces"]}
    category, err = _filter("category", categories)
    if err:
        return err
    surface, err = _filter("surface", surfaces)
    if err:
        return err
    status, err = _filter("status", help_catalog.STATUSES)
    if err:
        return err
    rows = help_catalog.search_features(
        query, category=category, surface=surface, status=status)
    body = _envelope(rows)
    if query and not rows:
        body["help_url"] = "/pe/report/help"
    return jsonify(body)


@help_bp.get("/features/<feature_id>")
def feature_detail(feature_id):
    feature = help_catalog.get_feature(feature_id)
    if not feature:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_envelope([feature]))

