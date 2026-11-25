# Plugin image security gate

Run this maintainer check from the repository root with Python 3.11 or later and a local Docker daemon. Give each scan a new output directory. The command inspects the local tag once, saves its immutable ID, checks the saved config and layer contents, and scans that archive. Leave enough disk for the Docker archive, scanner, vulnerability DB, cache and reports. No container needs to start.

### Darwin arm64 and Linux arm64

Build the Atlas production image from a clean checkout. Both host families use this command.

```sh
revision=$(git rev-parse HEAD)
version=$(python3 -c 'import tomllib; print(tomllib.load(open("knowledge-atlas/pyproject.toml","rb"))["project"]["version"])')
docker build --platform linux/arm64 --build-arg ATLAS_REVISION="$revision" \
  --build-arg ATLAS_VERSION="$version" -f knowledge-atlas/docker/Dockerfile \
  -t "knowledge-atlas:check-$revision" knowledge-atlas
scan_parent=$(mktemp -d)
python3 tools/image_security/gate.py scan --producer knowledge-atlas \
  --image "knowledge-atlas:check-$revision" --platform linux/arm64 --out "$scan_parent/atlas"
```

### Linux amd64

Build and scan the Redis Memory MCP production target. Run the same commands for `embeddings` with its own output directory. A local ARM64 Docker builder can build these amd64 targets and scan their saved archives.

```sh
revision=$(git rev-parse HEAD)
version=$(python3 -c 'import tomllib; print(tomllib.load(open("redis-memory-mcp/server/pyproject.toml","rb"))["project"]["version"])')
scan_parent=$(mktemp -d)
for target in mcp embeddings; do
  docker build --platform linux/amd64 --target "$target" --iidfile "$scan_parent/$target.iid" \
    --label org.opencontainers.image.source=https://github.com/sergesha/claude-essentials \
    --label org.opencontainers.image.version="$version" \
    --label org.opencontainers.image.revision="$revision" redis-memory-mcp/server || break
  python3 tools/image_security/gate.py scan --producer "redis-memory-$target" \
    --image "$(cat "$scan_parent/$target.iid")" --platform linux/amd64 --out "$scan_parent/$target"
done
```

### Decision and publication

| Exit | Decision | Meaning |
| --- | --- | --- |
| 0 | pass | All required checks completed with current evidence and no blocking finding. |
| 1 | blocked | Complete evaluation found a CRITICAL, a HIGH with an available fix, or a CISA KEV match. |
| 2 | incomplete | Required evidence is missing, inconsistent, expired or unavailable. Known blockers remain in the report. |

The collector creates a disposable, network-disabled container from the scanned config ID. It verifies the stopped container's identity, exports its merged filesystem, and removes the container. It records the base OS, installed binary and source package versions, Python distributions, native output hashes and wheel owners, interpreter version, and producer-specific build materials. Empty required inventories and unresolved native components produce an incomplete decision. The checked scope is the shipped image; model and provider downloads performed after startup have separate identities.

On Ubuntu Noble the collector fetches the current official Ubuntu CVE Tracker branch, examines the exact release/source-package status rows, and compares installed source versions using Debian epoch/revision ordering. Its receipt records the tree, commit, retrieval time, record hashes, priority, installed identities and original vendor wording. Uncertain vendor applicability remains a gap. The supplier directory retains requests, response hashes, times and response bytes for PSF, Node and OSV checks. Cargo compiler artifacts are a conservative superset and the runtime manifest identifies the shipped outputs. Source locks alone cannot establish linkage. Review `reports/decision.json` and `reports/coverage.json`. The report directory also holds the raw Trivy output when available, fetch and scan logs, the KEV catalog, structured alias receipts and evidence hashes. Stage-level errors appear in the decision, and finding-level errors appear with the finding. A failure to start or complete a scan leaves an incomplete receipt in the new directory.

The repository-local composite action takes `producer`, `image`, `platform` and `out` in the producer’s build job. It uploads the reports directory on success or failure. In GitHub Actions the candidate revision label must match `GITHUB_SHA`. Keep the candidate archive, cache and decision in the same trusted job. After both the action and evidence upload succeed, a trusted publisher tags the scanned config ID, then rechecks the destination immediately before its existing push.

```sh
candidate=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["candidate"]["id"])' "$out/reports/decision.json")
docker tag "$candidate" "$destination"
python3 tools/image_security/gate.py verify --image "$destination" --platform "$target_platform" \
  --revision "$release_sha" --out "$out"
```

Require exit 0. The publisher owns the push and the post-push association from registry manifest to checked config ID. Keep the verification record with the publication evidence. A change to the tag, archive, revision, policy files or advisory evidence requires a fresh scan.

### Scanner and evidence

The host asset pins are in `trivy-pins.json`. During pin selection, Sigstore authenticated the official v0.76.2 checksum list against issuer `https://token.actions.githubusercontent.com` and signer `https://github.com/aquasecurity/trivy/.github/workflows/reusable-release.yaml@refs/tags/v0.76.2`. SHA-256 of each of the three downloaded archives matched that signed list. Each invocation verifies its selected archive again, extracts the single CLI and checks its reported version. [Trivy release assets](https://github.com/aquasecurity/trivy/releases/expanded_assets/v0.76.2) · [Aqua signature verification](https://trivy.dev/docs/v0.76/getting-started/signature-verification/)

Trivy uses a fresh private working directory, environment and DB cache with explicit vulnerability scanners, package types and archive input. The DB must use schema 2, have a current update within 24 hours and an unexpired next update. A freshly retrieved CISA catalog can have an older release date. The pre-push check verifies the archive, evidence hashes and DB snapshot, then repeats the freshness checks.

Direct CVEs and trusted advisory equivalents enter KEV comparison. OSV aliases and GitHub global-advisory identifiers are independent sources. A GHSA may have additional equivalents in either source. Records are deduplicated within the scan. An unsuccessful necessary lookup or unresolved traversal limit makes the decision incomplete. Shared distribution bulletin numbers, related records and free text do not establish identity. The CI action uses the job’s ephemeral GitHub token for the exact public advisory GET. Locally the same GET can use an optional process-supplied `GITHUB_TOKEN`. The authenticated request refuses redirects. No token is passed to Docker or the scanner. After a source rate limit, the report identifies which remaining requests were skipped and records known matches from other evidence. [OSV aliases](https://ossf.github.io/osv-schema/#aliases-field) · [GitHub advisory identifiers](https://docs.github.com/en/rest/security-advisories/global-advisories#get-a-global-security-advisory) · [CISA KEV data](https://github.com/cisagov/kev-data)
