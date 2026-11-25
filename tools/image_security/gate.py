"""Evaluate and recheck one local Docker image. Python 3.11+ standard library."""

import argparse
import gzip
import hashlib
from http.client import HTTPException
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import advisories as adv
from advisories import demand, string

HERE = Path(__file__).resolve().parent
VERSION = "0.76.2"
SOURCE = "https://github.com/sergesha/claude-essentials"
POLICY = "plugin-image-security/1"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
KEV_SOURCES = (
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
    "https://raw.githubusercontent.com/cisagov/kev-data/develop/known_exploited_vulnerabilities.json",
)
SCAN_FLAGS = ["--scanners", "vuln", "--pkg-types", "os,library", "--format", "json", "--list-all-pkgs",
              "--skip-db-update", "--offline-scan", "--ignorefile", "", "--disable-telemetry"]
ERRORS = (AttributeError, HTTPException, ValueError, OSError, TypeError, KeyError, IndexError, EOFError, tarfile.TarError,
          subprocess.SubprocessError)


def now():
    return datetime.now(timezone.utc)


def stream_hash(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def sha256(path):
    with path.open("rb") as stream:
        return stream_hash(stream)


def load_pins():
    pins = read_json(HERE / "trivy-pins.json")
    demand(pins.get("version") == VERSION, "missing or unexpected Trivy version")
    demand(pins.get("release") == f"https://github.com/aquasecurity/trivy/releases/download/v{VERSION}/",
           "unexpected Trivy release URL")
    for host, suffix in (("Darwin-arm64", "macOS-ARM64"), ("Linux-arm64", "Linux-ARM64"),
                         ("Linux-amd64", "Linux-64bit")):
        asset = pins["assets"][host]
        demand(asset["name"] == f"trivy_{VERSION}_{suffix}.tar.gz" and
               re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]), f"invalid Trivy pin for {host}")
    demand(set(pins["assets"]) == {"Darwin-arm64", "Linux-arm64", "Linux-amd64"}, "unexpected Trivy host pin")
    return pins


def policy_files():
    return {name: sha256(HERE / name) for name in ("gate.py", "advisories.py", "coverage.py", "image_inventory.py",
                                                   "ubuntu_coverage.py", "supplier_coverage.py", "trivy-pins.json")}


def read_json(path):
    return json.loads(path.read_bytes())


def read_trivy_report(path, errors):
    # Preserve unrepresentable numeric tokens for diagnostics without changing the raw report.
    def unrepresentable(token):
        errors.append(f"{path.name}: unrepresentable JSON number {token}")
        return {"unrepresentable_number": token}

    def parse_float(token):
        value = float(token)
        return value if math.isfinite(value) else unrepresentable(token)

    return json.loads(path.read_bytes(), parse_float=parse_float, parse_constant=unrepresentable)


def write_json(path, value):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=True, allow_nan=False)
        handle.write("\n")
    tmp.replace(path)


def download_asset(url):
    request = Request(url, headers={"User-Agent": "claude-essentials-image-security/1"})
    with urlopen(request, timeout=60) as response:
        demand(response.url.startswith("https://"), "insecure redirect")
        return response.read()


