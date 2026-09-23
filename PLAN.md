# AI Agent Reference: Development Plan

- Status: canonical working plan
- Current implementation status: unit BOM link and local `.env` removal refactors implemented locally; PR and Jenkins integration test pending
- Baseline: production Pipeline from SCM is operational for the Renault Gen3 scenario
- Production source: `oss_requests-cict` `main`

## 1. Goals

1. Keep the production Jenkins/GitHub/Jira/Black Duck flow reliable.
2. Make AI-generated manifests as trustworthy as hand-written manifests.
3. Onboard additional OEMs and projects through configuration and tested adapters, not copy/paste forks.
4. Remove legacy-workspace ambiguity and keep the production repository self-contained.
5. Keep every change reviewable, testable, and reversible.

## 2. Completed Baseline

These capabilities are already present in production code:

- GitHub Actions detects pending request JSON changes and triggers Jenkins per Jira key.
- Jenkins uses Pipeline script from SCM with `jenkins_scripts/Jenkinsfile`.
- Jira input supports YAML attachment and free-form description modes.
- AI manifest generation uses an external Markdown system prompt.
- URL, heading, malformed-link, ignore-file, and flattened parent/child path cleanup exists.
- Catalog mapping resolves aliases and canonical project/OEM values.
- Strict manifest and Black Duck catalog validation runs before the build.
- TeamForge and Black Duck credentials are injected by Jenkins.
- OSS checkout uses persistent external cache and supports Windows Git plus WSL `repo` flows.
- Black Duck Detect scans APPL/FBL staging directories.
- Jira processing labels and final status are updated by Python flow modules.
- A focused regression test suite exists under `tests/`.
- Notification success/failure flags are enabled by the GitHub Actions Jenkins trigger.
- Failure notifications derive `Pipeline-FAIL` from Jenkins failure status or `last_run_summary.json`; failed units receive no Black Duck buttons.
- Failed Jira requests receive `oss-failed` and lose `processing`/`oss-request`; the default trigger query excludes `oss-failed` to prevent automatic reruns.

The Renault Gen3 production scenario has been observed completing AI generation, TeamForge checkout, FBL/APPL Black Duck scans, and Jira finalization successfully. This is an operational baseline, not a substitute for automated coverage.

## 3. Priority Plan

### P0: Correctness And Operations

- Make the production README match the actual Jenkinsfile and code defaults.
- Decide whether a missing requested scan path is a warning or a hard failure. Prefer a required-path policy for explicit user paths.
- Normalize `request.sw_version` into a compact, deterministic string while preserving source meaning.
- Add a manifest artifact report showing requested paths, resolved paths, skipped paths, and exclusions.
- Replace TLS bypass defaults with managed CA/certificate configuration.
- Standardize failure codes for input, LLM, catalog, checkout, path resolution, scan, and Jira finalization failures.
- Add a preflight check for credential presence without printing secret values.
- Refactor notification mail to use `test` folder `OSS_MAIL_TO` and `SMTP_CREDENTIALS` only; remove mail-script `C:\OSS` and `.env` fallbacks.
- Expose `OSS_MAIL_TO` to the Pipeline with Declarative `options { withFolderProperties() }` and fail in Precheck when notifications are enabled without the setting.
- Record Black Duck BOM and UI URLs per scan unit in `last_run_summary.json`; use those unit results for mail links instead of shared-log pairing.
- Remove the `run-bld.py` local `.env` loader and use only `TEAMFORGE_USER`, `TEAMFORGE_PASSWORD`, and `BLACKDUCK_TOKEN` injected by Jenkins.

Acceptance criteria:

- A missing required path cannot silently produce a successful partial scan.
- Every generated manifest has a machine-readable path-resolution report.
- No secret appears in logs or artifacts.
- Missing or invalid `OSS_MAIL_TO` or SMTP credentials fails before an anonymous or misdirected mail can be sent.
- FBL/APPL mail links remain correct regardless of parallel Detect completion or output order.
- Retry behavior distinguishes transient errors from deterministic 4xx/configuration errors.

