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
from image_inventory import inventory, trivy_disposition
from ubuntu_coverage import ubuntu_record


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
        return inventory("knowledge-atlas", image, self.trivy)

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
            ubuntu_record(raw, cve, "noble", self.source, rows, proofs, errors)
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
        ubuntu_record(raw, "CVE-2026-1234", "noble", self.source, rows, proofs, errors)
        self.assertEqual(rows, [])
        ubuntu_record(raw + b"noble_example: ignored (end of life, was needs-triage)\n",
                               "CVE-2026-1234", "noble", self.source, rows, proofs, errors)
        self.assertFalse(errors)
        self.assertEqual(rows[0]["VendorEvidence"]["applicability"], "potentially affected")

    def test_retained_atlas_swi_finding_survives_unavailable_local_diagnostics(self):
        candidate = dict(self.candidate, id="sha256:51fadf91d930e65cbd21da768700c7328c2c104a15b069a1c3fdbbf77c83c971",
                         revision="cbd1bc08beb2ef49cf0ba682d639bfbfe09bc0b9")
        with patch.object(producer, "export_rootfs", side_effect=OSError("export unavailable")):
            result = producer.collect_coverage("knowledge-atlas", candidate, None, self.root)
        self.assertFalse(result["errors"])
        row, = result["findings"]
        self.assertEqual(row["VulnerabilityID"], "CVE-2017-17524")
        self.assertEqual(row["Evidence"]["candidate_id"], candidate["id"])
        self.assertEqual(row["Copy"]["sha256"], "48553cb33a4660f112a83e73097c4591ea5059864ac4235af3dc24e425cdab0f")
        decision = advisories.evaluate(row, set(), advisories.Aliases(None), supplier_identity=True)
        self.assertEqual(decision["reasons"], ["HIGH with fix"])

    def test_redis_targets_scan_the_built_images_and_keep_both_reports(self):
        workflow = (gate.HERE.parents[1] / ".github/workflows/redis-memory.yml").read_text()
        for value in ("fail-fast: false", "target: [mcp, embeddings]", '--target "$TARGET"',
                      "--iidfile", "--platform linux/amd64", "uses: ./.github/actions/plugin-image-security",
                      "image: ${{ steps.build.outputs.image }}", "redis-memory-${{ matrix.target }}",
                      'smoke_mcp.py "$IMAGE"', "scope-isolation:", "native-python:"):
            self.assertIn(value, workflow)
        images = workflow.split("\n  images:\n", 1)[1].split("\n  native-python:\n", 1)[0]
        setup = images.index("uses: ./.github/actions/plugin-image-security/setup-docker")
        self.assertLess(images.index("uses: actions/checkout@"), setup)
        self.assertLess(setup, images.index("docker build"))
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

    def test_malformed_targeted_copy_evidence_preserves_original_finding(self):
        rootfs = self.tar({"usr/local/lib/python3.12/ensurepip/_bundled/pip-26.3-py3-none-any.whl": b"bad zip"})
        row = dict(VulnerabilityID="CVE-2025-47273", PkgName="setuptools", InstalledVersion="70.3.0")
        report = {"Results": [{"Type": "python-pkg", "Vulnerabilities": [row]}]}
        with patch.object(producer, "export_rootfs", return_value=rootfs):
            coverage = producer.collect_coverage("redis-memory-mcp", dict(self.candidate, platform="linux/amd64"),
                                                 report, self.root)
        self.assertFalse(coverage["errors"])
        self.assertIn("applicability evidence unavailable", " ".join(coverage["limitations"]))
        self.assertIsNone(trivy_disposition(row, {"type": "python-pkg"}, coverage["applicability"]))
        self.assertEqual(report["Results"][0]["Vulnerabilities"], [row])

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

    def test_ubuntu_uncertainty_preserves_policy_and_required_evidence(self):
        for priority, status, kev, blocked, gap in (
            ("critical", "needs-triage", False, True, False),
            ("high", "needs-triage", False, False, True),
            ("medium", "ignored (end of life, was needs-triage)", False, False, False),
            ("low", "not-affected (1.9)", True, True, False),
            ("bad", "needs-triage", True, True, True),
            ("high", "released (1:2.1)", False, True, False),
        ):
            with self.subTest(priority=priority, status=status):
                rows, proofs, errors = [], [], []
                cve = "CVE-2026-2345"
                ubuntu_record(f"Candidate: {cve}\nPriority: {priority}\nnoble_example: {status}\n".encode(),
                                       cve, "noble", self.source, rows, proofs, errors)
                self.assertEqual(len(rows), 1)
                decision = advisories.evaluate(rows[0], {cve} if kev else set(), advisories.Aliases(None))
                self.assertEqual(bool(decision["reasons"]), blocked)
                self.assertEqual(bool(errors or decision["errors"]), gap)
                self.assertEqual(rows[0]["VendorEvidence"]["vendor_status"], status)

    def test_malformed_batch_keeps_independent_matches_and_copy(self):
        from unittest.mock import Mock
        import supplier_coverage as suppliers
        result = {"queries": [], "components": [], "errors": [], "findings": []}
        for i in range(3):
            result["queries"].append({"component": i, "package": {"ecosystem": "PyPI", "name": str(i)}, "version": "1"})
            result["components"].append({"name": str(i), "version": "1", "origin": f"copy/{i}"})
        source = Mock()
        source.fetch.side_effect = [(json.dumps({"results": [{}, {"vulns": {}}, {"vulns": [
            {"id": "CVE-2026-2345", "modified": "2026-01-01T00:00:00Z"}]}]}).encode(), "batch"),
                                    ValueError("detail unavailable")]
        suppliers.osv_checked(result, source)
        self.assertGreaterEqual(len(result["errors"]), 2)
        row, = result["findings"]
        self.assertEqual(row["Copy"]["origin"], "copy/2")
        self.assertIn("CISA KEV", advisories.evaluate(row, {"CVE-2026-2345"}, advisories.Aliases(None))["reasons"])

    def test_partial_node_ranges_and_mismatched_osv_package(self):
        import supplier_coverage as suppliers
        self.assertTrue(suppliers.node_range("24.22.1", "<=24"))
        self.assertFalse(suppliers.node_range("24.22.1", ">24"))
        with self.assertRaises(ValueError):
            suppliers.fixed_version({"affected": []}, {"name": "example", "ecosystem": "PyPI"}, "1")

    def test_unowned_elf_and_empty_sbom_are_explicit_gaps(self):
        path = "opt/custom/libfoo.so"
        result = self.inspect({path: b"\x7fELFfake",
                               "x.dist-info/sboms/test.json": b'{"bomFormat":"CycloneDX","components":[]}'})
        self.assertIn(path, " ".join(result["errors"]))
        self.assertIn("test.json", " ".join(result["errors"]))

    def test_npm_root_and_swi_release_are_mandatory(self):
        result = self.inspect({"usr/local/lib/node_modules/npm/node_modules/a/package.json": b"{}",
                               "one/libswipl.so.10": b"\x7fELFtest"})
        errors = " ".join(result["errors"])
        for evidence in ("npm/package.json", "a/package.json", "SWI release"):
            self.assertIn(evidence, errors)

    def test_redis_local_build_and_scan_failure_is_propagated(self):
        import subprocess
        text = (gate.HERE / "README.md").read_text().split("### Linux amd64")[1].split("```sh")[1].split("```")[0]
        for failed in ("build/mcp", "scan/mcp", "scan/embeddings"):
            log = self.root / "calls"
            log.write_text("")
            script = """
git() { echo revision; }
docker() { shift; while [ "$1" != "--target" ]; do shift; done; t=$2; echo build/$t >> "$LOG"; [ "build/$t" != "$FAIL" ] || return 1; echo example > "$4"; }
python3() { case "$1" in -c) echo 1;; *) echo scan/$target >> "$LOG"; [ "scan/$target" != "$FAIL" ];; esac; }
"""
            import os
            proc = subprocess.run(["sh", "-c", script + text], env=dict(os.environ, LOG=str(log), FAIL=failed,
                                                                        TMPDIR=str(self.root)))
            self.assertNotEqual(proc.returncode, 0, failed)
            self.assertIn("build/embeddings", log.read_text())

    def test_swi_backport_is_required_in_runtime_artifacts(self):
        source = (gate.HERE.parents[1] / "knowledge-atlas/src/knowledge_atlas/runtime.py").read_text()
        self.assertIn("native-tools/swipl/lib/swipl/library/www_browser.pl", source)

    def test_pip_implementation_needs_all_matching_copies_and_advisory(self):
        from image_inventory import pip_implementation, trivy_disposition, copy_disposition
        ident = {"UID": "per-package-uid", "PURL": "pkg:pypi/setuptools@70.3.0"}
        bom = json.dumps({"components": [{"name": "setuptools", "version": "70.3.0", "purl": ident["PURL"]}]}).encode()
        impl = pip_implementation("setuptools", ["pkg_resources/__init__.py"], b"setuptools==70.3.0", bom,
                                  lambda _: b"pkg_resources source")
        copies = [dict(name="setuptools", version="70.3.0", origin=p, kind=k, implementation=impl)
                  for p, k in (("installed/vendor.txt", "vendored in pip"),
                               ("bootstrap.whl!vendor.txt", "vendored in bootstrap wheel"))]
        origin = {"type": "python-pkg", "target": "Python", "package": {"AnalyzedBy": "sbom", "Identifier": ident}}
        row = {"VulnerabilityID": "CVE-2025-47273", "PkgName": "setuptools", "InstalledVersion": "70.3.0",
               "PkgIdentifier": ident}
        self.assertEqual(trivy_disposition(row, origin, {"components": copies})["copies"], copies)
        mixed = copies + [dict(name="setuptools", version="70.3.0", origin="other/METADATA", kind="python")]
        self.assertIsNone(trivy_disposition(row, origin, {"components": mixed}))
        self.assertIsNone(copy_disposition(dict(row, VulnerabilityID="CVE-2020-12345"), copies[0]))
        self.assertIsNone(trivy_disposition(row, dict(origin, package={}), {"components": copies}))

    def test_oversized_node_requires_supplier_identity(self):
        import image_inventory
        with patch.object(image_inventory, "NODE_LIMIT", 4):
            r = self.inspect({"usr/local/bin/node": b"\x7fELFlarger-than-bound"})
        self.assertTrue(any("Node version inspection limit" in e for e in r["errors"]))

    def test_supplemental_severity_branch_and_local_identity(self):
        from unittest.mock import Mock
        import supplier_coverage as suppliers
        self.assertEqual(suppliers.osv_severity({"severity":[{"type":"CVSS_V3", "score":
            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]}), "CRITICAL")
        cna = {"affected":[{"product":"CPython","defaultStatus":"unaffected","versions":[
            {"version":"3.12.0","lessThan":"3.12.15","status":"affected"},
            {"version":"3.13.0","lessThan":"3.13.15","status":"affected"}]}]}
        self.assertEqual(suppliers.python_release_status(cna, "3.12.14"), ("affected", "3.12.15"))
        self.assertEqual(suppliers.python_release_status(cna, "3.12.16"), ("unaffected", ""))
        source = Mock()
        source.fetch.return_value = (json.dumps({"67": {"cve": [], "affectedEnvironments":["linux"],
            "severity":"high","vulnerable":"<=24","patched":"^24.23.1"}}).encode(), "source")
        r = {"findings":[],"errors":[]}
        suppliers.check_node({"name":"Node.js","version":"24.22.0","origin":"usr/local/bin/node"},r,source)
        row, = r["findings"]
        resolver = Mock()
        evaluated = advisories.evaluate(row,set(),resolver,supplier_identity=True)
        resolver.resolve.assert_not_called()
        self.assertEqual(evaluated["reasons"], ["HIGH with fix"])
        self.assertEqual(evaluated["aliases"]["status"], "no_published_cve")

    def test_wheel_sbom_rejects_partial_and_unreachable_graph(self):
        from native_evidence import sbom_packages
        root = {"name":"rpds-py","version":"1","bom-ref":"root","components":[
            {"bom-ref":"library","purl":"pkg:cargo/rpds-py@1#src/lib.rs"}]}
        doc = {"bomFormat":"CycloneDX","specVersion":"1.5","metadata":{"component":root},
               "components":[{"name":"crate","version":"1","bom-ref":"crate"}],
               "dependencies":[{"ref":"root","dependsOn":["crate"]}]}
        fn = lambda: sbom_packages(json.dumps(doc).encode(), ("rpds-py","1"))
        self.assertIn(("crate","1"), fn())
        doc["dependencies"][0]["dependsOn"] = ["unknown"]
        with self.assertRaises(ValueError):
            fn()
        doc["dependencies"] = []
        with self.assertRaises(ValueError):
            fn()

    def test_partial_cargo_inventory_cannot_dispose_source_only(self):
        from image_inventory import qualify_cargo, trivy_disposition
        material = "usr/share/doc/knowledge-atlas/cargo-inventory.json"
        roots = ["extract/target/debug/poc9-extract","terminusdb/src/rust/librust.so"]
        outputs = {s: "7"*64 for s in roots}
        result = {"native_outputs":[{"path":"opt/atlas-seed/"+s,"sha256":h} for s,h in outputs.items()],
                  "errors":[], "components":[], "source_lock_packages":{}}
        qualify_cargo(result, {material}, lambda _:b'{}', {}, material, {"artifacts":outputs})
        self.assertIn("Cargo compiler/output coverage", str(result["errors"]))
        row = {"PkgName":"a","InstalledVersion":"1"}
        self.assertIsNone(trivy_disposition(row,{"target":"source/Cargo.lock","type":"cargo"},result))

    def test_node_release_bulletin_checks_missing_index_records(self):
        from unittest.mock import Mock
        import supplier_coverage as suppliers
        source = Mock()
        releases = [{"version":"v24.22.3","date":"2026-11-10","security":True},
                    {"version":"v24.22.0","date":"2026-10-14","security":False}]
        bulletin = b"### Issue (CVE-2026-123863) - (high)\nImpact:\nactive release lines: 24.x\n/blog/release/v24.22.3/"
        source.fetch.side_effect = [(json.dumps(releases).encode(),"releases"),(bulletin,"november")]
        r = {"findings":[],"errors":[]}
        suppliers.check_node_releases({"name":"Node.js","version":"24.22.0"},r,source)
        self.assertEqual((r["findings"][0]["VulnerabilityID"],r["findings"][0]["FixedVersion"]),("CVE-2026-123863","24.22.3"))
        self.assertFalse(r["errors"])

    def test_later_osv_match_survives_missing_kev_detail(self):
        from unittest.mock import Mock
        import supplier_coverage as suppliers
        modified = "2026-10-01T00:00:00Z"
        pkg = {"ecosystem":"PyPI","name":"example"}
        r = {"components":[{"name":"example", "version":"1.0", "origin":"first/METADATA"}],
             "queries":[{"component":0,"package":pkg,"version":"1.0"}],"errors":[],"findings":[]}
        ids = ["CVE-2026-1234","CVE-2026-1235"]
        batch = {"results":[{"vulns":[{"id":i,"modified":modified} for i in ids]}]}
        detail = {"id":ids[1],"modified":modified,"aliases":[],"severity":[{"type":"CVSS_V3","score":"9.8"}],
                  "affected":[{"package":pkg,"ranges":[{"type":"ECOSYSTEM","events":[{"introduced":"0"},{"fixed":"1.1"}]}]}]}
        src = Mock()
        src.fetch.side_effect = [(json.dumps(batch).encode(),"batch"),OSError("detail unavailable"),
                                 (json.dumps(detail).encode(),"good")]
        suppliers.osv_checked(r,src)
        decisions = [advisories.evaluate(row,{ids[0]},advisories.Aliases(None)) for row in r["findings"]]
        self.assertEqual([a["reasons"] for a in decisions],[["CISA KEV"],["CRITICAL"]])
        self.assertEqual(r["findings"][1]["FixedVersion"],"1.1")
        self.assertIn("detail unavailable",str(r["errors"]))

    def test_cargo_semver_and_python_prerelease_matching(self):
        from supplier_coverage import package_version
        self.assertLess(package_version("1.2.0-beta.2","crates.io","SEMVER"),
                        package_version("1.2.0-beta.11","crates.io","SEMVER"))
        self.assertEqual(package_version("1.2.0+build.1","crates.io","SEMVER"),
                         package_version("1.2.0","crates.io","SEMVER"))
        for lower, higher in [("1.0.dev3","1.0a1"),("1.0rc1","1.0"),("1.0","1.0.post1")]:
            self.assertLess(package_version(lower,"PyPI","ECOSYSTEM"),package_version(higher,"PyPI","ECOSYSTEM"))