class RejectRedirect(HTTPRedirectHandler):
    """Require the exact advisory endpoint to serve its own bytes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def advisory_request(url):
    return Request(url, headers={"User-Agent": "claude-essentials-image-security/1"})


def fetch_advisory(request):
    demand(request.full_url.startswith("https://"), "advisory endpoint requires HTTPS")
    with build_opener(RejectRedirect()).open(request, timeout=60) as response:
        demand(response.url == request.full_url and response.url.startswith("https://"),
               "unexpected effective advisory URL")
        return response.read(), response.url


def lookup_advisory(source, identifier):
    if source == "osv":
        return fetch_advisory(advisory_request(adv.SOURCES["osv"] + adv.advisory_id(identifier)))
    demand(source == "github" and adv.GHSA.fullmatch(identifier), "invalid GitHub advisory lookup")
    url = adv.SOURCES["github"] + identifier
    request = advisory_request(url)
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        demand(token.isascii() and token.isprintable() and not any(c.isspace() for c in token),
               "invalid GitHub token encoding")
        request.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        return fetch_advisory(request)
    except HTTPError as exc:
        # Persist only the status and rate-limit headers. A peer controls the reason text.
        headers = {name: exc.headers.get(name) for name in
                   ("X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After") if exc.headers
                   and str(exc.headers.get(name, "")).isdigit()}
        limited = exc.code == 429 or (exc.code == 403 and
                                      (headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in str(exc).lower()))
        exc.close()
        raise HTTPError(url, exc.code, "rate limit exceeded" if limited else "GitHub advisory request failed",
                        headers, None) from None
    except (OSError, HTTPException, ValueError) as exc:
        raise OSError(f"GitHub advisory transport failed ({type(exc).__name__})") from None


def docker_env():
    return {key: value for key, value in os.environ.items() if key != "GITHUB_TOKEN"}


def inspect_image(ref):
    cmd = ["docker", "image", "inspect", "--", ref]
    result = subprocess.run(cmd, env=docker_env(), capture_output=True, check=False, timeout=90)
    demand(result.returncode == 0, result.stderr.decode(errors="replace").strip() or "docker inspect failed")
    images = json.loads(result.stdout)
    demand(isinstance(images, list) and len(images) == 1, "expected one local image")
    image = images[0]
    labels = image["Config"].get("Labels") or {}
    return {"id": image["Id"], "platform": image["Os"] + "/" + image["Architecture"],
            "diff_ids": image["RootFS"]["Layers"],
            **{key: labels.get("org.opencontainers.image." + key) for key in ("source", "version", "revision")}}


def check_candidate(candidate, target):
    demand(DIGEST.fullmatch(candidate["id"]), "invalid image config ID")
    demand(candidate["platform"] == target, "candidate platform mismatch")
    layers = candidate["diff_ids"]
    demand(isinstance(layers, list) and layers and all(DIGEST.fullmatch(x) for x in layers),
           "missing or invalid ordered DiffIDs")
    demand(candidate["source"] == SOURCE, "candidate OCI source label mismatch")
    demand(string(candidate["version"]) and string(candidate["revision"]) and REVISION.fullmatch(candidate["revision"]),
           "missing OCI version or invalid revision")


def export_candidate(ref, target, directory):
    candidate = inspect_image(ref)
    write_json(directory / "reports/candidate.json", candidate)
    check_candidate(candidate, target)
    result = subprocess.run(["docker", "image", "save", "--output", str(directory / "candidate.tar"),
                             "--", candidate["id"]], env=docker_env(), capture_output=True, timeout=1800)
    demand(result.returncode == 0, "docker save: " + result.stderr.decode(errors="replace"))
    return candidate


def check_archive(path, candidate):
    with tarfile.open(path, "r:") as archive:
        members = {}
        for member in archive:
            name = member.name.removeprefix("./")
            normalized = PurePosixPath(name)
            demand(str(normalized) == name and not normalized.is_absolute() and ".." not in normalized.parts,
                   f"noncanonical archive member {name}")
            demand(name not in members, f"duplicate archive member {name}")
            members[name] = member

        def content(name):
            demand(name in members and members[name].isfile(), f"missing or ambiguous archive member {name}")
            return archive.extractfile(members[name])

        with content("manifest.json") as stream:
            manifests = json.load(stream)
        demand(isinstance(manifests, list) and len(manifests) == 1, "expected one archive manifest")
        manifest = manifests[0]
        with content(manifest["Config"]) as stream:
            raw = stream.read()
        demand("sha256:" + hashlib.sha256(raw).hexdigest() == candidate["id"], "archive config ID mismatch")
        config = json.loads(raw)
        labels = config["config"]["Labels"]
        demand(config["os"] + "/" + config["architecture"] == candidate["platform"] and
               all(labels["org.opencontainers.image." + key] == candidate[key]
                   for key in ("source", "version", "revision")), "archive platform or OCI labels mismatch")
        demand(config["rootfs"]["diff_ids"] == candidate["diff_ids"], "archive config DiffIDs mismatch")
        layers = manifest["Layers"]
        demand(isinstance(layers, list) and len(layers) == len(candidate["diff_ids"]), "archive layer count mismatch")
        for name, expected in zip(layers, candidate["diff_ids"]):
            with content(name) as stream:
                if stream.peek(2)[:2] == b"\x1f\x8b":
                    with gzip.GzipFile(fileobj=stream) as uncompressed:
                        actual = stream_hash(uncompressed)
                else:
                    actual = stream_hash(stream)
            demand("sha256:" + actual == expected, f"archive layer DiffID mismatch: {name}")


def scanner_env(directory):
    return {"HOME": str(directory), "PATH": os.defpath,
            "TMPDIR": str(directory / "tmp"), "XDG_CACHE_HOME": str(directory / "cache")}


def unpack_scanner(archive, expected, directory):
    demand(sha256(archive) == expected, "scanner archive SHA-256 mismatch")
    binary = directory / "trivy"
    with tarfile.open(archive, "r:gz") as package:
        members = [m for m in package if m.name == "trivy"]
        demand(len(members) == 1 and members[0].isfile(), "invalid scanner archive")
        with package.extractfile(members[0]) as src, binary.open("xb") as dest:
            shutil.copyfileobj(src, dest)
    binary.chmod(0o700)
    version = subprocess.run([str(binary), "--version"], cwd=directory, env=scanner_env(directory),
                             text=True, capture_output=True, check=True, timeout=30)
    demand(version.stdout.splitlines()[:1] == ["Version: " + VERSION], "scanner version mismatch")
    return binary


def install_scanner(directory, pins):
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine(), platform.machine())
    host = platform.system() + "-" + arch
    demand(host in pins["assets"], f"unsupported scanner host {host}")
    asset = pins["assets"][host]
    url = pins["release"] + asset["name"]
    archive = directory / asset["name"]
    archive.write_bytes(download_asset(url))
    binary = unpack_scanner(archive, asset["sha256"], directory)
    return binary, dict(asset, host=host, version=VERSION, url=url, binary_sha256=sha256(binary))


def invoke(binary, directory, flags, name):
    args = [str(binary), "image", "--config", "", "--cache-dir", str(directory / "cache"),
            "--timeout", "30m", *flags]
    with (directory / "reports" / (name + ".log")).open("wb") as log:
        result = subprocess.run(args, cwd=directory, env=scanner_env(directory), stdout=log,
                                stderr=subprocess.STDOUT, timeout=1900)
    demand(result.returncode == 0, f"{name} exited {result.returncode}; see {name}.log")


def download_db(binary, directory):
    invoke(binary, directory, ["--download-db-only", "--disable-telemetry", "--no-progress"], "fetch-db")


def run_scanner(binary, directory):
    invoke(binary, directory, [*SCAN_FLAGS, "--input", str(directory / "candidate.tar"),
                               "--output", str(directory / "reports/trivy.json")], "scan")


def check_recent(value, at):
    age = at - adv.timestamp(value)
    demand(timedelta(0) <= age <= timedelta(hours=24), "evidence outside preceding 24 hours")


def check_db(metadata, at):
    demand(type(metadata["Version"]) is int and metadata["Version"] == 2, "incompatible Trivy DB schema")
    check_recent(metadata["UpdatedAt"], at)
    check_recent(metadata["DownloadedAt"], at)
    demand(adv.timestamp(metadata["UpdatedAt"]) <= adv.timestamp(metadata["DownloadedAt"]),
           "DB was downloaded before its update")
    demand(adv.timestamp(metadata["NextUpdate"]) > at, "expired DB NextUpdate")


def load_db(directory):
    base = directory / "cache/db"
    metadata = read_json(base / "metadata.json")
    demand((base / "trivy.db").stat().st_size > 0, "empty Trivy DB")
    return {"metadata": metadata, "sha256": sha256(base / "trivy.db"), "fetched_at": now().isoformat()}


def fetch_kev(reports):
    attempts = []
    for source in KEV_SOURCES:
        entry = {"source": source}
        attempts.append(entry)
        try:
            raw, entry["effective_url"] = fetch_advisory(advisory_request(source))
            entry.update(sha256=hashlib.sha256(raw).hexdigest(), fetched_at=now().isoformat())
            (reports / f"kev-response-{len(attempts)}.json").write_bytes(raw)
            catalog = json.loads(raw)
            cves = adv.catalog_cves(catalog, now())
            (reports / "kev.json").write_bytes(raw)
            metadata = {key: catalog[key] for key in ("catalogVersion", "dateReleased", "count")}
            return cves, dict(entry, **metadata)
        except ERRORS as exc:
            entry["error"] = str(exc)
            if isinstance(exc, HTTPError):
                exc.close()
        finally:
            write_json(reports / "kev-attempts.json", attempts)
    raise ValueError("KEV unavailable: " + "; ".join(a["source"] + ": " + a["error"] for a in attempts))


def collect_coverage(producer, candidate, archive, report, reports):
    from coverage import collect_coverage as collect
    return collect(producer, candidate, archive, report, reports)


def outcome(findings, errors):
    if errors or any(f["errors"] for f in findings):
        return "incomplete", 2
    if any(f["reasons"] for f in findings):
        return "blocked", 1
    return "pass", 0


def evaluate_report(report, candidate, evaluate, errors, virtual_workspaces=()):
    if not (report.get("SchemaVersion") == 2 and report.get("ArtifactType") == "container_image" and
            report.get("Trivy", {}).get("Version") == VERSION):
        errors.append("unexpected Trivy report schema, artifact type or version")
    metadata = report.get("Metadata", {})
    if (metadata.get("ImageID"), metadata.get("DiffIDs")) != (candidate.get("id"), candidate.get("diff_ids")):
        errors.append("Trivy report config ID or ordered DiffIDs mismatch")
    results = report.get("Results")
    demand(isinstance(results, list) and results, "missing Trivy package targets")
    os_inventory = False
    for index, result in enumerate(results):
        try:
            cls, target, typ = result.get("Class"), result.get("Target"), result.get("Type")
            packages = result.get("Packages")
            if not (cls in ("os-pkgs", "lang-pkgs") and string(target) and string(typ)):
                errors.append(f"invalid Trivy result target at {index}")
            if not (isinstance(packages, list) and packages and all(isinstance(p, dict) and
                    ((string(p.get("Name")) and string(p.get("Version"))) or
                    (typ == "cargo" and any(proof["target"] == target and proof["package_index"] == i and
                                           proof["row"] == p for proof in virtual_workspaces)))
                    for i, p in enumerate(packages))):
                errors.append(f"incomplete package inventory at target {index}")
            elif cls == "os-pkgs":
                os_inventory = True
            vulns = result.get("Vulnerabilities", [])
            demand(isinstance(vulns, list), "invalid vulnerabilities array")
            for item in vulns:
                evaluate(item, {"report": "trivy.json", "target": target, "type": typ, "result_index": index})
            if result.get("ModifiedFindings"):
                errors.append(f"unexpected modified or suppressed findings in target {index}")
        except ERRORS as exc:
            errors.append(f"Trivy target {index}: {exc}")
    if not os_inventory:
        errors.append("no OS package inventory")


def scan(args, directory):
    directory.mkdir(mode=0o700)
    reports = directory / "reports"
    reports.mkdir()
    decision = {"status": "incomplete", "policy": POLICY, "policy_files": None, "started_at": now().isoformat(),
                "producer": args.producer, "image": args.image, "platform": args.platform,
                "candidate": None, "archive_sha256": None, "scanner": None, "db": None, "kev": None,
                "config": {"flags": SCAN_FLAGS, "isolated": True}, "findings": [],
                "errors": ["evaluation did not finish"], "evidence": {}}
    write_json(reports / "decision.json", decision)
    errors, findings = [], decision["findings"]
    (directory / "tmp").mkdir()
    aliases = adv.Aliases(lookup_advisory)

    def step(name, work):
        try:
            return work()
        except ERRORS as exc:
            errors.append(f"{name}: {exc}")
            return None

    pins = step("scanner pins", load_pins)
    decision["policy_files"] = step("policy file identities", policy_files)
    archive = directory / "candidate.tar"
    candidate = step("candidate", lambda: export_candidate(args.image, args.platform, directory))
    if candidate is None:
        if (reports / "candidate.json").exists():
            decision["candidate"] = read_json(reports / "candidate.json")
    else:
        decision["candidate"] = candidate
        write_json(reports / "candidate.json", candidate)

        def bind():
            check_candidate(candidate, args.platform)
            if os.environ.get("GITHUB_ACTIONS") == "true":
                demand(candidate["revision"] == os.environ.get("GITHUB_SHA"), "candidate revision differs from CI checkout")
            check_archive(archive, candidate)
            return sha256(archive)
        decision["archive_sha256"] = step("archive", bind)

    kev = set()
    binary = None
    if decision["archive_sha256"]:
        scanner = step("scanner integrity", lambda: install_scanner(directory, pins)) if pins else None
        if scanner:
            binary, decision["scanner"] = scanner
            def fetch_db():
                download_db(binary, directory)
                decision["db"] = load_db(directory)
                check_db(decision["db"]["metadata"], now())
                return True
            if step("vulnerability DB", fetch_db):
                step("Trivy scan", lambda: run_scanner(binary, directory))
        collected = step("KEV", lambda: fetch_kev(reports))
        if collected:
            kev, decision["kev"] = collected

    def evaluate(item, origin):
        try:
            assessed = adv.evaluate(item, kev, aliases)
            assessed["origin"] = origin
            findings.append(assessed)
        except ERRORS as exc:
            errors.append(f"invalid finding in {origin}: {exc}")

    raw = reports / "trivy.json"
    report = None
    if raw.exists():
        report = step("Trivy JSON", lambda: read_trivy_report(raw, errors))
    else:
        errors.append("Trivy report unavailable")
    coverage = None
    if decision["archive_sha256"]:
        coverage = step("coverage", lambda: collect_coverage(args.producer, candidate, archive, report, reports))
    if coverage is None:
        coverage = {"complete": False, "components": [], "findings": [], "errors": ["coverage unavailable"]}
    coverage.update(producer=args.producer, image_id=(candidate or {}).get("id"),
                    archive_sha256=decision["archive_sha256"])
    write_json(reports / "coverage.json", coverage)

    if report is not None:
        step("Trivy evidence", lambda: evaluate_report(report, candidate or {}, evaluate, errors,
                                                       coverage.get("virtual_workspaces", [])))

    def assess_coverage():
        rows = coverage["findings"]
        demand(isinstance(rows, list), "invalid supplemental findings")
        for item in rows:
            evaluate(item, {"report": "coverage.json", "producer": args.producer})
        demand(isinstance(coverage["errors"], list), "invalid coverage errors")
        errors.extend(coverage["errors"])
        demand(coverage["complete"] is True and isinstance(coverage["components"], list) and
               coverage["components"], "incomplete producer coverage")
    step("coverage qualification", assess_coverage)
    write_json(reports / "aliases.json", list(aliases.records.values()))
    if decision["db"]:
        step("final DB freshness", lambda: check_db(decision["db"]["metadata"], now()))
    if decision["kev"]:
        step("final KEV freshness", lambda: check_recent(decision["kev"]["fetched_at"], now()))
    decision["errors"] = errors
    decision["finished_at"] = now().isoformat()
    decision["status"], code = outcome(findings, errors)
    decision["evidence"] = {p.relative_to(reports).as_posix(): sha256(p)
                            for p in sorted(reports.rglob("*")) if p.is_file() and p.name != "decision.json"}
    write_json(reports / "decision.json", decision)
    print(f"{decision['status']} ({len(findings)} findings): {reports / 'decision.json'}")
    return code


def verify(args, directory):
    reports = directory / "reports"
    record = {"status": "incomplete", "at": now().isoformat(), "image": args.image,
              "platform": args.platform, "revision": args.revision}
    try:
        decision = read_json(reports / "decision.json")
        demand(decision["policy"] == POLICY and decision["policy_files"] == policy_files() and decision["status"] == "pass" and
               outcome(decision["findings"], decision["errors"]) == ("pass", 0), "requires passing decision")
        demand(decision["platform"] == args.platform and REVISION.fullmatch(args.revision),
               "invalid revision or mismatched decision platform")
        evidence = decision["evidence"]
        required = {"candidate.json", "trivy.json", "kev.json", "aliases.json", "coverage.json", "kev-attempts.json"}
        demand(required <= evidence.keys(), "missing evidence in decision")
        for name, digest in evidence.items():
            path = (reports / name).resolve()
            demand(path.is_relative_to(reports) and sha256(path) == digest, f"evidence hash mismatch: {name}")
        candidate = read_json(reports / "candidate.json")
        check_candidate(candidate, args.platform)
        demand(candidate == decision["candidate"] and candidate["revision"] == args.revision, "candidate revision mismatch")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            demand(os.environ.get("GITHUB_SHA") == args.revision, "revision differs from CI checkout")
        archive = directory / "candidate.tar"
        demand(sha256(archive) == decision["archive_sha256"], "candidate archive hash mismatch")
        check_archive(archive, candidate)
        scanner = decision["scanner"]
        pin = load_pins()["assets"][scanner["host"]]
        demand(scanner["version"] == VERSION and scanner["sha256"] == pin["sha256"] and
               scanner["name"] == pin["name"], "unexpected scanner identity")
        coverage = read_json(reports / "coverage.json")
        demand(coverage["complete"] is True and coverage["components"] and not coverage["errors"] and
               coverage["producer"] == decision["producer"] and coverage["image_id"] == candidate["id"] and
               coverage["archive_sha256"] == decision["archive_sha256"], "coverage incomplete or mismatched")
        catalog = read_json(reports / "kev.json")
        adv.catalog_cves(catalog, now())
        demand(decision["kev"]["source"] in KEV_SOURCES and
               decision["kev"]["effective_url"] == decision["kev"]["source"] and decision["kev"]["sha256"] == evidence["kev.json"],
               "KEV evidence mismatch")
        db = decision["db"]
        demand(read_json(directory / "cache/db/metadata.json") == db["metadata"] and
               sha256(directory / "cache/db/trivy.db") == db["sha256"], "DB snapshot mismatch")
        demand(inspect_image(args.image) == candidate, "destination image identity mismatch")
        checked_at = now()
        record["at"] = checked_at.isoformat()
        check_db(db["metadata"], checked_at)
        check_recent(db["fetched_at"], checked_at)
        check_recent(decision["kev"]["fetched_at"], checked_at)
        record.update(status="pass", image_id=candidate["id"], archive_sha256=decision["archive_sha256"])
    except ERRORS as exc:
        record["error"] = str(exc)
    # Keep previous checks, including earlier refusals, outside the scan evidence manifest.
    if reports.is_dir():
        write_json(reports / ("verify-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"), record)
    print(json.dumps(record))
    return 0 if record["status"] == "pass" else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--producer" if name == "scan" else "--revision", required=True)
        for arg in ("image", "platform", "out"):
            command.add_argument("--" + arg, required=True)
    args = parser.parse_args(argv)
    try:
        return (scan if args.command == "scan" else verify)(args, Path(args.out).resolve())
    except ERRORS as exc:
        print(f"incomplete: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
