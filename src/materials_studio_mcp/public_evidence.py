"""Constrained, metadata-only public evidence lookup for model intake.

This is deliberately narrower than a web browser or downloader.  It only
calls two fixed HTTPS APIs, returns a small normalized metadata record, and
never downloads structures, force fields, scripts, or executable content.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener


_PROVIDERS = frozenset({"pubchem", "crossref"})
_MAX_RESULTS = 5
_MAX_RESPONSE_BYTES = 256 * 1024
_QUERY = re.compile(r"^[^\x00-\x1f]{2,120}$")
_PUBCHEM_HOST = "pubchem.ncbi.nlm.nih.gov"
_CROSSREF_HOST = "api.crossref.org"


class _RejectRedirects(HTTPRedirectHandler):
    """Prevent a provider response from redirecting this narrow client elsewhere."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _validate_query(query: str, provider: str, max_results: int) -> tuple[str, str, int]:
    if provider not in _PROVIDERS:
        raise ValueError(f"provider must be one of {sorted(_PROVIDERS)}")
    if not isinstance(query, str) or _QUERY.fullmatch(query.strip()) is None:
        raise ValueError("query must contain 2 to 120 printable characters")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= _MAX_RESULTS:
        raise ValueError(f"max_results must be an integer from 1 to {_MAX_RESULTS}")
    return query.strip(), provider, max_results


def build_public_evidence_request(query: str, provider: str, max_results: int = 3) -> dict[str, Any]:
    """Validate an external metadata request and return its fixed-endpoint plan."""

    query, provider, max_results = _validate_query(query, provider, max_results)
    if provider == "pubchem":
        encoded = quote(query, safe="")
        url = (
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
            f"{encoded}/property/MolecularFormula,MolecularWeight,CanonicalSMILES,IsomericSMILES,InChI,InChIKey/JSON"
        )
        expected_host = _PUBCHEM_HOST
        purpose = "compound_identity_metadata"
    else:
        params = urlencode({
            "query.bibliographic": query,
            "rows": str(max_results),
            "select": "DOI,title,author,published,container-title,URL,type,score",
        })
        url = f"https://api.crossref.org/works?{params}"
        expected_host = _CROSSREF_HOST
        purpose = "literature_metadata"
    return {
        "provider": provider,
        "purpose": purpose,
        "source_url": url,
        "expected_host": expected_host,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest().upper(),
        "max_results": max_results,
        "request_constraints": {
            "https_only": True,
            "fixed_provider_host": True,
            "redirects_allowed": False,
            "max_response_bytes": _MAX_RESPONSE_BYTES,
            "download_or_execution": "prohibited",
        },
    }


