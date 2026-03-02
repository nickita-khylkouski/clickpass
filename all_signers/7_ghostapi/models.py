from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EndpointRecord:
    method: str
    host: str
    path_template: str
    sample_url: str
    calls: int = 0
    auth_required: bool = False
    query_params: set[str] = field(default_factory=set)
    statuses: set[int] = field(default_factory=set)
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None

    @property
    def operation_key(self) -> str:
        return f"{self.method} {self.path_template}"


@dataclass(frozen=True)
class Endpoint:
    method: str
    url: str
    path: str
    source: str
    status: Optional[int] = None
    content_type: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def key(self) -> str:
        return f"{self.method.upper()} {self.path}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Finding:
    severity: str
    title: str
    description: str
    source: str
    endpoint: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AuthSummary:
    auth_type: str = "none"
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "AuthSummary":
        return cls(
            auth_type=str(payload.get("auth_type", "none")),
            notes=str(payload.get("notes", "")),
        )


@dataclass
class ApiEndpoint:
    method: str
    path: str
    host: str
    sample_url: str
    query_params: List[str] = field(default_factory=list)
    auth_required: bool = False
    statuses: List[int] = field(default_factory=list)
    request_schema: Dict[str, Any] | None = None
    response_schema: Dict[str, Any] | None = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ApiEndpoint":
        return cls(
            method=str(payload.get("method", "GET")).upper(),
            path=str(payload.get("path", payload.get("path_template", "/"))),
            host=str(payload.get("host", "")),
            sample_url=str(payload.get("sample_url", "")),
            query_params=[str(x) for x in payload.get("query_params", [])],
            auth_required=bool(payload.get("auth_required", False)),
            statuses=[int(x) for x in payload.get("statuses", []) if isinstance(x, int) or str(x).isdigit()],
            request_schema=payload.get("request_schema"),
            response_schema=payload.get("response_schema"),
        )


@dataclass
class SiteAnalysis:
    site: str
    base_url: str
    auth: AuthSummary
    endpoints: List[ApiEndpoint]
    summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "site": self.site,
            "base_url": self.base_url,
            "auth": self.auth.to_dict(),
            "summary": dict(self.summary),
            "endpoints": [ep.to_dict() for ep in self.endpoints],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SiteAnalysis":
        auth_payload = payload.get("auth", {}) if isinstance(payload.get("auth"), dict) else {}
        return cls(
            site=str(payload.get("site", "")),
            base_url=str(payload.get("base_url", "")),
            auth=AuthSummary.from_dict(auth_payload),
            endpoints=[ApiEndpoint.from_dict(ep) for ep in payload.get("endpoints", []) if isinstance(ep, dict)],
            summary=payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {},
        )
