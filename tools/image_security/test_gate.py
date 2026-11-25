import gzip
import hashlib
import io
import itertools
import json
import os
import tarfile
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

import advisories as adv
import gate


CVE = "CVE-2025-12345"
KEV = "CVE-2024-22222"
GHSA = "GHSA-2345-6789-cfgh"
OSV = "PYSEC-2024-123"
REV = "a" * 40
NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)


def finding(identifier=CVE, severity="LOW", fixed="", vendors=()):
    return dict(VulnerabilityID=identifier, VendorIDs=list(vendors), Severity=severity,
                InstalledVersion="1.0", PkgName="sample", FixedVersion=fixed,
                Status="fixed" if fixed else "affected", DataSource={"ID": "source"})


def db(now):
    return {"Version": 2, "UpdatedAt": (now - timedelta(hours=2)).isoformat(),
            "DownloadedAt": now.isoformat(), "NextUpdate": (now + timedelta(hours=3)).isoformat()}


def catalog():
    return {"catalogVersion": "2025.01.10", "dateReleased": "2025-01-10T00:00:00Z",
            "count": 1, "vulnerabilities": [{"cveID": KEV}]}


def lookup(records):
    def read(source, identifier):
        payload = records[(source, identifier)]
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload).encode(), adv.SOURCES[source] + identifier
    return read


