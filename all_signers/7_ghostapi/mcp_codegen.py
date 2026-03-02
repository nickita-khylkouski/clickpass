from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent
from typing import Any

from .models import SiteAnalysis


def generate_mcp_server(
    analysis: dict[str, Any] | SiteAnalysis,
    output_path_or_server_name: str | None = None,
    *,
    server_name: str | None = None,
) -> str:
    """Generate MCP server scaffold.

    Backward compatibility:
    - `generate_mcp_server(site_analysis, "/tmp/server.py")` writes file.
    - `generate_mcp_server(analysis_dict, server_name="captured_site")` returns code string.
    - `generate_mcp_server(analysis_dict, "captured_site")` treats arg as server_name.
    """
    analysis_dict = _analysis_to_dict(analysis)

    out_path: Path | None = None
    inferred_server_name = server_name

    if output_path_or_server_name:
        if output_path_or_server_name.endswith(".py") or "/" in output_path_or_server_name:
            out_path = Path(output_path_or_server_name)
        else:
            inferred_server_name = output_path_or_server_name

    code = _generate_code(analysis_dict, server_name=inferred_server_name or "captured_site")
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(code, encoding="utf-8")
    return code


def _analysis_to_dict(analysis: dict[str, Any] | SiteAnalysis) -> dict[str, Any]:
    if isinstance(analysis, SiteAnalysis):
        return {
            "summary": {
                "server_url": analysis.base_url,
                "entries_seen": analysis.summary.get("entries_seen", 0),
                "endpoints_found": len(analysis.endpoints),
            },
            "endpoints": [
                {
                    "method": endpoint.method,
                    "host": endpoint.host,
                    "path_template": endpoint.path,
                    "sample_url": endpoint.sample_url,
                    "query_params": endpoint.query_params,
                    "auth_required": endpoint.auth_required,
                    "statuses": endpoint.statuses,
                    "request_schema": endpoint.request_schema,
                    "response_schema": endpoint.response_schema,
                }
                for endpoint in analysis.endpoints
            ],
        }
    return analysis


def _generate_code(analysis: dict[str, Any], server_name: str) -> str:
    blocks = [_tool_block(ep) for ep in analysis.get("endpoints") or []]
    tools = "\n\n".join(blocks) if blocks else "# No endpoints discovered."
    base_url = (analysis.get("summary") or {}).get("server_url", "")

    return dedent(
        f'''\
        """
        Generated MCP scaffold from authorized HAR capture.
        Review auth/session behavior before use.
        """
        from __future__ import annotations

        import json
        import os
        import urllib.error
        import urllib.parse
        import urllib.request
        from typing import Any

        from mcp.server.fastmcp import FastMCP

        BASE_URL = {base_url!r}
        AUTH_HEADER = os.environ.get("GHOSTAPI_AUTH_HEADER", "").strip()
        COOKIE_HEADER = os.environ.get("GHOSTAPI_COOKIE", "").strip()
        mcp = FastMCP({server_name!r})


        def _request(
            method: str,
            path: str,
            query: dict[str, Any] | None = None,
            payload: dict[str, Any] | None = None,
            auth_header: str | None = None,
            cookie: str | None = None,
        ) -> dict[str, Any]:
            query = query or {{}}
            payload = payload or {{}}
            url = BASE_URL.rstrip("/") + path
            if query:
                url += "?" + urllib.parse.urlencode(query, doseq=True)

            data = None
            headers = {{"Accept": "application/json"}}
            auth_value = auth_header if auth_header is not None else AUTH_HEADER
            cookie_value = cookie if cookie is not None else COOKIE_HEADER
            if auth_value:
                headers["Authorization"] = auth_value
            if cookie_value:
                headers["Cookie"] = cookie_value
            if method in {{"POST", "PUT", "PATCH", "DELETE"}} and payload:
                data = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"

            req = urllib.request.Request(url=url, method=method, data=data, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    if "json" in (response.headers.get("content-type") or "").lower():
                        return json.loads(body)
                    return {{"raw": body}}
            except urllib.error.HTTPError as exc:
                return {{"error": f"HTTP {{exc.code}}", "body": exc.read().decode("utf-8", errors="replace")}}


        {tools}


        if __name__ == "__main__":
            mcp.run()
        '''
    ).rstrip() + "\n"


def _tool_block(endpoint: dict[str, Any]) -> str:
    method = endpoint["method"]
    path = endpoint["path_template"]
    name = _tool_name(method, path)

    path_args = re.findall(r"\{([^{}]+)\}", path)
    params = [f"{arg}: str" for arg in path_args]
    params.append("query: dict[str, Any] | None = None")
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        params.append("payload: dict[str, Any] | None = None")
    params.append("auth_header: str | None = None")
    params.append("cookie: str | None = None")
    signature = ", ".join(params)

    payload_arg = ", payload=payload" if method in {"POST", "PUT", "PATCH", "DELETE"} else ""
    doc = f"{method} {path} (auth_required={bool(endpoint.get('auth_required'))})"
    return dedent(
        f"""\
        @mcp.tool(name={name!r})
        def {name}({signature}) -> dict[str, Any]:
            \"\"\"{doc}\"\"\"
            return _request(
                {method!r},
                f{path!r},
                query=query{payload_arg},
                auth_header=auth_header,
                cookie=cookie,
            )
        """
    ).rstrip()


def _tool_name(method: str, path: str) -> str:
    base = method.lower() + "_" + "_".join(segment.strip("{}") for segment in path.strip("/").split("/") if segment)
    base = re.sub(r"[^a-zA-Z0-9_]", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    return base or f"{method.lower()}_root"
