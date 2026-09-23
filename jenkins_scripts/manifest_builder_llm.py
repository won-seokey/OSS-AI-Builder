import argparse
import getpass
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed. Please install with: pip install pyyaml")
    sys.exit(1)

from manifest_builder import (
    auto_normalize_checkout_commands,
    expand_scan_paths,
    looks_like_checkout_command,
    normalize_checkout_line,
    normalize_scan_path_line,
    strip_markdown_link,
)
from blackduck_catalog import (
    build_catalog_index,
    get_project_unit_version,
    load_catalog,
    normalize_key,
    normalize_oem,
    resolve_project_candidate,
)
from manifest_validator import ManifestValidator, render_text_report


DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "manifest_system_prompt.md"


class OpenAIRequestError(RuntimeError):
    pass


class CatalogMappingError(RuntimeError):
    pass


def load_system_prompt(path: Path) -> str:
    try:
        prompt = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"system prompt file could not be read: {path}") from exc
    if not prompt:
        raise RuntimeError(f"system prompt file is empty: {path}")
    return prompt


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(description="LLM-assisted builder for Black Duck manifest YAML")
    parser.add_argument(
        "--input-text-file",
        help="path to a text file containing free-form Jira request text",
    )
    parser.add_argument(
        "--output",
        default="generated_manifest_llm.yaml",
        help="output yaml path (default: generated_manifest_llm.yaml)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        help="OpenAI-compatible model name (default: gpt-5.6-luna)",
    )
    parser.add_argument(
        "--system-prompt-path",
        default=str(DEFAULT_SYSTEM_PROMPT_PATH),
        help="path to the system prompt markdown file",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=os.environ.get("OPENAI_REASONING_EFFORT", "xhigh"),
        choices=("none", "low", "medium", "high", "xhigh"),
        help="Reasoning effort for supported models (default: xhigh)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        help="OpenAI-compatible API base URL",
    )
    parser.add_argument(
        "--api-key",
        help="OpenAI API key (or set OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--strict-wiki",
        action="store_true",
        help="run validator in strict-wiki core mode after generation",
    )
    parser.add_argument(
        "--strict-wiki-full",
        action="store_true",
        help="run validator in strict-wiki full mode after generation",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="skip post-generation validation",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="optional OpenAI sampling temperature; omitted by default so the model chooses its supported default",
    )
    parser.add_argument(
        "--ca-bundle",
        help="path to custom CA bundle PEM file for HTTPS verification",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification for OpenAI API (default behavior)",
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        help="enable default TLS certificate verification",
    )
    parser.add_argument(
        "--bd-catalog",
        default=str(script_dir / "blackduck_catalog.yaml"),
        help="path to Black Duck project catalog yaml",
    )
    parser.add_argument(
        "--bd-catalog-cache",
        default=str(script_dir / "blackduck_catalog.cache.yaml"),
        help="path to cached catalog yaml used as API fallback",
    )
    parser.add_argument(
        "--bd-api-url",
        help="optional Black Duck catalog API URL",
    )
    parser.add_argument(
        "--bd-api-token",
        help="optional Black Duck catalog API token (or BLACKDUCK_API_TOKEN env)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="disable interactive project selection; fail if ambiguous",
    )
    return parser.parse_args()


def read_input_text(path: str | None) -> str:
    if path:
        p = Path(path)
        return p.read_text(encoding="utf-8")

    print("Paste Jira/free-form request text. Type 'END' on a new line to finish.")
    rows: list[str] = []
    while True:
        line = input("| ")
        if line.strip() == "END":
            break
        rows.append(line)
    return "\n".join(rows).strip()


def resolve_api_key(cli_key: str | None) -> str:
    key = (cli_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    entered = getpass.getpass("OpenAI API Key: ").strip()
    if not entered:
        raise RuntimeError("OpenAI API key is required")
    return entered


def build_ssl_context(ca_bundle: str | None, insecure: bool, verify_ssl: bool) -> ssl.SSLContext:
    if ca_bundle:
        pem = Path(ca_bundle)
        if not pem.exists():
            raise RuntimeError(f"CA bundle file not found: {pem}")
        return ssl.create_default_context(cafile=str(pem))

    # Corporate environments often break public CA validation.
    # Default behavior is bypass unless explicit verification is requested.
    if verify_ssl and not insecure:
        return ssl.create_default_context()
    return ssl._create_unverified_context()


def call_openai_chat(
    model: str,
    api_key: str,
    user_text: str,
    temperature: float | None,
    ssl_context: ssl.SSLContext,
    base_url: str,
    reasoning_effort: str,
    system_prompt: str,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "Convert the following request text into the JSON schema exactly.\n\n"
                    f"[REQUEST_TEXT]\n{user_text}\n[/REQUEST_TEXT]"
                ),
            },
        ],
    }
    if temperature is not None:
        payload["temperature"] = temperature

    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl_context) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        if 400 <= e.code < 500:
            raise OpenAIRequestError(f"OpenAI API HTTP {e.code}: {detail}") from e
        raise RuntimeError(f"OpenAI API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI API connection error: {e}") from e

    data = json.loads(body)
    content = data["choices"][0]["message"]["content"]
    return parse_llm_json(content)