class GateTest(unittest.TestCase):
    def setUp(self):
        ci = patch.dict(os.environ, {"GITHUB_ACTIONS": "false"})
        ci.start()
        self.addCleanup(ci.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_policy_matrix(self):
        for severity, fixed, exploited in itertools.product(
                ["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"], ["", "2.1"], [False, True]):
            with self.subTest(severity=severity, fixed=fixed, kev=exploited):
                result = adv.evaluate(finding(severity=severity, fixed=fixed), {CVE} if exploited else set(),
                                      adv.Aliases(lookup({})))
                block = severity == "CRITICAL" or (severity == "HIGH" and bool(fixed)) or exploited
                self.assertEqual(gate.outcome([result], []), ("blocked", 1) if block else ("pass", 0))
                self.assertEqual(result["matched_kev"], [CVE] if exploited else [])

    def test_alias_sources_and_no_alias_vs_failure(self):
        records = {("osv", OSV): {"id": OSV, "aliases": [GHSA], "related": [CVE]},
                   ("osv", GHSA): {"id": GHSA, "aliases": [OSV]},
                   ("github", GHSA): {"ghsa_id": GHSA, "cve_id": CVE, "identifiers": [
                       {"type": "CVE", "value": KEV}, {"type": "GHSA", "value": GHSA}]}}
        aliases = adv.Aliases(lookup(records))
        for item in [finding(GHSA, vendors=[CVE]), finding(OSV)]:
            result = adv.evaluate(item, {KEV}, aliases)
            self.assertEqual(gate.outcome([result], []), ("blocked", 1))
            self.assertEqual(result["matched_kev"], [KEV])
            self.assertTrue(any(e["to"] == KEV and e["source"] == "github" and len(e["sha256"]) == 64
                                for e in result["aliases"]["edges"]))
        self.assertEqual(len(aliases.records), 3)
        ignored = adv.Aliases(lookup({("osv", OSV): {"id": OSV, "related": [KEV],
                                                      "upstream": [KEV], "summary": KEV}}))
        result = adv.evaluate(finding(OSV, vendors=["RHSA-2024:1234"]), {KEV}, ignored)
        self.assertEqual(result["aliases"]["status"], "no_published_cve")
        self.assertEqual(gate.outcome([result], []), ("pass", 0))
        for resolver in (adv.Aliases(lookup({("osv", OSV): OSError("unavailable")})),
                         adv.Aliases(lookup(records), limit=1)):
            result = adv.evaluate(finding(OSV), {KEV}, resolver)
            self.assertEqual(gate.outcome([result], []), ("incomplete", 2))
            self.assertTrue(result["errors"])

    def debian_temp(self, description, notes="", *, issue_name=None, fields=None, responses=None):
        from urllib.error import HTTPError
        identifier = "TEMP-0000000-402D17"
        issue_url = "https://security-tracker.debian.org/tracker/" + identifier
        name = issue_name or identifier
        raw = (f"<html><head><title>{name}</title></head><body><header><h1>{name}</h1></header>"
               f"<table><tr><td><b>Name</b></td><td>{name}</td></tr>"
               f"<tr><td><b>Description</b></td><td>{description}</td></tr>"
               "<tr><td><b>Source</b></td><td>Automatically generated temporary name.  "
               f"Not for external reference.</td></tr></table><h2>Notes</h2><pre>{notes}</pre></body></html>").encode()
        item = finding(identifier) | {
            "Title": "[" + description + "]", "PrimaryURL": issue_url,
            "DataSource": {"ID": "debian", "Name": "Debian Security Tracker",
                           "URL": "https://salsa.debian.org/security-tracker-team/security-tracker"}}
        item.update(fields or {})
        payloads = {issue_url: raw, **(responses or {})}
        def fetch(request):
            url = request.full_url
            body = payloads.get(url, 404)
            if isinstance(body, int):
                raise HTTPError(url, body, "lookup failed", {}, None)
            if isinstance(body, Exception):
                raise body
            return (body if isinstance(body, bytes) else json.dumps(body).encode()), url
        resolver = adv.Aliases(gate.lookup_advisory)
        with patch.object(gate, "fetch_advisory", side_effect=fetch):
            result = adv.evaluate(item, {KEV}, resolver)
        return result, list(resolver.records.values()), hashlib.sha256(raw).hexdigest()

    def test_debian_temp_local_name_retains_row_and_vendor_kev(self):
        for vendors in ([], [KEV]):
            with self.subTest(vendors=vendors):
                result, receipts, digest = self.debian_temp("more related to " + KEV, fields={"VendorIDs": vendors})
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["matched_kev"], vendors)
                self.assertEqual(result["finding"]["VulnerabilityID"], "TEMP-0000000-402D17")
                self.assertEqual(result["aliases"]["status"], "resolved" if vendors else "no_published_cve")
                self.assertEqual([r["sha256"] for r in receipts], [digest])

    def test_debian_temp_repository_advisory_and_osv_absence(self):
        web = "https://github.com/seccomp/libseccomp/security/advisories/" + GHSA
        api = "https://api.github.com/repos/seccomp/libseccomp/security-advisories/" + GHSA
        osv = "https://api.osv.dev/v1/vulns/" + GHSA
        note = f'<a href="{web}">{web}</a><br/>'
        repo = {"ghsa_id": GHSA, "state": "published", "html_url": web, "url": api,
                "cve_id": None, "identifiers": [{"type": "GHSA", "value": GHSA}]}
        assigned = dict(repo, cve_id=KEV, identifiers=repo["identifiers"] + [{"type": "CVE", "value": KEV}])
        for upstream, secondary, outcome, known in [
            (repo, 404, "pass", []), (assigned, 404, "blocked", [KEV]),
            (repo, {"id": GHSA, "aliases": [KEV]}, "blocked", [KEV]),
            (404, 404, "incomplete", []), (403, 404, "incomplete", []), (429, 404, "incomplete", []),
            (dict(repo, ghsa_id="GHSA-4q85-33p6-j5g6"), 404, "incomplete", []),
            (dict(repo, state="draft"), 404, "incomplete", []),
            (dict(repo, html_url=web.replace("seccomp/", "other/", 1)), 404, "incomplete", []),
            (dict(repo, identifiers=[]), 404, "incomplete", []),
            (repo, {"id": "wrong"}, "incomplete", []), (assigned, 403, "incomplete", [KEV]),
            (assigned, 429, "incomplete", [KEV]), (assigned, OSError("offline"), "incomplete", [KEV]),
            (404, {"id": GHSA, "aliases": [KEV]}, "incomplete", [KEV]),
        ]:
            with self.subTest(upstream=upstream, secondary=secondary):
                result, receipts, digest = self.debian_temp(
                    GHSA + ": example", note, responses={api: upstream, osv: secondary})
                self.assertEqual(gate.outcome([result], [])[0], outcome)
                self.assertEqual(result["matched_kev"], known)
                self.assertIn(digest, {r.get("sha256") for r in receipts})
                if outcome != "incomplete" and secondary == 404:
                    self.assertEqual(result["aliases"]["absent_records"],
                                     [{"id": GHSA, "url": osv, "http_status": 404, "confirmed_by": api}])
                if outcome != "incomplete":
                    self.assertEqual({r["url"] for r in receipts}, {result["finding"]["PrimaryURL"], api, osv})
                    self.assertTrue(any(edge["to"] == GHSA and edge.get("sha256") == digest
                                        for edge in result["aliases"]["edges"]))

    def test_debian_temp_requires_exact_source_and_unambiguous_identity(self):
        web = "https://github.com/seccomp/libseccomp/security/advisories/" + GHSA
        link = f'<a href="{web}">{web}</a><br/>'
        for description, notes, name, fields in [
            ("local issue", "", None, {"PrimaryURL": "https://example.org/tracker/TEMP-0000000-402D17"}),
            ("local issue", "", None, {"DataSource": {"ID": "debian", "URL": "https://example.org/"}}),
            ("local issue", "", "TEMP-0000000-414660", {}), (GHSA + ": example", "", None, {}),
            (GHSA + ": example", "Related: " + link, None, {}), ("local issue", link, None, {}),
            ("local issue", "", None, {"References": ["https://example.org/advisories/issue-123"]}),
            ("PYSEC-2025-1234: example", "", None, {}),
            ("local issue", "Alias: " + KEV, None, {}),
            ('<a href="https://example.org/advisories/issue-123">local issue</a>', "", None,
             {"Title": "[local issue]"}),
            ("local issue", '<a href="https://example.org/advisories/issue-123">issue</a><br/>', None, {}),
        ]:
            with self.subTest(description=description, notes=notes, name=name, fields=fields):
                result, receipts, _ = self.debian_temp(description, notes, issue_name=name,
                                                     fields={"VendorIDs": [KEV], **fields})
                self.assertEqual(gate.outcome([result], [])[0], "incomplete")
                self.assertEqual(result["matched_kev"], [KEV])
                self.assertFalse(any(r["url"].startswith("https://example.org/") for r in receipts))

    def test_freshness_and_unavailable_evidence(self):
        gate.check_db(db(NOW), NOW)
        self.assertEqual(adv.catalog_cves(catalog(), NOW), {KEV})
        for key, value in [("UpdatedAt", NOW - timedelta(hours=25)), ("NextUpdate", NOW),
                           ("DownloadedAt", NOW + timedelta(minutes=1)), ("UpdatedAt", NOW + timedelta(hours=1))]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.check_db(dict(db(NOW), **{key: value.isoformat()}), NOW)
        for altered in [dict(catalog(), count=0), dict(catalog(), vulnerabilities=[{"cveID": "bad"}]),
                        dict(catalog(), count=2, vulnerabilities=[{"cveID": KEV}] * 2)]:
            with self.assertRaises(ValueError):
                adv.catalog_cves(altered, NOW)
        with self.assertRaises(ValueError):
            gate.check_recent((NOW - timedelta(hours=25)).isoformat(), NOW)

    def archive(self, out, corrupt_layer=False, platform="linux/arm64"):
        data = b"layer contents"
        diff = "sha256:" + hashlib.sha256(data).hexdigest()
        labels = {"org.opencontainers.image." + k: v for k, v in
                  {"source": gate.SOURCE, "version": "1", "revision": REV}.items()}
        config = json.dumps({"os": "linux", "architecture": platform.split("/")[1], "config": {"Labels": labels},
                             "rootfs": {"type": "layers", "diff_ids": [diff]}}).encode()
        ident = hashlib.sha256(config).hexdigest()
        config_name = "blobs/sha256/" + ident
        manifest = [{"Config": config_name, "Layers": ["layer"]}]
        with tarfile.open(out, "w") as tar:
            for name, content in [("manifest.json", json.dumps(manifest).encode()), (config_name, config),
                                  ("layer", gzip.compress(b"bad" if corrupt_layer else data))]:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        return {"id": "sha256:" + ident, "platform": platform, "diff_ids": [diff],
                "source": gate.SOURCE, "revision": REV, "version": "1"}

    def scan_fixture(self, out, scanner_error=False, qualified=True, extra_findings=(), encode_report=json.dumps,
                     supplemental=(), report_targets=(), producer="sample", use_real_coverage=False):
        def export(ref, platform, directory):
            return self.archive(directory / "candidate.tar", platform=platform)

        def scanner(binary, directory):
            candidate = gate.read_json(directory / "reports/candidate.json")
            report = {
                "SchemaVersion": 2, "ArtifactType": "container_image", "Trivy": {"Version": gate.VERSION},
                "Metadata": {"ImageID": candidate["id"], "DiffIDs": candidate["diff_ids"]},
                "Results": [{"Class": "os-pkgs", "Type": "debian", "Target": "sample",
                             "Packages": [{"Name": "sample", "Version": "1.0"}],
                             "Vulnerabilities": [finding(severity="CRITICAL")] if scanner_error else list(extra_findings)},
                            *report_targets]}
            (directory / "reports/trivy.json").write_text(encode_report(report))
            if scanner_error:
                raise ValueError("scanner interrupted")

        def download_db(binary, directory):
            path = directory / "cache/db"
            path.mkdir(parents=True)
            (path / "trivy.db").write_bytes(b"DB fixture")
            gate.write_json(path / "metadata.json", db(gate.now()))

        coverage = {"complete": qualified, "errors": [] if qualified else ["unqualified"],
                    "components": [{"name": "sample", "version": "1.0"}], "findings": list(supplemental)}
        coverage_patch = nullcontext() if use_real_coverage else patch.object(gate, "collect_coverage", return_value=coverage)
        platform = "linux/amd64" if producer.startswith("redis-memory-") else "linux/arm64"
        host = "Darwin-arm64"
        scanner_id = dict(gate.load_pins()["assets"][host], host=host, version=gate.VERSION)
        with patch.object(gate, "export_candidate", side_effect=export), \
             patch.object(gate, "install_scanner", return_value=(Path("trivy"), scanner_id)), \
             patch.object(gate, "download_db", side_effect=download_db), \
             patch.object(gate, "run_scanner", side_effect=scanner), \
             patch.object(gate, "fetch_advisory", return_value=(json.dumps(catalog()).encode(), gate.KEV_SOURCES[0])), \
             coverage_patch:
            return gate.main(["scan", "--image", "image", "--producer", producer,
                              "--platform", platform, "--out", str(out)])

    def test_automatic_scan_reports_onnx_without_supplier_inventory(self):
        out = self.root / "onnx"
        python_target = dict(Class="lang-pkgs", Type="python-pkg", Target="Python", Packages=[
            dict(Name="onnxruntime", Version="1.30.0", AnalyzedBy="python-pkg")])
        # Docker export belongs to the former blanket inventory; a normal automated
        # scan must work when this unrelated operation would be unavailable.
        with patch("subprocess.run", side_effect=OSError("no merged filesystem export")):
            self.assertEqual(self.scan_fixture(out, producer="redis-memory-embeddings",
                                               report_targets=[python_target], use_real_coverage=True), 0)
        reports = out / "reports"
        coverage = gate.read_json(reports / "coverage.json")
        self.assertEqual(coverage["targets"][1]["package_count"], 1)
        self.assertIn("ONNX", " ".join(coverage["limitations"]))
        self.assertEqual(gate.read_json(reports / "trivy.json")["Results"][1], python_target)
        decision = gate.read_json(reports / "decision.json")
        self.assertEqual(coverage["archive_sha256"], decision["archive_sha256"])
        self.assertEqual(coverage["image_id"], decision["candidate"]["id"])
        with patch.object(gate, "inspect_image", return_value=decision["candidate"]):
            self.assertEqual(gate.main(["verify", "--image", "dst", "--platform", "linux/amd64",
                                        "--revision", REV, "--out", str(out)]), 0)

    def test_cargo_workspace_metadata_without_linkage_proof(self):
        out = self.root / "workspace"
        cargo = dict(Class="lang-pkgs", Type="cargo", Target="workspace/Cargo.lock", Packages=[
            dict(ID="workspace-root", Relationship="root", DependsOn=["example@1.2.3"], AnalyzedBy="cargo"),
            dict(ID="example@1.2.3", Name="example", Version="1.2.3", AnalyzedBy="cargo")])
        with patch("subprocess.run", side_effect=OSError("no merged filesystem export")):
            self.assertEqual(self.scan_fixture(out, producer="redis-memory-mcp", report_targets=[cargo],
                                               use_real_coverage=True), 0)
        report = gate.read_json(out / "reports/coverage.json")
        target = report["targets"][1]
        self.assertEqual((target["package_count"], target["metadata_row_count"], target["scope"]),
                         (1, 1, "source metadata"))
        malformed = dict(cargo, Packages=[dict(cargo["Packages"][0], DependsOn=["missing"]), cargo["Packages"][1]])
        with patch("subprocess.run", side_effect=OSError("no merged filesystem export")):
            self.assertEqual(self.scan_fixture(self.root / "bad-workspace", producer="redis-memory-mcp",
                                               report_targets=[malformed], use_real_coverage=True), 2)

    def test_copy_disposition_keeps_raw_reasons_and_independent_copy_blocking(self):
        from image_inventory import pip_implementation
        source = b"pkg_resources source"
        proof = pip_implementation("setuptools", ["pkg_resources/__init__.py"], b"setuptools==70.3.0",
                                   None, lambda _: source)
        affected = dict(finding("CVE-2025-47273", "HIGH", "78.1.1"), PkgName="setuptools", InstalledVersion="70.3.0")
        vendor = dict(affected, Copy={"name":"setuptools", "version":"70.3.0", "origin":"pip/_vendor/vendor.txt",
                                      "implementation":proof})
        regular = dict(affected, Copy={"name":"setuptools", "version":"70.3.0", "origin":"another/METADATA"})
        fake = dict(affected, ImplementationDisposition={"status":"unaffected"})
        out = self.root / "per-copy"
        self.assertEqual(self.scan_fixture(out, extra_findings=[fake], supplemental=[vendor, regular]), 1)
        raw, checked_vendor, checked_regular = gate.read_json(out / "reports/decision.json")["findings"]
        self.assertEqual(raw["reasons"], ["HIGH with fix"])
        self.assertEqual(checked_regular["reasons"], ["HIGH with fix"])
        self.assertEqual(checked_vendor["reasons"], [])
        self.assertEqual(checked_vendor["raw_reasons"], ["HIGH with fix"])
        self.assertEqual(checked_vendor["finding"], vendor)
        self.assertEqual(checked_vendor["origin"]["finding_index"], 0)

    def test_copy_disposition_preserves_kev_blocker(self):
        identifier = "CVE-2025-47273"
        row = dict(finding(identifier, "HIGH", "78.1.1"), PkgName="setuptools", InstalledVersion="70.3.0",
                   Copy={"implementation": {"absent": "setuptools/package_index"}})
        out = self.root / "copy-kev"
        with patch(__name__ + ".KEV", identifier):
            self.assertEqual(self.scan_fixture(out, supplemental=[row]), 1)
        checked = gate.read_json(out / "reports/decision.json")["findings"][0]
        self.assertEqual(checked["matched_kev"], [identifier])
        self.assertEqual(checked["reasons"], ["CISA KEV"])
        self.assertCountEqual(checked["raw_reasons"], ["HIGH with fix", "CISA KEV"])

    def failed_atlas_archive(self, out, failure, **identity):
        out = out.resolve()
        candidate = {"id": "sha256:51fadf91d930e65cbd21da768700c7328c2c104a15b069a1c3fdbbf77c83c971",
                     "revision": "cbd1bc08beb2ef49cf0ba682d639bfbfe09bc0b9", "platform": "linux/arm64",
                     "diff_ids": ["sha256:" + "b" * 64], "source": gate.SOURCE, "version": "0.3.4"}
        candidate.update(identity)

        def docker_save(command, **_):
            self.assertEqual(command, ["docker", "image", "save", "--output", str(out / "candidate.tar"),
                                       "--", candidate["id"]])
            if failure == "save":
                return CompletedProcess(command, 1, b"", b"storage unavailable")
            (out / "candidate.tar").write_bytes(b"invalid Docker archive")
            return CompletedProcess(command, 0, b"", b"")

        with patch.object(gate, "inspect_image", return_value=candidate), \
             patch.object(gate.subprocess, "run", side_effect=docker_save), \
             patch.object(gate, "install_scanner", side_effect=AssertionError("unbound candidate started scanner")), \
             patch.object(gate, "fetch_kev", side_effect=AssertionError("unbound candidate fetched KEV")):
            self.assertEqual(gate.main(["scan", "--image", "atlas", "--producer", "knowledge-atlas",
                                        "--platform", "linux/arm64", "--out", str(out)]), 2)
        decision = gate.read_json(out / "reports/decision.json")
        self.assertEqual(decision["status"], "incomplete")
        self.assertEqual(decision["candidate"], candidate)
        self.assertIsNone(decision["archive_sha256"])
        self.assertTrue(any(error.startswith("candidate: docker save:") if failure == "save"
                            else error.startswith("archive:") for error in decision["errors"]))
        return decision, gate.read_json(out / "reports/coverage.json")

    def test_retained_swi_finding_survives_candidate_archive_failures(self):
        for failure in ("save", "invalid archive"):
            with self.subTest(failure=failure):
                decision, coverage = self.failed_atlas_archive(self.root / failure, failure)
                self.assertEqual(len(decision["findings"]), 1)
                finding = decision["findings"][0]
                self.assertEqual(finding["reasons"], ["HIGH with fix"])
                self.assertEqual(finding["finding"]["VulnerabilityID"], "CVE-2017-17524")
                evidence = finding["finding"]["Evidence"]
                self.assertEqual(evidence["candidate_id"], decision["candidate"]["id"])
                self.assertEqual(evidence["candidate_revision"], decision["candidate"]["revision"])
                self.assertEqual(evidence["provenance"], "retained candidate file inspection")
                self.assertEqual(coverage["image_id"], decision["candidate"]["id"])
                self.assertEqual(coverage["findings"], [finding["finding"]])
                self.assertFalse(coverage["complete"])

    def test_unmatched_atlas_identity_does_not_inherit_retained_swi_finding(self):
        for field, replacement in (("id", "sha256:" + "c" * 64), ("revision", "c" * 40)):
            with self.subTest(field=field):
                decision, coverage = self.failed_atlas_archive(self.root / field, "save", **{field: replacement})
                self.assertEqual(decision["findings"], [])
                self.assertEqual(coverage["findings"], [])

    def test_failed_scan_preserves_partial_results(self):
        out = self.root / "out"
        self.assertEqual(self.scan_fixture(out, scanner_error=True, qualified=False), 2)
        receipt = gate.read_json(out / "reports/decision.json")
        self.assertEqual(receipt["status"], "incomplete")
        self.assertIn("CRITICAL", receipt["findings"][0]["reasons"])
        self.assertTrue((out / "reports/trivy.json").is_file())
        self.assertIn("scanner interrupted", str(receipt["errors"]))
        self.assertEqual(self.scan_fixture(out), 2)
        self.assertEqual(gate.read_json(out / "reports/decision.json"), receipt)

    def test_archive_retag_platform_and_revision_mismatch(self):
        out = self.root / "out"
        self.assertEqual(self.scan_fixture(out), 0)
        candidate = gate.read_json(out / "reports/candidate.json")
        def verify(current=candidate, target="linux/arm64", revision=REV):
            with patch.object(gate, "inspect_image", return_value=current):
                return gate.main(["verify", "--image", "dst", "--platform", target,
                                  "--revision", revision, "--out", str(out)])
        self.assertEqual(verify(), 0)
        for key, value in [("id", "sha256:" + "b"*64), ("diff_ids", []), ("source", "other")]:
            self.assertEqual(verify(dict(candidate, **{key: value})), 2)
        self.assertEqual(verify(target="linux/amd64"), 2)
        self.assertEqual(verify(revision="b" * 40), 2)
        with (out / "candidate.tar").open("ab") as f:
            f.write(b"substitution")
        self.assertEqual(verify(), 2)
        other = self.root / "other.tar"
        ident = self.archive(other, corrupt_layer=True)
        with self.assertRaises(ValueError):
            gate.check_archive(other, ident)

    def test_scanner_digest_or_version_mismatch(self):
        archive = self.root / "scanner.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            content = b"#!/bin/sh\necho Version: 0.1\n"
            info = tarfile.TarInfo("trivy")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        for expected, message in [("0" * 64, "SHA-256"), (gate.sha256(archive), "version")]:
            with self.assertRaisesRegex(ValueError, message):
                gate.unpack_scanner(archive, expected, self.root)

    def test_rate_limit_preserves_known_matches_and_other_sources(self):
        from urllib.error import HTTPError
        limited = HTTPError("https://api.github.com", 403, "rate limit exceeded",
                            {"X-RateLimit-Remaining": "0"}, None)
        gh2 = "GHSA-6789-2345-cfgh"
        calls = []
        def read(source, identifier):
            calls.append((source, identifier))
            if source == "github":
                raise limited
            return json.dumps({"id": identifier, "aliases": [KEV]}).encode(), adv.SOURCES[source] + identifier
        resolver = adv.Aliases(read)
        for identifier in (GHSA, gh2):
            result = adv.evaluate(finding(identifier), {KEV}, resolver)
            self.assertEqual(gate.outcome([result], []), ("incomplete", 2))
            self.assertEqual(result["matched_kev"], [KEV])
        self.assertEqual(calls, [("osv", GHSA), ("github", GHSA), ("osv", gh2)])
        self.assertNotIn("fetched_at", resolver.records[("github", gh2)])

    def test_malformed_or_contradictory_evidence(self):
        for changes in [dict(Status="fixed"), dict(Severity="mild"), dict(FixedVersion=None),
                        dict(InstalledVersion=""), dict(DataSource={}), dict(VendorIDs=[None]),
                        dict(VendorIDs=["UNCLASSIFIED-2024-0001"])]:
            result = adv.evaluate(dict(finding(), **changes), set(), adv.Aliases(lookup({})))
            self.assertEqual(gate.outcome([result], []), ("incomplete", 2))
        unknown = finding(fixed="2", severity="HIGH")
        del unknown["Status"]
        self.assertEqual(gate.outcome([adv.evaluate(unknown, set(), adv.Aliases(lookup({})))], []),
                         ("blocked", 1))

    def test_kev_mirror_and_outage(self):
        reports = self.root
        with patch.object(gate, "fetch_advisory", side_effect=[OSError("canonical outage"),
                                                              (json.dumps(catalog()).encode(), gate.KEV_SOURCES[1])]):
            cves, meta = gate.fetch_kev(reports)
        self.assertEqual(cves, {KEV})
        self.assertEqual(meta["source"], gate.KEV_SOURCES[1])
        self.assertIn("canonical outage", gate.read_json(reports / "kev-attempts.json")[0]["error"])
        with patch.object(gate, "fetch_advisory", side_effect=[OSError("canonical outage"), ValueError("mirror invalid")]):
            with self.assertRaisesRegex(ValueError, "mirror invalid"):
                gate.fetch_kev(reports)
        self.assertTrue(all("error" in attempt for attempt in gate.read_json(reports / "kev-attempts.json")))

    def test_qualified_vs_unqualified_exit_and_verify_refusal(self):
        for name, qualified, findings, code in [("uncovered", False, [], 2),
                                                ("policy", True, [finding(severity="HIGH", fixed="2")], 1)]:
            out = self.root / name
            self.assertEqual(self.scan_fixture(out, qualified=qualified, extra_findings=findings), code)
            receipt = gate.read_json(out / "reports/decision.json")
            self.assertEqual(receipt["status"], "blocked" if code == 1 else "incomplete")
            self.assertEqual(len(receipt["findings"]), len(findings))
            self.assertEqual(gate.main(["verify", "--image", "image", "--platform", "linux/arm64",
                                        "--revision", REV, "--out", str(out)]), 2)

    def test_verify_expiry_and_evidence_tampering(self):
        out = self.root / "scan"
        self.assertEqual(self.scan_fixture(out), 0)
        candidate = gate.read_json(out / "reports/candidate.json")
        def verify():
            with patch.object(gate, "inspect_image", return_value=candidate):
                return gate.main(["verify", "--image", "dst", "--platform", "linux/arm64",
                                  "--revision", REV, "--out", str(out)])
        future = gate.now() + timedelta(hours=26)
        with patch.object(gate, "now", return_value=future):
            self.assertEqual(verify(), 2)
        (out / "reports/trivy.json").write_text("{}")
        self.assertEqual(verify(), 2)
        self.assertEqual(len(list((out / "reports").glob("verify-*.json"))), 2)

    def test_action_scan_exit_propagation(self):
        import os
        import subprocess
        import textwrap
        path = Path(__file__).resolve().parents[2] / ".github/actions/plugin-image-security"
        run = path.joinpath("action.yml").read_text().split("      run: |\n")[1].split("    - name:")[0]
        fake = self.root / "python3"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_FILE"\nexit "$RESULT"\n')
        fake.chmod(0o700)
        for code in (0, 1, 2):
            out = self.root / f"report {code}"
            outputs = self.root / f"outputs{code}"
            env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.defpath, ARGV_FILE=str(self.root / "args"),
                       GITHUB_ACTION_PATH=str(path), GITHUB_OUTPUT=str(outputs), GITHUB_RUN_ID="42",
                       GITHUB_RUN_ATTEMPT="3", PRODUCER="sample", IMAGE="sample:1", PLATFORM="linux/arm64",
                       OUT=str(out), RESULT=str(code))
            proc = subprocess.run(["bash", "-euc", textwrap.dedent(run)], env=env)
            self.assertEqual(proc.returncode, code)
            self.assertEqual((self.root / "args").read_text().splitlines()[1:], [
                "scan", "--producer", "sample", "--image", "sample:1", "--platform", "linux/arm64", "--out", str(out)])
            self.assertIn("artifact=image-security-42-3-sample-linux-arm64", outputs.read_text())
            self.assertIn(f"reports={out}/reports/", outputs.read_text())


    def test_token_confined_to_fixed_github_request(self):
        from urllib.error import HTTPError
        token = "synthetic_test_value"
        calls = []
        class FakeOpener:
            def open(self, request, timeout):
                calls.append(request)
                response = io.BytesIO(b"{}")
                response.url = request.full_url
                return response
        with patch.dict(os.environ, {"GITHUB_TOKEN": token}), \
             patch.object(gate, "build_opener", return_value=FakeOpener()) as opener:
            self.assertEqual(gate.lookup_advisory("github", GHSA), (b"{}", adv.SOURCES["github"] + GHSA))
            request = calls[0]
            self.assertEqual(request.full_url, "https://api.github.com/advisories/" + GHSA)
            self.assertEqual(request.unredirected_hdrs["Authorization"], "Bearer " + token)
            self.assertFalse(request.headers.get("Authorization"))
            handler = opener.call_args.args[0]
            for destination in ("https://example.com/leak", "https://api.github.com/another"):
                self.assertIsNone(handler.redirect_request(request, None, 302, "", {}, destination))
            self.assertEqual(gate.lookup_advisory("osv", OSV), (b"{}", adv.SOURCES["osv"] + OSV))
            self.assertFalse(calls[-1].has_header("Authorization"))
            with self.assertRaises(ValueError):
                gate.lookup_advisory("github", "../elsewhere")
            path = "seccomp/libseccomp/security-advisories/" + GHSA
            gate.lookup_advisory("github-repository", path)
            self.assertEqual(calls[-1].full_url, "https://api.github.com/repos/" + path)
            self.assertEqual(calls[-1].unredirected_hdrs["Authorization"], "Bearer " + token)
            gate.lookup_advisory("debian", "TEMP-0000000-402D17")
            self.assertFalse(calls[-1].has_header("Authorization"))
            with self.assertRaises(ValueError):
                gate.lookup_advisory("github-repository", "../org/security-advisories/" + GHSA)
            self.assertEqual(len(calls), 4)
            self.assertNotIn("GITHUB_TOKEN", gate.docker_env())
            self.assertNotIn("GITHUB_TOKEN", gate.scanner_env(self.root))
            opener.return_value.open = Mock(side_effect=HTTPError(request.full_url, 403, token, {}, None))
            record = adv.Aliases(gate.lookup_advisory).record("github", GHSA)
            self.assertIn("403", record["error"])
            self.assertNotIn(token, json.dumps(record))
        with patch.dict(os.environ, {"GITHUB_TOKEN": token}), \
             patch.object(gate, "build_opener", return_value=FakeOpener()):
            for url in gate.KEV_SOURCES:
                gate.fetch_advisory(gate.advisory_request(url))
                self.assertFalse(calls[-1].has_header("Authorization"))
        pins = gate.load_pins()
        url = pins["release"] + pins["assets"]["Darwin-arm64"]["name"]
        response = io.BytesIO(b"pinned downloadable asset")
        response.url = "https://release-assets.githubusercontent.com/example"
        with patch.dict(os.environ, {"GITHUB_TOKEN": token}), \
             patch.object(gate, "urlopen", return_value=response) as fetch:
            self.assertEqual(gate.download_asset(url), b"pinned downloadable asset")
            self.assertFalse(fetch.call_args.args[0].has_header("Authorization"))
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}), \
             patch.object(gate, "build_opener", return_value=FakeOpener()):
            gate.lookup_advisory("github", GHSA)
            self.assertFalse(calls[-1].has_header("Authorization"))


    def test_destination_retagged_during_db_verification(self):
        out = self.root / "scan"
        self.assertEqual(self.scan_fixture(out), 0)
        candidate = gate.read_json(out / "reports/candidate.json")
        current = candidate.copy()
        real_hash = gate.sha256
        def hash_and_retag(path):
            digest = real_hash(path)
            if path == out.resolve() / "cache/db/trivy.db":
                current["id"] = "sha256:" + "f" * 64
            return digest
        with patch.object(gate, "sha256", side_effect=hash_and_retag), \
             patch.object(gate, "inspect_image", side_effect=lambda ref: current.copy()):
            self.assertEqual(gate.main(["verify", "--image", "destination", "--platform", "linux/arm64",
                                        "--revision", REV, "--out", str(out)]), 2)
        refusal = gate.read_json(next((out / "reports").glob("verify-*.json")))
        self.assertEqual(refusal["error"], "destination image identity mismatch")

    def test_trusted_advisory_redirects_and_provenance(self):
        from urllib.error import HTTPError
        foreign = "https://other.example/feed"
        osv = adv.SOURCES["osv"] + OSV
        content = json.dumps(catalog()).encode()
        def response(url, body):
            stream = io.BytesIO(body)
            stream.url = url
            return stream
        def opener(handler):
            def fetch(request, timeout):
                url = request.full_url
                if url in (gate.KEV_SOURCES[0], osv):
                    for redirect in (foreign, "http://other.example/feed"):
                        self.assertIsNone(handler.redirect_request(request, None, 302, "", {}, redirect))
                    raise HTTPError(url, 302, "redirect refused", {}, None)
                return response(url, content)
            return Mock(open=fetch)
        with patch.object(gate, "build_opener", side_effect=opener), \
             patch.object(gate, "urlopen", side_effect=lambda *a, **k: response(foreign, content)):
            cves, provenance = gate.fetch_kev(self.root)
            self.assertEqual(cves, {KEV})
            self.assertEqual(provenance["source"], gate.KEV_SOURCES[1])
            self.assertEqual(provenance["effective_url"], gate.KEV_SOURCES[1])
            self.assertIn("302", gate.read_json(self.root / "kev-attempts.json")[0]["error"])
            record = adv.Aliases(gate.lookup_advisory).record("osv", OSV)
            self.assertIn("302", record["error"])
            self.assertNotIn("effective_url", record)
        with patch.object(gate, "build_opener", return_value=Mock(open=Mock(
                return_value=response(osv, json.dumps({"id": OSV}).encode())))):
            record = adv.Aliases(gate.lookup_advisory).record("osv", OSV)
            self.assertEqual(record["effective_url"], osv)
            self.assertNotIn("error", record)

    def test_pin_failure_writes_diagnostic_at_cli_entry(self):
        import shutil
        import subprocess
        import sys
        code = self.root / "tools"
        code.mkdir()
        for filename in ("advisories.py", "gate.py"):
            shutil.copyfile(gate.HERE / filename, code / filename)
        for index, content in enumerate((None, "{", "{}", '{"version": "bad"}')):
            if content is not None:
                (code / "trivy-pins.json").write_text(content)
            out = self.root / f"scan-{index}"
            proc = subprocess.run([sys.executable, str(code / "gate.py"), "scan", "--image", "sample",
                                   "--producer", "sample", "--platform", "linux/arm64", "--out", str(out)],
                                  capture_output=True, text=True, env=dict(os.environ, PATH=""))
            with self.subTest(content=content):
                self.assertEqual(proc.returncode, 2)
                receipt = gate.read_json(out / "reports/decision.json")
                self.assertEqual(receipt["status"], "incomplete")
                self.assertIn("scanner pins", " ".join(receipt["errors"]))
                self.assertNotIn("Traceback", proc.stderr)

    def test_malformed_findings_keep_independent_blockers(self):
        out = self.root / "scan"
        rows = [dict(finding(KEV, severity="CRITICAL"), Status={}),
                dict(finding(KEV), Severity=["LOW"])]
        self.assertEqual(self.scan_fixture(out, extra_findings=rows), 2)
        receipt = gate.read_json(out / "reports/decision.json")
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual([f["finding"] for f in receipt["findings"]], rows)
        for f in receipt["findings"]:
            self.assertEqual(f["matched_kev"], [KEV])
            self.assertIn("CISA KEV", f["reasons"])
            self.assertTrue(f["errors"])
        self.assertIn("CRITICAL", receipt["findings"][0]["reasons"])

    def test_db_or_kev_expires_during_destination_inspection(self):
        for source in ("db", "kev"):
            with self.subTest(source=source):
                out = self.root / source
                with patch.object(gate, "now", return_value=NOW):
                    self.assertEqual(self.scan_fixture(out), 0)
                decision = out / "reports/decision.json"
                receipt = gate.read_json(decision)
                if source == "db":
                    boundary = adv.timestamp(receipt["db"]["metadata"]["NextUpdate"])
                else:
                    boundary = NOW + timedelta(seconds=1)
                    receipt["kev"]["fetched_at"] = (boundary - timedelta(hours=24)).isoformat()
                    gate.write_json(decision, receipt)
                instant = boundary - timedelta(seconds=1)
                def inspect(ref):
                    nonlocal instant
                    instant = boundary + timedelta(seconds=1)
                    return receipt["candidate"]
                with patch.object(gate, "now", side_effect=lambda: instant), \
                     patch.object(gate, "inspect_image", side_effect=inspect) as inspected:
                    code = gate.main(["verify", "--image", "dst", "--platform", "linux/arm64",
                                      "--revision", REV, "--out", str(out)])
                inspected.assert_called_once_with("dst")
                self.assertEqual(code, 2)
                record = gate.read_json(next((out / "reports").glob("verify-*.json")))
                self.assertIn("expired DB NextUpdate" if source == "db" else "preceding 24 hours", record["error"])

    def test_overflowing_json_number_retains_raw_and_known_blockers(self):
        out = self.root / "overflow"
        rows = [finding(severity="CRITICAL"), float("inf"),
                dict(finding(KEV), Custom={"scores": [-float("inf")]})]
        def encode(report):
            return json.dumps(report).replace("Infinity", "1e400")
        self.assertEqual(self.scan_fixture(out, extra_findings=rows, encode_report=encode), 2)
        raw = (out / "reports/trivy.json").read_bytes()
        self.assertIn(b'1e400', raw)
        self.assertIn(b'-1e400', raw)
        decision = gate.read_json(out / "reports/decision.json")
        self.assertEqual(decision["status"], "incomplete")
        self.assertEqual(len(decision["findings"]), 3)
        first, malformed, nested = decision["findings"]
        self.assertIn("CRITICAL", first["reasons"])
        self.assertTrue(malformed["errors"])
        self.assertIn("1e400", json.dumps(malformed["finding"], allow_nan=False))
        self.assertIn("-1e400", json.dumps(nested["finding"]["Custom"], allow_nan=False))
        self.assertIn("CISA KEV", nested["reasons"])
        self.assertEqual(decision["evidence"]["trivy.json"], hashlib.sha256(raw).hexdigest())
        json.dumps(decision, allow_nan=False)
        only_nested = self.root / "nested"
        nested_row = dict(finding(), Custom={"scores": [float("inf")]})
        self.assertEqual(self.scan_fixture(only_nested, extra_findings=[nested_row], encode_report=encode), 2)
        incomplete = gate.read_json(only_nested / "reports/decision.json")
        self.assertTrue(any("unrepresentable JSON number 1e400" in error for error in incomplete["errors"]))

    def test_composite_action(self):
        from re import findall
        action = (Path(__file__).resolve().parents[2] / ".github/actions/plugin-image-security/action.yml").read_text()
        self.assertEqual(findall(r"^  ([a-z]+):$", action.split("runs:")[0], flags=8),
                         ["producer", "image", "platform", "out", "revision"])
        self.assertIn("if: always()", action)
        self.assertRegex(action, r"actions/upload-artifact@[a-f0-9]{40}")
        self.assertIn("if-no-files-found: error", action)
        self.assertNotIn("continue-on-error", action)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", action)
        self.assertIn("gate.py", action)
        self.assertIn(" scan ", action)


if __name__ == "__main__":
    unittest.main()
