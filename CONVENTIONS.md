# AI Agent Reference: Coding And GitHub Conventions

- Scope: `oss_requests-cict` production repository
- Purpose: rules for AI agents and contributors

## 1. Source And Path Rules

- Production code lives under `jenkins_scripts/`.
- The production Jenkinsfile is `jenkins_scripts/Jenkinsfile`.
- The production runner is `jenkins_scripts/run-bld.py`.
- The production system prompt is `jenkins_scripts/prompts/manifest_system_prompt.md`.
- Do not modify or cite `C:\OSS` legacy files as if they were production files.
- Use repository-relative paths in Jenkins commands and documentation.
- Do not add absolute local paths such as `C:\OSS` to production code.
- Keep Jenkins workspace artifacts under `jenkins_runtime/`; keep source caches under `OSS_CACHE_ROOT`.
- `jenkins_scripts/step7_mail.ps1` and `jenkins_scripts/run-bld.py` must not reference `C:\OSS` or local `.env` files. The runner uses `TEAMFORGE_USER`, `TEAMFORGE_PASSWORD`, and `BLACKDUCK_TOKEN` from Jenkins Credentials.

## 2. Python Conventions

- Use Python 3 syntax already used by the repository, including type annotations and `pathlib.Path`.
- Use small modules with clear ownership:
  - `pipeline_runner.py`: CLI routing only
  - `input_collector.py`: Jira input and contract files
  - `manifest_flow.py`: generation/validation subprocess flow
  - `manifest_builder_llm.py`: LLM payload, parsing, normalization, catalog mapping
  - `blackduck_catalog.py`: catalog data and resolution
  - `manifest_validator.py`: quality gate
  - `build_flow.py`: Jira processing guard and runner invocation
  - `run-bld.py`: checkout, staging, Black Duck Detect
  - `finalize_flow.py`: Jira finalization and notification
- Prefer existing helpers over new parallel implementations.
- Keep public CLI names and artifact filenames stable unless a migration is included.
- Keep error messages actionable and include the stage or contract field involved.
- Do not use broad exception swallowing to hide failed scans or failed validation.
- Do not add comments that merely narrate obvious assignments.
- Keep security-sensitive behavior explicit and testable.

## 3. Manifest And Prompt Conventions

- The manifest root contains `request` and `scan_units`.
- `request` contains `oem`, `project_name`, and `sw_version`.
- A scan unit contains `name`, `checkout_commands`, `scan_paths`, `blackduck_project`, and `blackduck_version`.
- Current strict validation supports `APPL` and `FBL`; extending unit types requires validator and runner work together.
- `blackduck_version` must follow the catalog/validator contract, currently `SW V<version>_APPL` or `SW V<version>_FBL`.
- `sw_version` is release metadata; it is not a substitute for `blackduck_version`.
- System prompt text belongs in the Markdown prompt file, not as a large Python string literal.
- The prompt must tell the model to distinguish filesystem paths from URLs, links, headings, branch names, explanatory text, and ignore candidates.
- Deterministic post-processing remains authoritative over model output.
- Never trust an LLM path without normalization, exclusion filtering, catalog mapping, and validation.

## 4. Jenkins And Credential Conventions

- Jenkins is configured as `Pipeline script from SCM`.
- SCM branch is `main`; script path is `jenkins_scripts/Jenkinsfile`.
- Credential values are injected with `withCredentials`; never hardcode secrets.
- Current credential contracts:
  - `JIRA_TOKEN_CRED_ID` -> `JIRA_OSS_TOKEN`
  - `OPENAI_KEY_CRED_ID` -> `OPENAI_API_KEY`
  - `TEAMFORGE_CRED_ID` -> `TEAMFORGE_USER` and `TEAMFORGE_PASSWORD`
  - `GITHUB_CRED_ID` -> `GITHUB_TOKEN` for private `github.com` checkout
  - `BLACKDUCK_CRED_ID` -> `BLACKDUCK_TOKEN`
  - `SMTP_CRED_ID` -> notification credentials
  - `GITHUB_APP_AUTH` -> SCM checkout credential in Jenkins job configuration
