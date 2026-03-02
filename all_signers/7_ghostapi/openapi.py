from __future__ import annotations

from typing import Any


def build_openapi(analysis: dict[str, Any], title: str = "Captured Website API") -> dict[str, Any]:
    server_url = (analysis.get("summary") or {}).get("server_url") or ""
    paths: dict[str, Any] = {}
    any_auth = False

    for endpoint in analysis.get("endpoints") or []:
        path = endpoint["path_template"]
        method = str(endpoint["method"]).lower()
        operation = _build_operation(endpoint)
        paths.setdefault(path, {})[method] = operation
        any_auth = any_auth or bool(endpoint.get("auth_required"))

    spec: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": title,
            "version": "0.1.0",
            "description": "Generated from authorized HAR capture.",
        },
        "paths": paths,
    }
    if server_url:
        spec["servers"] = [{"url": server_url}]
    if any_auth:
        spec["components"] = {
            "securitySchemes": {
                "cookieAuth": {"type": "apiKey", "in": "cookie", "name": "session"},
                "bearerAuth": {"type": "http", "scheme": "bearer"},
            }
        }
    return spec


def _build_operation(endpoint: dict[str, Any]) -> dict[str, Any]:
    params = []
    path_params = _extract_path_params(endpoint["path_template"])
    for param in path_params:
        params.append(
            {
                "name": param,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        )
    for query in endpoint.get("query_params") or []:
        params.append(
            {
                "name": query,
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            }
        )

    statuses = endpoint.get("statuses") or [200]
    responses = {}
    for status in statuses:
        code = str(status)
        responses[code] = {
            "description": "Captured response",
            "content": {
                "application/json": {
                    "schema": endpoint.get("response_schema") or {"type": "object"},
                }
            },
        }

    operation: dict[str, Any] = {
        "operationId": _operation_id(endpoint),
        "summary": f"{endpoint['method']} {endpoint['path_template']}",
        "responses": responses,
    }
    if params:
        operation["parameters"] = params
    if endpoint.get("request_schema") and endpoint["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
        operation["requestBody"] = {
            "required": False,
            "content": {
                "application/json": {"schema": endpoint["request_schema"]},
            },
        }
    if endpoint.get("auth_required"):
        operation["security"] = [{"cookieAuth": []}, {"bearerAuth": []}]
    return operation


def _extract_path_params(path_template: str) -> list[str]:
    names: list[str] = []
    start = 0
    while True:
        left = path_template.find("{", start)
        if left == -1:
            break
        right = path_template.find("}", left + 1)
        if right == -1:
            break
        names.append(path_template[left + 1 : right])
        start = right + 1
    return names


def _operation_id(endpoint: dict[str, Any]) -> str:
    method = str(endpoint["method"]).lower()
    path = endpoint["path_template"].strip("/")
    parts = [p.strip("{}").replace("-", "_") for p in path.split("/") if p]
    if not parts:
        parts = ["root"]
    return "_".join([method, *parts])