### P1: Multi-OEM And Multi-Project Onboarding

- Expand catalog entries with canonical project, aliases, unit versions, and known unit types.
- Decide whether the manifest model should remain APPL/FBL or support arbitrary unit names.
- Add OEM/project fixtures containing both YAML attachments and Jira descriptions.
- Add source-control profiles for TeamForge Git, SSH `repo`, GitHub/GitLab, and other approved providers as needed.
- Make repository credentials and provider selection configuration-driven.
- Define per-project rules for path roots, generated-file exclusions, and required paths.
- Add dry-run onboarding tests that never contact real Jira, TeamForge, or Black Duck.

Acceptance criteria:

- A new OEM can be added by catalog/profile/test-fixture changes without modifying generic orchestration.
- Ambiguous aliases fail clearly in non-interactive mode.
- Project/unit version names are deterministic and catalog-backed.
- Each onboarding fixture proves checkout parsing, path resolution, validation, and scan command construction.

### P2: Quality And Maintainability

- Separate generic pipeline policy from OEM/project configuration.
- Add schema validation for request JSON and manifest YAML before LLM invocation where possible.
- Add structured JSON logs for stage, Jira key, OEM, unit, and failure code.
- Add tests for ADF Jira text extraction, Markdown links, ignore sections, path hierarchy, duplicate paths, and malformed model output.
- Add CI checks for Python syntax, unit tests, prompt-file existence, and secret-pattern scanning.
- Keep `jenkins_scripts/` as the only production pipeline implementation.

### P3: Cleanup And Documentation

- Keep `C:\OSS` and local `.env` files out of production runtime code; use Jenkins Credentials and Folder Properties/parameters instead.
- Ensure temporary TeamForge askpass files are deleted on Windows/WSL checkout success, failure, and creation or permission exceptions.
- Replace historical hardcoded mail-template values with runtime placeholders and use Black Duck API Components deep-links for browser navigation.
- Remove stale documentation claims about `tools/`, Freestyle stages, old runner paths, and unsupported defaults.
- Keep one canonical architecture document, one development plan, and one coding/Git convention document for agents.
- Archive old scripts only after confirming no Jenkins job or operator depends on them.

## 4. Required Test Matrix

### Case A: YAML attachment

- Jira issue has one YAML attachment.
- AI generation is skipped.
- Attachment manifest is downloaded and strictly validated.
- Build and Jira finalization succeed.

### Case B: Free-form description

- Jira issue has no YAML attachment and has a valid description.
- AI generation runs with the external prompt.
- URL/link/ignore/path hierarchy cleanup runs.
- Generated manifest passes strict validation.
- Build and Jira finalization succeed.

### Case C: Invalid input

- Empty description, malformed request JSON, missing project, ambiguous catalog alias, or missing required path.
- Pipeline fails with a stable failure code.
- Jira `processing` label is cleaned up.
- No partial scan is reported as successful.

### Case D: Transient dependency failure

- OpenAI timeout/429, Jira transient error, or TeamForge network interruption.
- Only retryable failures retry with bounded backoff.
- Deterministic 4xx/configuration failures stop immediately.

### Case E: Additional OEM/project

- Catalog entry and fixture are added.
- The generic pipeline requires no OEM-specific code branch.
- Both dry-run and strict validation pass.

## 5. Change Workflow For Agents

Before editing:

1. Use the production repository, not `C:\OSS` legacy files.
2. Identify the owning module and one nearby test or executable check.
3. State a falsifiable hypothesis and the smallest validation.
4. Check the worktree for user changes.

After editing:

1. Run the narrowest executable test immediately.
2. Repair the same slice if it fails.
3. Run syntax/tests and inspect the diff.
4. Do not widen scope into unrelated cleanup.

## 6. Definition Of Done

A change is complete only when:

- The production path is updated, not only a local legacy copy.
- Tests or a focused executable validation pass.
- Documentation and configuration contracts agree with code.
- Secrets are not added to source or artifacts.
- A clean branch is pushed and a PR targets `main`.
- The user reviews and merges the PR; agents do not commit directly to `main`.