def parse_llm_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise RuntimeError("LLM response did not contain JSON object")

    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse JSON from LLM response: {e}") from e

    if not isinstance(parsed, dict):
        raise RuntimeError("LLM response JSON root must be an object")
    return parsed


def as_str(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value).strip() or fallback


def _path_key(value: str) -> str:
    return value.replace("\\", "/").strip().rstrip("/").lower()


def _looks_like_url(value: str) -> bool:
    text = strip_markdown_link(value).strip()
    return bool(re.match(r"^(?:https?|ftp):/*", text, flags=re.IGNORECASE) or text.startswith("//"))


def extract_excluded_scan_paths(source_text: str) -> set[str]:
    excluded: set[str] = set()
    in_ignore_section = False

    for raw_line in source_text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if not line:
            continue

        if re.search(r"ignore|exclude|제외|무시", lower):
            in_ignore_section = True
            path_match = re.search(r"(?:file\s*path|path)\s*:\s*(.+)$", line, flags=re.IGNORECASE)
            if path_match:
                candidate = path_match.group(1).strip()
                if not _looks_like_url(candidate):
                    excluded.add(_path_key(normalize_scan_path_line(candidate)))
            continue

        if in_ignore_section and re.match(r"^(?:#{1,6}|\*{2,}|={3,})", line):
            in_ignore_section = False
            continue

        if not in_ignore_section:
            continue

        path_match = re.search(r"(?:file\s*path|path)\s*:\s*(.+)$", line, flags=re.IGNORECASE)
        if not path_match:
            continue
        candidate = path_match.group(1).strip()
        if not _looks_like_url(candidate):
            excluded.add(_path_key(normalize_scan_path_line(candidate)))

    excluded.discard("")
    return excluded


def choose_project_interactively(oem: str, candidate: str, suggestions: list[str]) -> str:
    print("\n[BlackDuck Project Mapping]")
    print(f"  OEM: {oem}")
    print(f"  Input project: {candidate}")
    print("  No exact catalog match. Choose one of the suggested projects:")
    for idx, p in enumerate(suggestions, start=1):
        print(f"    {idx}. {p}")

    while True:
        raw = input("  Select number (or 'q' to abort): ").strip().lower()
        if raw == "q":
            raise RuntimeError("Project mapping aborted by user")
        if raw.isdigit():
            num = int(raw)
            if 1 <= num <= len(suggestions):
                return suggestions[num - 1]
        print("  - Invalid selection.")


def fix_version_suffix(version: str, unit_name: str, request_sw: str) -> str:
    name = unit_name.upper().strip()
    base = version.strip() if version and version.strip() else ""

    # sw_version is release metadata, not the Black Duck dashboard version key.
    # Only use it as a last fallback when no explicit blackduck_version is available.
    if not base:
        base = request_sw.strip()

    if not base:
        base = "UNKNOWN"

    if "_" in base:
        left, right = base.rsplit("_", 1)
        if right.strip().upper() in {"APPL", "FBL"}:
            return f"{left}_{name}"

    return f"{base}_{name}"


def normalize_scan_paths(raw_paths: Any, excluded_paths: set[str] | None = None) -> list[str]:
    excluded = excluded_paths or set()

    if isinstance(raw_paths, str):
        lines = [x for x in raw_paths.splitlines()]
        expanded = expand_scan_paths(lines)
        if expanded:
            raw_paths = expanded
        else:
            raw_paths = lines

    if not isinstance(raw_paths, list):
        return []

    # Preserve indentation-aware expansion when list contains raw lines.
    if any(isinstance(x, str) and (x.startswith(" ") or x.startswith("\t")) for x in raw_paths):
        expanded = expand_scan_paths([str(x) for x in raw_paths])
        if expanded:
            raw_paths = expanded

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_paths:
        if isinstance(item, dict):
            # Handle LLM outputs like {"path": "..."}
            candidate = as_str(item.get("path"))
        else:
            candidate = as_str(item)

        if _looks_like_url(candidate):
            continue

        normalized = normalize_scan_path_line(candidate)
        if not normalized:
            continue
        key = _path_key(normalized)
        if key in {"appl", "fbl"} or key in excluded:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return _repair_relative_scan_paths(out)


