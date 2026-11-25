import gzip
import hashlib
import io
import itertools
import json
import os
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
        return json.dumps(payload).encode()
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

    def archive(self, out, corrupt_layer=False):
        data = b"layer contents"
        diff = "sha256:" + hashlib.sha256(data).hexdigest()
        labels = {"org.opencontainers.image." + k: v for k, v in
                  {"source": gate.SOURCE, "version": "1", "revision": REV}.items()}
        config = json.dumps({"os": "linux", "architecture": "arm64", "config": {"Labels": labels},
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
        return {"id": "sha256:" + ident, "platform": "linux/arm64", "diff_ids": [diff],
                "source": gate.SOURCE, "revision": REV, "version": "1"}

    def scan_fixture(self, out, scanner_error=False, qualified=True, extra_findings=()):
        def export(ref, platform, directory):
            return self.archive(directory / "candidate.tar")

        def scanner(binary, directory):
            candidate = gate.read_json(directory / "reports/candidate.json")
            gate.write_json(directory / "reports/trivy.json", {
                "SchemaVersion": 2, "ArtifactType": "container_image", "Trivy": {"Version": gate.VERSION},
                "Metadata": {"ImageID": candidate["id"], "DiffIDs": candidate["diff_ids"]},
                "Results": [{"Class": "os-pkgs", "Type": "debian", "Target": "sample",
                             "Packages": [{"Name": "sample", "Version": "1.0"}],
                             "Vulnerabilities": [finding(severity="CRITICAL")] if scanner_error else list(extra_findings)}]})
            if scanner_error:
                raise ValueError("scanner interrupted")

        def download_db(binary, directory):
            path = directory / "cache/db"
            path.mkdir(parents=True)
            (path / "trivy.db").write_bytes(b"DB fixture")
            gate.write_json(path / "metadata.json", db(gate.now()))

        coverage = {"complete": qualified, "errors": [] if qualified else ["unqualified"],
                    "components": [{"name": "sample", "version": "1.0"}], "findings": []}
        host = "Darwin-arm64"
        scanner_id = dict(gate.PINS["assets"][host], host=host, version=gate.VERSION)
        with patch.object(gate, "export_candidate", side_effect=export), \
             patch.object(gate, "install_scanner", return_value=(Path("trivy"), scanner_id)), \
             patch.object(gate, "download_db", side_effect=download_db), \
             patch.object(gate, "run_scanner", side_effect=scanner), \
             patch.object(gate, "download", return_value=json.dumps(catalog()).encode()), \
             patch.object(gate, "collect_coverage", return_value=coverage):
            return gate.main(["scan", "--image", "image", "--producer", "sample",
                              "--platform", "linux/arm64", "--out", str(out)])

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
            return json.dumps({"id": identifier, "aliases": [KEV]}).encode()
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
        with patch.object(gate, "download", side_effect=[OSError("canonical outage"), json.dumps(catalog()).encode()]):
            cves, meta = gate.fetch_kev(reports)
        self.assertEqual(cves, {KEV})
        self.assertEqual(meta["source"], gate.KEV_SOURCES[1])
        self.assertIn("canonical outage", gate.read_json(reports / "kev-attempts.json")[0]["error"])
        with patch.object(gate, "download", side_effect=[OSError("canonical outage"), ValueError("mirror invalid")]):
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


    def test_composite_action(self):
        from re import findall
        action = (Path(__file__).resolve().parents[2] / ".github/actions/plugin-image-security/action.yml").read_text()
        self.assertEqual(findall(r"^  ([a-z]+):$", action.split("runs:")[0], flags=8),
                         ["producer", "image", "platform", "out"])
        self.assertIn("if: always()", action)
        self.assertRegex(action, r"actions/upload-artifact@[a-f0-9]{40}")
        self.assertIn("if-no-files-found: error", action)
        self.assertNotIn("continue-on-error", action)
        self.assertIn("gate.py", action)
        self.assertIn(" scan ", action)


if __name__ == "__main__":
    unittest.main()
