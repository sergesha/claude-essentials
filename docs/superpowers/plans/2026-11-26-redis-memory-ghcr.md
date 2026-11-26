# Redis Memory GHCR delivery implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Ship Redis Memory 0.12.0 as prebuilt public MCP and embeddings images.
**Architecture:** Native amd64/arm64 build jobs scan exact image archives. A publisher depends on every candidate, revalidates their evidence, then publishes immutable version indexes. The launcher pulls versioned images and keeps existing backend configuration/data.
**Tech Stack:** Bash, Python stdlib, Docker Compose/buildx, GitHub Actions, existing Trivy/CISA KEV gate.
**Spec:** User-approved five-step delivery description in this conversation (2026-11-26), recorded below.

## Global constraints
- Two images: ghcr.io/sergesha/redis-memory-mcp and ghcr.io/sergesha/redis-memory-embeddings; linux/amd64 and linux/arm64.
- Block CRITICAL, HIGH with a fix, any KEV, and real mandatory-check failures; preserve existing gate and reports.
- No manual reconstruction of binary inventories as an additional release requirement.
- Preserve Redis data, credentials, namespace, embedding profile, model cache, shared mode and network/socket options.
- No automatic source-build fallback on registry failure. Developer builds remain an explicit Compose override.
- Release Please invokes image publication directly, since its token-created tags do not trigger another workflow.

## Review focus
- One failed architecture must prevent every publication (workflow dependency assertion).
- Registry errors must not be interpreted as absent versions (publisher refusal tests).
- Existing version tags must not be overwritten with different bytes (publisher identity tests).
- Shared mode must not start infrastructure; dedicated mode must retain override files and bind paths (launcher tests).
- Offline cached releases and explicit unsupported refs must behave predictably (launcher tests).

### Task 1: Registry launcher
Files: redis-memory-mcp/start.sh, docker-compose.yaml, docker-compose.build.yaml, tests/test_launcher.py, README.md.
- [x] Test mocked Docker/curl execution: shared pull/run, dedicated no-build/override, failed pulls abort, cached release fallback, invalid refs.
- [x] Replace source downloads/builds with versioned GHCR images; keep selected cache directory and local Compose override.
- [x] Provide explicit development Compose builds and update user instructions.
- [x] Run launcher tests and bash syntax checks.

### Task 2: Qualified publication
Files: .github/workflows/redis-memory.yml, redis-memory-images.yml, release-please.yml; redis-memory-mcp/scripts/publish_images.py and tests/test_image_delivery.py.
- [x] Test registry absence/error/conflict and exact index membership; assert publish depends on all candidate jobs.
- [x] Build/scan on native amd64 and arm64, transfer candidate archives and verification material, reverify before pushes.
- [x] Publish component manifests and immutable version indexes; verify anonymous pulls on both architectures.
- [x] Add Release Please outputs/call and trigger coverage; run security tests and workflow validation.

### Task 3: Release and verify
- [x] Review complete diff, push feature PR, require relevant CI and merge.
- [x] Verify generated 0.12.0 release PR/version fields, merge after CI, observe actual release and GHCR publication.
- [x] Verify public multiarchitecture manifests and local pull/runtime with existing backend data.

## Delivery evidence

- Implementation PR #158 and release PR #159 merged; tag `redis-memory-mcp-v0.12.0` points to `36ef2b66e844c9da999e0880a280448761de4034`.
- [Release workflow 45866574526](https://github.com/sergesha/claude-essentials/actions/runs/45866574526) passed: four candidate gates, exact-image publication and anonymous amd64/arm64 delivery checks. Every final scan reported zero blockers and zero errors.
- Both packages were already Public after first publication; no visibility mutation was needed.
- MCP index: `sha256:2b1e73ca04074451d0b6a90b86b026d965652ac19cb71d4ebbaa5f6243d19552`.
- Embeddings index: `sha256:e1ef7d75d02b3e978893267b112e6ba521c0771859cc2922256ddd8d71306e63`.
- Local Codex and Claude Code installations report 0.12.0. A fresh Codex session loaded the published ARM64 MCP image and passed KV and semantic reads. The pulled embeddings image passed health/profile/inference checks. Existing data mounts, namespace settings, model cache and local override were retained; Redis and Atlas containers were unchanged.