- Do not print tokens, passwords, API keys, or full credential-derived URLs.
- Preserve Jenkins masking and avoid putting secrets in command-line arguments when an environment binding is available.
- Keep `REQUEST_JSON` repository-relative and validate `REQUEST_REF` as a full commit SHA.
- Archive useful runtime artifacts, but exclude secrets and unnecessary source trees.
- Configure the required `OSS_MAIL_TO` comma-separated recipient list in the `test` folder's Folder Properties.
- Declarative Pipelines must use `withFolderProperties()` when they consume Folder Properties as environment variables.
- Bind `SMTP_CREDENTIALS` as `SMTP_CREDENTIALS_USR` and `SMTP_CREDENTIALS_PSW`; use the Username as the sender and do not allow anonymous SMTP fallback.
- Bind `TEAMFORGE_CRED_ID` as `TEAMFORGE_USER` and `TEAMFORGE_PASSWORD`; do not use `SERVICE_USER`, `SERVICE_PASSWORD`, or `TEAMFORGE_ID` aliases.
- Bind `GITHUB_CRED_ID` as the Secret Text variable `GITHUB_TOKEN`; do not put the PAT in checkout URLs, manifests, or logs. `run-bld.py` selects it only for `github.com` remotes and keeps TeamForge credentials for `teamforge.example.com`.
- Use the fixed notification SMTP endpoint `smtp.example.com:25` with SSL disabled.
- Failure notifications use `Pipeline-FAIL` and omit Black Duck buttons for failed units; all-failed runs have no generic Dashboard fallback.
- Failed Jira cleanup adds `oss-failed` and removes `processing` plus `oss-request`. The default Jira query excludes `oss-failed`; retry requires an explicit operator label update after the cause is corrected.

## 5. Testing Conventions

Run focused checks before broad checks:

```powershell
python -m py_compile jenkins_scripts\manifest_builder_llm.py jenkins_scripts\pipeline_runner.py
python -m unittest discover -s tests -v
```

For manifest changes also verify:

- YAML parsing
- strict wiki validation
- strict Black Duck catalog validation
- exact Jira fixture regression
- URL/Markdown link filtering
- ignore/exclude path filtering
- parent/child path reconstruction
- missing-path behavior
- catalog alias ambiguity

For checkout/build changes, use dry-run or mocked command tests before real TeamForge/Black Duck execution. Do not use production credentials in tests.

For notification changes also verify PowerShell parsing, `OSS_MAIL_TO` validation and case-insensitive deduplication, SMTP credential sender selection, missing credential failure, and absence of `C:\OSS`/`.env` references in `step7_mail.ps1`.

For Black Duck notification-link changes also verify that each parallel scan unit records its own BOM/UI URL in `last_run_summary.json`; `step7_mail.ps1` must not infer unit links from the shared `oss_build.log`.

For TeamForge checkout changes also verify askpass-file cleanup on Windows and WSL paths, including failures while making the temporary script executable.

For mail template changes also verify runtime placeholders and reject historical hardcoded ticket/project values; Black Duck links must use the browser-compatible API Components deep-link.

## 6. Git Branch And PR Policy

The repository policy is mandatory:

1. Never commit directly to `main`.
2. Create a feature or fix branch from the current `main`.
3. Stage only intended files.
4. Commit with a focused message.
5. Push the branch and set upstream.
6. Open a PR targeting `main`.
7. Let reviewers merge the PR; do not merge on behalf of the user.
8. Remote branch is deleted after merge according to repository policy.
9. Delete the obsolete local branch after merge.

Recommended branch names:

- `feature/<short-purpose>`
- `fix/<short-purpose>`
- `docs/<short-purpose>`
- `chore/<short-purpose>`

Recommended commit style:

- imperative and focused, for example `Improve AI manifest path filtering`
- one behavioral purpose per commit where practical
- do not include generated artifacts, credentials, `.env`, `__pycache__`, or unrelated formatting churn

Recommended PR body:

- summary of behavior change
- root cause or design reason
- validation commands and results
- configuration or credential changes
- remaining risks or manual test steps

## 7. Documentation Conventions

- Keep this document, the architecture document, and the development plan as agent references.
- Write explanatory prose in `README.md` and other repository documentation in Korean by default. Keep product names, Jenkins/GitHub UI labels, commands, paths, environment variables, credential IDs, API fields, and log messages in their original form when they are technical identifiers.
- When code changes invalidate a documented path, command, default, or credential contract, update the relevant reference in the same PR or explicitly record the follow-up.
- Treat the production README as operator guidance; treat code and tests as the behavioral source of truth.
- Mark historical/legacy material clearly instead of presenting it as current operation.
- Avoid duplicating full architecture or plans across multiple documents.
