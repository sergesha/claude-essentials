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

Build and scan both Redis Memory production targets. The command returns a failure if either build or scan fails. A local ARM64 Docker builder can build these amd64 targets and scan their saved archives.

```sh
revision=$(git rev-parse HEAD)
version=$(python3 -c 'import tomllib; print(tomllib.load(open("redis-memory-mcp/server/pyproject.toml","rb"))["project"]["version"])')
scan_parent=$(mktemp -d)
(
status=0
for target in mcp embeddings; do
  docker build --platform linux/amd64 --target "$target" --iidfile "$scan_parent/$target.iid" \
    --label org.opencontainers.image.source=https://github.com/sergesha/claude-essentials \
    --label org.opencontainers.image.version="$version" \
    --label org.opencontainers.image.revision="$revision" redis-memory-mcp/server || { status=1; continue; }
  python3 tools/image_security/gate.py scan --producer "redis-memory-$target" \
    --image "$(cat "$scan_parent/$target.iid")" --platform linux/amd64 --out "$scan_parent/$target" || status=1
done
exit "$status"
)
```

### Decision and publication

| Exit | Decision | Meaning |
| --- | --- | --- |
| 0 | pass | All required checks completed with current evidence and no blocking finding. |
| 1 | blocked | Complete evaluation found a CRITICAL, a HIGH with an available fix, or a CISA KEV match. |
| 2 | incomplete | Required evidence is missing, inconsistent, expired or unavailable. Known blockers remain in the report. |

The required check uses Trivy's unfiltered OS and library package results, a current vulnerability DB, and CISA KEV evaluation. `reports/coverage.json` identifies the candidate and summarizes the package targets, package counts and metadata analyzers Trivy reported. Cargo lockfile entries are source metadata. Trivy's inventory does not establish the composition of bundled native code or the source tag of an ONNX Runtime wheel. The checked scope is the shipped image. Model and provider downloads after startup have separate identities.

Review `reports/decision.json`, `reports/coverage.json` and the original `reports/trivy.json`. The report directory also holds available fetch and scan logs, the KEV catalog, structured alias receipts and evidence hashes. Stage-level errors appear in the decision, and finding-level errors appear with the finding. An execution or validation failure leaves an incomplete decision and retains all available findings.

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

Direct CVEs and trusted advisory equivalents enter KEV comparison. OSV aliases and GitHub advisory identifiers are independent sources. A GHSA may have additional equivalents in either source. Records are deduplicated within the scan. An unsuccessful necessary lookup or unresolved traversal limit makes the decision incomplete. Shared distribution bulletin numbers, related records and free text do not establish identity. The CI action uses the job’s ephemeral GitHub token for the exact public advisory GET. Locally the same GET can use an optional process-supplied `GITHUB_TOKEN`. The authenticated request refuses redirects. No token is passed to Docker or the scanner. After a source rate limit, the report identifies which remaining requests were skipped and records known matches from other evidence. [OSV aliases](https://ossf.github.io/osv-schema/#aliases-field) · [GitHub advisory identifiers](https://docs.github.com/en/rest/security-advisories/global-advisories#get-a-global-security-advisory) · [CISA KEV data](https://github.com/cisagov/kev-data)

For a reported Debian temporary name, the gate checks the exact generated issue on the Debian Security Tracker. A bracketed Trivy title beginning with a GHSA must agree with the Debian description and a bare repository advisory link in its Notes. The gate checks the published GitHub repository advisory and OSV for that GHSA. A secondary OSV 404 is recorded as an absence only after the repository confirms the same typed GHSA. The original row, explicit vendor identifiers, source hashes and lookup errors remain in the reports. [Debian temporary names](https://security-tracker.debian.org/tracker/data/fake-names) · [GitHub repository advisory](https://docs.github.com/en/rest/security-advisories/repository-advisories#get-a-repository-security-advisory)


For a specific existing finding, the gate may inspect the implicated files in a disposable stopped container created from the scanned config ID. The original scanner rows remain in `trivy.json` and any supplemental findings appear in `coverage.json`. `decision.json` retains an implementation disposition, its candidate-specific evidence and the original policy reasons in `raw_reasons`. A missing or unproven disposition leaves the original reasons in place. A CISA KEV match remains blocking. Evidence that a Cargo finding occurs only in inactive source requires the compiled output hashes, the compiled dependency inventory, both active build locks and the affected source lock.