def _repair_relative_scan_paths(paths: list[str]) -> list[str]:
    repaired: list[str] = []
    context_parent = ""
    context_index: int | None = None

    for path in paths:
        clean_path = path.strip()
        path_without_trailing_slash = clean_path.rstrip("/")
        is_bare_child = "/" not in path_without_trailing_slash

        if context_parent and is_bare_child:
            if context_index is not None and context_index < len(repaired):
                repaired.pop(context_index)
                context_index = None
            repaired.append(f"{context_parent.rstrip('/')}/{clean_path.lstrip('/')}")
            continue

        repaired.append(clean_path)
        if clean_path.endswith("/") and path_without_trailing_slash.count("/") >= 1:
            context_parent = clean_path
            context_index = len(repaired) - 1
        else:
            context_parent = ""
            context_index = None

    return repaired


def normalize_checkout_commands(raw_commands: Any) -> list[str]:
    if isinstance(raw_commands, str):
        raw_list = [x for x in raw_commands.splitlines() if x.strip()]
    elif isinstance(raw_commands, list):
        raw_list = [as_str(x) for x in raw_commands if as_str(x)]
    else:
        raw_list = []

    normalized = [normalize_checkout_line(x) for x in raw_list]
    normalized = [x for x in normalized if x]
    normalized, _notes = auto_normalize_checkout_commands(normalized)

    # Filter out obvious non-command lines if LLM mixes headings.
    normalized = [x for x in normalized if looks_like_checkout_command(x)]
    return normalized


def normalize_manifest(data: dict[str, Any], source_text: str = "") -> dict[str, Any]:
    request = data.get("request") if isinstance(data.get("request"), dict) else {}
    source_exclusions = extract_excluded_scan_paths(source_text)
    root_exclusions = data.get("excluded_scan_paths")
    if isinstance(root_exclusions, list):
        source_exclusions.update(
            _path_key(normalize_scan_path_line(as_str(item)))
            for item in root_exclusions
            if as_str(item)
        )

    out: dict[str, Any] = {
        "request": {
            "oem": as_str(request.get("oem"), "RENAULT").upper(),
            "project_name": as_str(request.get("project_name"), "UNKNOWN_PROJECT"),
            "sw_version": as_str(request.get("sw_version"), "UNKNOWN_VERSION"),
        },
        "scan_units": [],
    }

    raw_units = data.get("scan_units")
    if not isinstance(raw_units, list):
        raw_units = []

    for unit in raw_units:
        if not isinstance(unit, dict):
            continue

        name = as_str(unit.get("name"), "APPL").upper()
        checkout_commands = normalize_checkout_commands(unit.get("checkout_commands"))
        unit_exclusions = set(source_exclusions)
        explicit_exclusions = unit.get("excluded_scan_paths")
        if isinstance(explicit_exclusions, list):
            unit_exclusions.update(
                _path_key(normalize_scan_path_line(as_str(item)))
                for item in explicit_exclusions
                if as_str(item)
            )
        scan_paths = normalize_scan_paths(unit.get("scan_paths"), unit_exclusions)

        out["scan_units"].append(
            {
                "name": name,
                "checkout_commands": checkout_commands,
                "scan_paths": scan_paths,
                "blackduck_project": as_str(
                    unit.get("blackduck_project"),
                    out["request"]["project_name"],
                ),
                "blackduck_version": as_str(unit.get("blackduck_version"), ""),
            }
        )

    return out