def _open_provider_json(url: str, expected_host: str) -> dict[str, Any]:
    """Fetch one bounded JSON response from an allowlisted HTTPS provider."""

    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != expected_host or parsed.username or parsed.password:
        raise PermissionError("Public evidence requests must use the fixed HTTPS provider endpoint")
    opener = build_opener(
        HTTPSHandler(context=ssl.create_default_context()),
        _RejectRedirects(),
    )
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "materials-studio-mcp/1.0 evidence-metadata"})
    try:
        with opener.open(request, timeout=10.0) as response:
            final = urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != expected_host:
                raise PermissionError("Public evidence provider attempted to leave its approved HTTPS host")
            content_length = response.headers.get("Content-Length")
            if content_length and (not content_length.isdigit() or int(content_length) > _MAX_RESPONSE_BYTES):
                raise ValueError("Public evidence response exceeds the size limit")
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code == 404:
            return {"_not_found": True}
        raise RuntimeError(f"Public evidence provider returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("Public evidence provider is unavailable") from exc
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise ValueError("Public evidence response exceeds the size limit")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Public evidence provider returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Public evidence provider returned an invalid JSON object")
    return decoded


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _pubchem_records(payload: dict[str, Any], source_url: str, max_results: int) -> list[dict[str, Any]]:
    if payload.get("_not_found"):
        return []
    properties = payload.get("PropertyTable", {}).get("Properties", [])
    if not isinstance(properties, list):
        raise ValueError("PubChem response does not contain a property list")
    records: list[dict[str, Any]] = []
    for item in properties[:max_results]:
        if not isinstance(item, dict) or not isinstance(item.get("CID"), int):
            continue
        record = {
            "cid": item["CID"],
            "molecular_formula": _string(item.get("MolecularFormula")),
            "molecular_weight": item.get("MolecularWeight") if isinstance(item.get("MolecularWeight"), (int, float)) else None,
            "canonical_smiles": _string(item.get("ConnectivitySMILES")) or _string(item.get("CanonicalSMILES")),
            "isomeric_smiles": _string(item.get("SMILES")) or _string(item.get("IsomericSMILES")),
            "inchi": _string(item.get("InChI")),
            "inchikey": _string(item.get("InChIKey")),
            "candidate_sdf_url": f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{item['CID']}/record/SDF?record_type=3d",
            "source_url": source_url,
        }
        records.append(record)
    return records


def _crossref_records(payload: dict[str, Any], source_url: str, max_results: int) -> list[dict[str, Any]]:
    if payload.get("_not_found"):
        return []
    items = payload.get("message", {}).get("items", [])
    if not isinstance(items, list):
        raise ValueError("Crossref response does not contain a works list")
    records: list[dict[str, Any]] = []
    for item in items[:max_results]:
        if not isinstance(item, dict):
            continue
        title = item.get("title", [])
        container = item.get("container-title", [])
        published = item.get("published", {}).get("date-parts", [])
        year = None
        if isinstance(published, list) and published and isinstance(published[0], list) and published[0]:
            year = published[0][0] if isinstance(published[0][0], int) else None
        authors = []
        for author in item.get("author", []) if isinstance(item.get("author"), list) else []:
            if isinstance(author, dict):
                display = " ".join(part for part in (author.get("given"), author.get("family")) if isinstance(part, str))
                if display:
                    authors.append(display)
        records.append({
            "doi": _string(item.get("DOI")),
            "title": title[0] if isinstance(title, list) and title and isinstance(title[0], str) else None,
            "container_title": container[0] if isinstance(container, list) and container and isinstance(container[0], str) else None,
            "published_year": year,
            "type": _string(item.get("type")),
            "score": item.get("score") if isinstance(item.get("score"), (int, float)) else None,
            "authors": authors[:10],
            "source_url": _string(item.get("URL")) or source_url,
        })
    return records


def search_public_model_evidence(query: str, provider: str, max_results: int = 3) -> dict[str, Any]:
    """Query fixed public metadata endpoints; no writes or downloads are performed."""

    plan = build_public_evidence_request(query, provider, max_results)
    payload = _open_provider_json(plan["source_url"], plan["expected_host"])
    records = (
        _pubchem_records(payload, plan["source_url"], plan["max_results"])
        if plan["provider"] == "pubchem"
        else _crossref_records(payload, plan["source_url"], plan["max_results"])
    )
    return {
        "schema_version": 1,
        "provider": plan["provider"],
        "purpose": plan["purpose"],
        "query_sha256": plan["query_sha256"],
        "source_url": plan["source_url"],
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records": records,
        "evidence_level": "public_metadata_candidate_only",
        "scientific_limitations": [
            "Returned metadata does not establish a force field, partial charges, crystal structure, simulation protocol, or scientific validity.",
            "Candidate SDF URLs are references only; this tool does not download them.",
            "The response must be independently reviewed and registered before use in a controlled write or calculation workflow.",
        ],
        "network_receipt": {
            "https_only": True,
            "redirects_allowed": False,
            "download_or_execution": "not_performed",
            "max_response_bytes": _MAX_RESPONSE_BYTES,
        },
    }
