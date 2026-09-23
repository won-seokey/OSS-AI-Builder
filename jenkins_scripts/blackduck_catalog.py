import difflib
import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:
    raise RuntimeError("PyYAML is required for blackduck catalog support") from exc


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_oem(value: str) -> str:
    text = _clean_text(value).upper()
    return " ".join(text.split())


def normalize_key(value: str) -> str:
    text = _clean_text(value).lower()
    return " ".join(text.split())


def _normalize_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for e in entries:
        oem = normalize_oem(_clean_text(e.get("oem")))
        project = _clean_text(e.get("project"))
        if not oem or not project:
            continue

        aliases_raw = e.get("aliases") or []
        aliases: list[str] = []
        if isinstance(aliases_raw, list):
            for a in aliases_raw:
                s = _clean_text(a)
                if s:
                    aliases.append(s)

        key = (oem, project.lower())
        if key in seen:
            continue
        seen.add(key)
        version_by_unit: dict[str, str] = {}
        ver_raw = e.get("version_by_unit") or e.get("versions") or {}
        if isinstance(ver_raw, dict):
            for k, v in ver_raw.items():
                k_norm = _clean_text(k).upper()
                v_norm = _clean_text(v)
                if k_norm in {"APPL", "FBL"} and v_norm:
                    version_by_unit[k_norm] = v_norm

        out.append(
            {
                "oem": oem,
                "project": project,
                "aliases": aliases,
                "version_by_unit": version_by_unit,
            }
        )

    return out


def parse_catalog_schema(data: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    projects = data.get("projects")
    if isinstance(projects, list):
        for p in projects:
            if isinstance(p, dict):
                entries.append(
                    {
                        "oem": p.get("oem"),
                        "project": p.get("project") or p.get("name"),
                        "aliases": p.get("aliases") or [],
                        "version_by_unit": p.get("version_by_unit") or p.get("versions") or {},
                    }
                )

    groups = data.get("groups")
    if isinstance(groups, list):
        for g in groups:
            if not isinstance(g, dict):
                continue
            oem = g.get("oem") or g.get("name") or g.get("group")
            plist = g.get("projects")
            if not isinstance(plist, list):
                continue
            for p in plist:
                if isinstance(p, str):
                    entries.append({"oem": oem, "project": p, "aliases": []})
                elif isinstance(p, dict):
                    entries.append(
                        {
                            "oem": oem,
                            "project": p.get("project") or p.get("name"),
                            "aliases": p.get("aliases") or [],
                            "version_by_unit": p.get("version_by_unit") or p.get("versions") or {},
                        }
                    )

    return _normalize_entries(entries)


def load_catalog_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        return []

    return parse_catalog_schema(data)


def save_catalog_file(path: Path, entries: list[dict[str, Any]]) -> None:
    payload = {"projects": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)


def fetch_catalog_from_api(
    api_url: str,
    api_token: str,
    ssl_context: ssl.SSLContext,
    timeout_sec: int = 20,
) -> list[dict[str, Any]]:
    req = urllib.request.Request(
        url=api_url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        method="GET",
    )

    with urllib.request.urlopen(req, timeout=timeout_sec, context=ssl_context) as resp:
        body = resp.read().decode("utf-8")

    data = json.loads(body)
    if isinstance(data, dict):
        return parse_catalog_schema(data)
    if isinstance(data, list):
        return _normalize_entries([
            {
                "oem": x.get("oem") if isinstance(x, dict) else None,
                "project": (x.get("project") or x.get("name")) if isinstance(x, dict) else None,
                "aliases": x.get("aliases") if isinstance(x, dict) else [],
                "version_by_unit": (x.get("version_by_unit") or x.get("versions") or {}) if isinstance(x, dict) else {},
            }
            for x in data
        ])
    return []


def load_catalog(
    catalog_path: Path,
    cache_path: Path | None = None,
    api_url: str | None = None,
    api_token: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> list[dict[str, Any]]:
    # Priority: API -> cache -> local file.
    if api_url and api_token and ssl_context:
        try:
            fetched = fetch_catalog_from_api(api_url, api_token, ssl_context)
            if fetched:
                if cache_path:
                    save_catalog_file(cache_path, fetched)
                return fetched
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            pass

    if cache_path and cache_path.exists():
        cached = load_catalog_file(cache_path)
        if cached:
            return cached

    return load_catalog_file(catalog_path)


def build_catalog_index(entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_project: dict[str, dict[str, Any]] = {}
    alias_to_project: dict[str, str] = {}
    alias_to_projects: dict[str, list[str]] = {}
    by_oem: dict[str, list[str]] = {}

    for e in entries:
        oem = normalize_oem(e["oem"])
        project = e["project"]
        p_key = normalize_key(project)

        by_project[p_key] = e
        by_oem.setdefault(oem, []).append(project)

        for alias in e.get("aliases", []):
            a_key = normalize_key(alias)
            if a_key:
                alias_to_project[a_key] = project
                alias_to_projects.setdefault(a_key, []).append(project)

    return {
        "entries": entries,
        "by_project": by_project,
        "alias_to_project": alias_to_project,
        "alias_to_projects": alias_to_projects,
        "by_oem": by_oem,
    }


def resolve_project_candidate(
    index: dict[str, Any],
    oem: str,
    candidate: str,
    limit: int = 5,
) -> tuple[str | None, list[str]]:
    oem_key = normalize_oem(oem)
    candidate_key = normalize_key(candidate)

    by_project = index.get("by_project", {})
    alias_to_project = index.get("alias_to_project", {})

    if candidate_key in by_project:
        entry = by_project[candidate_key]
        if normalize_oem(entry["oem"]) == oem_key:
            return entry["project"], []

    alias_projects = index.get("alias_to_projects", {}).get(candidate_key, [])
    if not alias_projects and candidate_key in alias_to_project:
        alias_projects = [alias_to_project[candidate_key]]

    for p_name in alias_projects:
        p_entry = by_project.get(normalize_key(p_name))
        if p_entry and normalize_oem(p_entry["oem"]) == oem_key:
            return p_entry["project"], []

    if len(alias_projects) == 1:
        p_entry = by_project.get(normalize_key(alias_projects[0]))
        if p_entry:
            return p_entry["project"], []

    project_pool = index.get("by_oem", {}).get(oem_key, [])
    if not project_pool:
        # OEM not found, fallback to all projects for suggestion only.
        project_pool = [e["project"] for e in index.get("entries", [])]

    suggestions = difflib.get_close_matches(candidate, project_pool, n=limit, cutoff=0.3)

    if not suggestions and candidate_key:
        # Additional alias fuzzy check.
        alias_candidates = list(alias_to_project.keys())
        alias_hits = difflib.get_close_matches(candidate_key, alias_candidates, n=limit, cutoff=0.3)
        mapped = []
        for a in alias_hits:
            p = alias_to_project.get(a)
            if p and p not in mapped:
                mapped.append(p)
        suggestions = mapped

    return None, suggestions


def get_project_unit_version(index: dict[str, Any], project_name: str, unit_name: str) -> str:
    entry = index.get("by_project", {}).get(normalize_key(project_name))
    if not entry:
        return ""
    unit_key = normalize_oem(unit_name)
    unit_key = "APPL" if unit_key.startswith("APPL") else ("FBL" if unit_key.startswith("FBL") else unit_key)
    ver_map = entry.get("version_by_unit") or {}
    return _clean_text(ver_map.get(unit_key, ""))