def apply_catalog_mapping(
    manifest: dict[str, Any],
    catalog_index: dict[str, Any],
    non_interactive: bool,
) -> dict[str, Any]:
    req = manifest.get("request", {})
    oem = as_str(req.get("oem"), "RENAULT").upper()
    req["oem"] = oem
    request_sw = as_str(req.get("sw_version"), "")

    resolved_projects: list[str] = []

    for idx, unit in enumerate(manifest.get("scan_units", [])):
        if not isinstance(unit, dict):
            continue

        unit_name = as_str(unit.get("name"), "APPL").upper()
        unit["name"] = unit_name

        candidate = as_str(unit.get("blackduck_project"), as_str(req.get("project_name"), "")).strip()
        if not candidate:
            raise CatalogMappingError(f"scan_units[{idx}] missing blackduck_project candidate")

        exact, suggestions = resolve_project_candidate(catalog_index, oem, candidate)
        if exact:
            resolved_project = exact
            print(f"[Catalog] {unit_name}: '{candidate}' -> '{resolved_project}'")
        else:
            if not suggestions:
                raise CatalogMappingError(
                    f"No Black Duck project match for OEM={oem}, candidate='{candidate}'. "
                    f"Update catalog or input a valid project."
                )

            if non_interactive:
                raise CatalogMappingError(
                    f"Ambiguous project '{candidate}' for OEM={oem}. "
                    f"Suggestions: {', '.join(suggestions)}"
                )

            resolved_project = choose_project_interactively(oem, candidate, suggestions)
            print(f"[Catalog] Selected '{resolved_project}'")

        unit["blackduck_project"] = resolved_project
        resolved_projects.append(resolved_project)

        # First priority: known project/unit dashboard version from catalog.
        catalog_ver = get_project_unit_version(catalog_index, resolved_project, unit_name)
        if catalog_ver:
            unit["blackduck_version"] = catalog_ver
        else:
            raw_ver = as_str(unit.get("blackduck_version"), "")
            unit["blackduck_version"] = fix_version_suffix(raw_ver, unit_name, request_sw)

    # If all units mapped to same project, update request.project_name to canonical catalog name.
    if resolved_projects and len(set(resolved_projects)) == 1:
        req["project_name"] = resolved_projects[0]

    resolved_oems = {
        normalize_oem(catalog_index["by_project"][normalize_key(project)]["oem"])
        for project in resolved_projects
        if normalize_key(project) in catalog_index.get("by_project", {})
    }
    if len(resolved_oems) == 1:
        req["oem"] = resolved_oems.pop()

    manifest["request"] = req
    return manifest


def run_validation(manifest_path: Path, mode: str) -> int:
    validator = ManifestValidator(manifest_path, strict_wiki_mode=mode)
    if validator.load():
        validator.validate()

    print("\n=== Validation Result ===")
    print(render_text_report(validator.issues))
    has_error = any(i.level == "ERROR" for i in validator.issues)
    return 1 if has_error else 0


def main() -> int:
    args = parse_args()

    text = read_input_text(args.input_text_file)
    if not text:
        print("ERROR: input text is empty")
        return 1

    api_key = resolve_api_key(args.api_key)
    system_prompt = load_system_prompt(Path(args.system_prompt_path))

    print("Calling OpenAI API...")
    ssl_context = build_ssl_context(args.ca_bundle, args.insecure, args.verify_ssl)

    bypass_ssl = args.insecure or (not args.verify_ssl and not args.ca_bundle)
    if bypass_ssl:
        print("WARNING: TLS certificate verification is disabled (--insecure).")
    elif args.ca_bundle:
        print(f"Using custom CA bundle: {args.ca_bundle}")
    else:
        print("Using default TLS certificate verification (--verify-ssl).")

    try:
        llm_data = call_openai_chat(
            args.model,
            api_key,
            text,
            args.temperature,
            ssl_context,
            args.base_url,
            args.reasoning_effort,
            system_prompt,
        )
    except OpenAIRequestError as exc:
        print(f"ERROR: {exc}")
        return 2

    try:
        normalized = normalize_manifest(llm_data, source_text=text)

        catalog_path = Path(args.bd_catalog)
        cache_path = Path(args.bd_catalog_cache) if args.bd_catalog_cache else None
        bd_token = (args.bd_api_token or os.environ.get("BLACKDUCK_API_TOKEN") or "").strip() or None
        entries = load_catalog(
            catalog_path=catalog_path,
            cache_path=cache_path,
            api_url=args.bd_api_url,
            api_token=bd_token,
            ssl_context=ssl_context,
        )
        if not entries:
            raise RuntimeError(
                f"Black Duck catalog is empty. Check catalog file '{catalog_path}' "
                f"or API settings."
            )

        catalog_index = build_catalog_index(entries)
        normalized = apply_catalog_mapping(normalized, catalog_index, non_interactive=args.non_interactive)
    except CatalogMappingError as exc:
        print(f"ERROR: {exc}")
        return 2

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(normalized, f, sort_keys=False, allow_unicode=True)

    print(f"Saved manifest: {out_path}")

    if args.skip_validate:
        return 0

    mode = "off"
    if args.strict_wiki:
        mode = "core"
    if args.strict_wiki_full:
        mode = "full"
    return run_validation(out_path, mode)


if __name__ == "__main__":
    sys.exit(main())
