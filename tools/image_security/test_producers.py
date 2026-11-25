import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import advisories
import gate
import coverage as producer


class TestProducerCoverage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = dict(id="sha256:" + "a" * 64, platform="linux/arm64",
                              diff_ids=["sha256:" + "b" * 64])
        self.trivy = {"Results": [{"Class": "os-pkgs", "Type": "ubuntu", "Packages": [
            {"Name": "libexample", "Version": "1:2.0~rc1-1ubuntu1", "Arch": "arm64"}]}]}
        self.source = {"example": [{"name": "libexample", "architecture": "arm64",
                                    "version": "1:2.0~rc1-1ubuntu1", "source_version": "1:2.0~rc1-1ubuntu1"}]}

    def tar(self, entries):
        path = self.root / "rootfs.tar"
        with tarfile.open(path, "w") as archive:
            for name, data in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return path

    def inspect(self, entries):
        image = self.tar(entries)
        return producer.inventory("knowledge-atlas", image, self.trivy)

    def test_missing_copied_interpreter_or_bundled_library_is_incomplete(self):
        result = self.inspect({})
        for expected in ("CPython", "OpenBLAS", "GMP"):
            self.assertIn(expected, " ".join(result["errors"]))
        self.assertFalse(result["complete"])

    def test_lockfile_is_not_linkage_evidence(self):
        result = self.inspect({"opt/atlas-seed/extract/Cargo.lock": b'[[package]]\nname = "quinn-proto"\nversion = "0.11.14"\n'})
        self.assertIn("output", " ".join(result["errors"]))
        self.assertFalse(any(c.get("name") == "quinn-proto" and c.get("applicability") == "shipped"
                             for c in result["components"]))

    def test_supplemental_finding_enters_shared_policy(self):
        rows = []
        proofs = []
        errors = []
        for i, (severity, vendor_status) in enumerate((("critical", "ignored (will not fix)"),
                                                      ("low", "in-progress"), ("low", "needed"),
                                                      ("high", "released (1:2.0~rc1-1ubuntu1)"),
                                                      ("high", "released (1:2.0-1ubuntu1)"))):
            cve = f"CVE-2026-{1000+i}"
            raw = (f"Candidate: {cve}\nPriority: {severity}\nPatches_example:\n"
                   f"jammy_example: needed\nnoble_example: {vendor_status}\n").encode()
            producer.ubuntu_record(raw, cve, "noble", self.source, rows, proofs, errors)
        self.assertFalse(errors)
        self.assertEqual([r["VulnerabilityID"] for r in rows],
                         ["CVE-2026-1000", "CVE-2026-1001", "CVE-2026-1002", "CVE-2026-1004"])
        decisions = [advisories.evaluate(r, {"CVE-2026-1001"}, advisories.Aliases(None)) for r in rows]
        self.assertEqual([bool(d["reasons"]) for d in decisions], [True, True, False, True])
        self.assertFalse(any(d["errors"] for d in decisions))
        self.assertTrue(all(r["DataSource"]["ID"] == "ubuntu-cve-tracker" for r in rows))
        self.assertEqual(proofs[3]["applicability"], "fixed in installed version")

    def test_other_release_and_uncertain_vendor_status(self):
        rows, proofs, errors = [], [], []
        raw = b"Candidate: CVE-2026-1234\nPriority: critical\njammy_example: needed\n"
        producer.ubuntu_record(raw, "CVE-2026-1234", "noble", self.source, rows, proofs, errors)
        self.assertEqual(rows, [])
        producer.ubuntu_record(raw + b"noble_example: ignored (end of life, was needs-triage)\n",
                               "CVE-2026-1234", "noble", self.source, rows, proofs, errors)
        self.assertTrue(errors)
        self.assertFalse(rows)

    def test_missing_ubuntu_vendor_source_blocks_even_for_trivy_listed_package(self):
        dpkg = (b"Package: libexample\nStatus: install ok installed\nArchitecture: arm64\n"
                b"Source: example\nVersion: 1:2.0~rc1-1ubuntu1\n")
        archive = self.tar({"var/lib/dpkg/status": dpkg, "usr/lib/os-release":
                            b"ID=ubuntu\nVERSION_ID=\"24.04\"\nVERSION_CODENAME=noble\n"})
        with patch.object(producer, "export_rootfs", return_value=archive), \
             patch.object(producer, "ubuntu_snapshot", side_effect=OSError("vendor unavailable")) as fetch:
            result = producer.collect_coverage("knowledge-atlas", self.candidate, archive, self.trivy, self.root)
        fetch.assert_called_once()
        self.assertFalse(result["complete"])
        self.assertIn("example", " ".join(result["errors"]))
        self.assertIn("vendor unavailable", " ".join(result["errors"]))
        self.assertEqual(result["diff_ids"], self.candidate["diff_ids"])

    def test_redis_targets_scan_the_built_images_and_keep_both_reports(self):
        workflow = (gate.HERE.parents[1] / ".github/workflows/redis-memory.yml").read_text()
        for value in ("fail-fast: false", "target: [mcp, embeddings]", '--target "$TARGET"',
                      "--iidfile", "--platform linux/amd64", "uses: ./.github/actions/plugin-image-security",
                      "image: ${{ steps.build.outputs.image }}", "redis-memory-${{ matrix.target }}",
                      'smoke_mcp.py "$IMAGE"', "scope-isolation:", "native-python:"):
            self.assertIn(value, workflow)
        for trigger in ("tools/image_security/**", ".github/actions/plugin-image-security/**"):
            self.assertEqual(workflow.count(trigger), 2)
        self.assertIn("coverage.py", gate.policy_files())

    def test_debian_epoch_and_revision_from_scanner(self):
        from image_inventory import trivy_inventory
        report = {"Results": [{"Class": "os-pkgs", "Type": "debian", "Packages": [
            {"Name": "bsdutils", "Version": "2.41.5", "Release": "0+deb13u1", "Epoch": 1,
             "Arch": "amd64"}]}]}
        self.assertIn(("bsdutils", "1:2.41.5-0+deb13u1", None), trivy_inventory(report)["debian"])

    def test_osv_batch_modified_precision_and_applicable_fix(self):
        import supplier_coverage as suppliers
        self.assertTrue(suppliers.same_osv_revision("2026-11-22T10:00:02.260974Z",
                                                  "2026-11-22T10:00:02.260974192Z"))
        self.assertFalse(suppliers.same_osv_revision("2026-11-22T10:00:02.260973Z",
                                                   "2026-11-22T10:00:02.260974192Z"))
        package = {"ecosystem": "crates.io", "name": "test-crate"}
        record = {"affected": [{"package": package, "ranges": [{"type": "SEMVER", "events": [
            {"introduced": "1.0.0"}, {"fixed": "1.1.5"}, {"introduced": "2.0.0"}, {"fixed": "2.0.4"}]}]}]}
        self.assertEqual(suppliers.fixed_version(record, package, "2.0.1"), "2.0.4")

    def test_supplier_node_branch_and_platform(self):
        from supplier_coverage import check_node
        from unittest.mock import Mock
        rows = {"1": {"vulnerable": "22.x || 24.x", "patched": "^22.23.4 || ^24.22.1",
                      "affectedEnvironments": ["all"], "severity": "high", "cve": ["CVE-2026-1234"]}}
        sources = Mock()
        sources.fetch.return_value = (json.dumps(rows).encode(), "source-ref")
        result = {"findings": []}
        check_node({"name": "Node.js", "version": "24.22.0"}, result, sources)
        self.assertEqual(len(result["findings"]), 1)
        check_node({"name": "Node.js", "version": "24.22.1"}, result, sources)
        self.assertEqual(len(result["findings"]), 1)

    def test_node_unknown_record_keeps_later_known_findings(self):
        from supplier_coverage import check_node
        from unittest.mock import Mock
        records = {"broken": {"vulnerable": "nonsense", "patched": "0.0.0", "affectedEnvironments": ["all"]},
                   "valid": {"vulnerable": ">= 24.0.0", "patched": "^24.22.1", "cve": ["CVE-2026-1234"],
                             "affectedEnvironments": ["linux"], "severity": "critical"}}
        source = Mock()
        source.fetch.return_value = (json.dumps(records).encode(), "response")
        result = {"errors": [], "findings": []}
        check_node({"name": "Node.js", "version": "24.22.0"}, result, source)
        self.assertIn("broken", " ".join(result["errors"]))
        self.assertEqual([r["VulnerabilityID"] for r in result["findings"]], ["CVE-2026-1234"])

    def test_malformed_package_keeps_previous_inventory(self):
        dpkg = b"Package: base\nVersion: 1\nArchitecture: amd64\nStatus: install ok installed\n"
        rootfs = self.tar({"var/lib/dpkg/status": dpkg, "usr/lib/os-release": b"ID=debian\n",
                           "usr/local/lib/python3.12/site-packages/bad.dist-info/METADATA": b"Name: bad\n"})
        with patch.object(producer, "export_rootfs", return_value=rootfs):
            result = producer.collect_coverage("redis-memory-mcp", dict(self.candidate, platform="linux/amd64"),
                                               rootfs, {}, self.root)
        self.assertIn("base", [c["name"] for c in result["components"]])
        self.assertIn("bad.dist-info", " ".join(result["errors"]))
        self.assertFalse(result["complete"])

    def test_virtual_cargo_workspace_requires_exact_members(self):
        from image_inventory import workspace_evidence
        path = "opt/src/Cargo.lock"
        files = {"opt/src/Cargo.toml": b'[workspace]\nmembers=["a"]\n',
                 "opt/src/a/Cargo.toml": b'[package]\nname="a"\nversion="1.0.0"\n',
                 path: b'[[package]]\nname="a"\nversion="1.0.0"\n'}
        refs = [{"Name": "a", "Version": "1.0.0", "ID": "a@1.0.0"},
                {"Relationship": "root", "ID": "workspace", "DependsOn": ["a@1.0.0"]}]
        self.assertEqual(workspace_evidence(path, refs, files, files.__getitem__)["package_index"], 1)
        refs[1]["DependsOn"].append("other@1.0")
        self.assertIsNone(workspace_evidence(path, refs, files, files.__getitem__))
