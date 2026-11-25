"""Match reviewed supplier inputs to each installed native member by name and bytes."""

import hashlib
import json
from pathlib import Path

from advisories import demand

MATERIAL = json.loads(Path(__file__).with_name("native-sources.json").read_text())
WHEELS = {
    "c116fcf1e80d461ce0a35c332974f25949e8359416f50b3d53371810d2ce1ccc":
        ("tokenizers", "0.22.2", "tokenizers/tokenizers.abi3.so", "369cc9fc8cc10cb24143873a0d95438bb8ee257bb80c71989e3ee290e8d72c67", None),
    "fa049ce975669d8a90fb48960f412e626fa54cf596c2f75d6820949f4888e910":
        ("tokenizers", "0.22.2", "tokenizers/tokenizers.abi3.so", "2249487018adec45d6e3554c71d46eb39fa8ea67156c640f7513eb26f318cec7", None),
    "b370edad9e013363856697167609e0b6d43f509d79117b6ee4b4a0e192e6c56e":
        ("hf-xet", "1.8.0", "hf_xet/hf_xet.abi3.so", "29dab7c904cd1914a0439c6e1306c12c7497c8bd32290fbe563ebf51ca4b6d6d",
         "cd96414a2916a5fb1bfb289957a0233a474cec6249c5712c371243957b08e99d"),
    "1e3c483ab69b4b0637cbcbf0a7e3989e350f8195388724ce4f7d8efe4b6fe05e":
        ("rpds-py", "2026.9.1", "rpds/rpds.cpython-312-x86_64-linux-gnu.so", "addeda51556dac7c1a2f14cda62db8b621cd12afba3091d03a96c72932387eab",
         "0c8885a9d45f3c9280aeb1e06701077036748b22bd6e3a4201fe09e6b73f99b5"),
    "4ac1622d643ff188f4f355cc89a439f6881c397d766911e1246f9f4a215d051d":
        ("protobuf", "7.36.2", "google/_upb/_message.abi3.so", "89f23aa53c24553a2416fd4fd1ec06f74fa42b14b546d8883128813f775bbfd2", None),
}

# OSV commit queries identify the upstream tree. Tagged and snapshot inputs also
# carry their own repository so their native advisories are checked independently.
UPSTREAM = {
    "tokenizers": [("Oniguruma", "6.9.10", "kkos/oniguruma", "v6.9.10"),
                   ("esaxx", "0.1.10 source", None, "dacdea35f18b551cb23bd3bf6ab58363784a94f4")],
    "hf-xet": [("AWS-LC", "5.7.0", "aws/aws-lc", "v5.7.0"),
               ("jitterentropy", "3.6.3", "smuellerDD/jitterentropy-library", "v3.6.3"),
               ("s2n-bignum bundled snapshot", "aws-lc-sys 0.45.0", None, "02561621ffa4cf17c0c4f70bc11a82df36b42ae9")],
    "protobuf": [("upb", "protobuf 36.2", None, "2c74169b34066ceb8ddb6b882fcb3fb32d737a55"),
                 ("utf8_range", "protobuf 36.2", None, "2c74169b34066ceb8ddb6b882fcb3fb32d737a55"),
                 ("Abseil core headers", "20250512.1", None, "76bb24329e8bf5f39704eb10d21b9a80befa7c81")],
}

def sbom_packages(raw, owner):
    doc = json.loads(raw)
    demand(doc["bomFormat"] == "CycloneDX" and doc["specVersion"] == "1.5", "unsupported wheel SBOM")
    root = doc["metadata"]["component"]
    demand(root["name"].lower().replace("_", "-") == owner[0] and root["version"] == owner[1]
           and any(t.get("purl", "").endswith("#src/lib.rs") for t in root["components"]),
           "wheel SBOM library root/target mismatch")
    deps = doc["components"]
    demand(isinstance(deps, list) and deps, "empty wheel SBOM dependencies")
    nodes = [root, *root["components"], *deps]
    refs = {n["bom-ref"] for n in nodes}
    demand(len(nodes) == len(refs), "duplicate SBOM reference")
    edges = {}
    for edge in doc["dependencies"]:
        links = edge.get("dependsOn", [])
        demand(edge["ref"] in refs and edge["ref"] not in edges and isinstance(links, list)
               and set(links) <= refs, "invalid SBOM dependency reference")
        edges[edge["ref"]] = links
    seen, pending = set(), [root["bom-ref"]]
    while pending:
        ref = pending.pop()
        if ref not in seen:
            seen.add(ref)
            pending.extend(edges.get(ref, []))
    demand(all(n["bom-ref"] in seen for n in deps), "unreachable SBOM dependency")
    return {(n["name"], n["version"]): next((h["content"] for h in n.get("hashes", [])
                                             if h["alg"] == "SHA-256"), None) for n in deps}


def add_source(result, item, name, version, request, **proof):
    index = len(result["components"])
    result["components"].append(dict(kind="native build input", name=name, version=version, origin=item["path"],
                                     sha256=item["sha256"], **proof))
    result["queries"].append(dict(component=index, **request))


def wheel_inputs(item, sboms, result):
    match = WHEELS.get(item["sha256"])
    if not match:
        return False
    name, version, member, wheel, bom = match
    demand(item["path"].endswith("/site-packages/" + member) and item["wheel"] and
           (item["wheel"][0].lower().replace("_", "-"), item["wheel"][1]) == (name, version),
           "supplier wheel member/owner mismatch")
    platform = ("linux/arm64" if item["sha256"] ==
                "fa049ce975669d8a90fb48960f412e626fa54cf596c2f75d6820949f4888e910" else "linux/amd64")
    demand(not result.get("platform") or result["platform"] == platform, "supplier wheel platform mismatch")
    item["platform"] = platform
    item["supplier_wheel_sha256"] = wheel
    site = item["path"].rsplit("/site-packages/", 1)[0] + "/site-packages/"
    linked = {}
    if bom:
        raw = sboms.get((site, name, version, bom))
        demand(raw is not None, "missing matching supplier SBOM")
        linked = sbom_packages(raw, (name, version))
    lock = MATERIAL["locks"].get(name)
    if lock:
        entries = {(n, v): h for n, v, h in lock["packages"]}
        demand(all(k in entries and entries[k] == h for k, h in linked.items()), "SBOM/lock checksum mismatch")
        for (n, v), h in entries.items():
            request = ({"package": {"ecosystem": "crates.io", "name": n}, "version": v}
                       if h else {"commit": lock["revision"]})
            add_source(result, item, n, v, request, lock_sha256=lock["sha256"], input_sha256=h,
                       source=lock["source"], applicability="reachable SBOM dependency" if (n,v) in linked
                       else "conservative build superset")
    for n, v, repo, rev in UPSTREAM.get(name, []):
        req = {"repository": repo, "tag": rev} if repo else {"commit": rev}
        snapshot_projects = {"s2n-bignum bundled snapshot": "awslabs/s2n-bignum",
                             "utf8_range": "protocolbuffers/utf8_range"}
        add_source(result, item, n, v, req, supplier_wheel_sha256=wheel,
                   advisory_repository=snapshot_projects.get(n))
    item["disposition"] = "supplier build inputs"
    return True

OPENBLAS = {
    "e58b9ec0d373252b70f37d9dc891d7183a4f57f062db87f38e9a1bf7442d3037":
        ("libscipy_openblas64_-f552eb69.so", "0.3.34.106.0", "446c436e10450c348169808f1a6b3fae0925c9f7",
         "e038d5a53076f901a6f7175447df0b3fa015b31c49eba12c2709d550eee5af14"),
    "ad494a874a8241624183202c0313c67605d648c3b3d6807d899bd6b350347472":
        ("libscipy_openblas64_-4b78795a.so", "0.3.34.237.2", "1ad90fb122f1e5d06646c40d807c7d48f38f7d9b",
         "291e1e4f9ce4b60decbd2e29fd6a45d0270d84d936bb3321853e006325f719ff"),
}
SWI_BROWSER = "opt/atlas-seed/native-tools/swipl/lib/swipl/library/www_browser.pl"
SWI_OLD = "48553cb33a4660f112a83e73097c4591ea5059864ac4235af3dc24e425cdab0f"
SWI_FIXED = "4964d0c2f14238baa85c98626e73168a8a9f2b2bf527e931cc42b25015f1ea38"


def runtime_inputs(item, result):
    match = OPENBLAS.get(item["sha256"])
    if match:
        filename, version, revision, wheel = match
        demand(item["path"].endswith("/numpy.libs/" + filename), "OpenBLAS supplier path mismatch")
        for n, v, req in (("OpenBLAS", version, {"commit": revision}),
                          ("LAPACK", "3.12.0", {"repository": "Reference-LAPACK/lapack", "tag": "v3.12.0"})):
            cpe = "openblas_project:openblas:0.3.34" if n == "OpenBLAS" else "lapack_project:lapack:3.12.0"
            add_source(result, item, n, v, req, cpe=cpe, supplier_wheel_sha256=wheel, source_revision=revision)
        if filename.endswith("4b78795a.so"):
            version = "14.2.1-11.el8_10"
            add_source(result, item, "gcc-toolset-14-gcc-gfortran", version, {"version": version,
                       "package": {"name": "gcc-toolset-14-gcc-gfortran", "ecosystem": "AlmaLinux:8"}},
                       implementation="pow_r4_i8 and pow_r8_i8", rpm_sha256="72309da01a3f22e82b2158ff2b61018f2708018ca3d5ffbfa2d6da6754b9f863")
            add_source(result, item, "gfortran_compat.c", "scipy-openblas64 0.3.34.237.2",
                       {"commit": "596ed7c42625e6a85b03cd1112b019d2e4c4b350"})
        item["disposition"] = "matched supplier wheel and bundled inputs"
        return True
    if (item["sha256"] == "25899a29a06d3f2927021b4823d858558839fea0e7e759eedb8926fd0a339ea4"
            and item["path"].endswith("/numpy.libs/libgfortran-e1b7dfc8-d8198c01.so.5.0.0")):
        version = "8.5.0-28.el8_10.alma.1"
        add_source(result, item, "libgfortran", version, {"version": version, "package":
                   {"name": "libgfortran", "ecosystem": "AlmaLinux:8"}},
                   rpm_sha256="3f5398e86e1d919074f2b98db87c8f9b17d0698014775fba16bbab1387bb6f58")
        cna_records = {"CVE-2026-114690": "2383c4dd1bee0ce496f9c9cfdd161f3966e3ab2a481c0829e869ec2dd51b131f",
                       "CVE-2026-114691": "ed2f4d1f9b82e09f2c18b09d1996bc384b1a98929ae2d2a7b50f11b5e8db5fa6"}
        for cve, cna_hash in cna_records.items():
            result["findings"].append({"VulnerabilityID": cve, "PkgName": "libgfortran", "InstalledVersion": version,
                "Severity": "MEDIUM", "FixedVersion": "8.5.0-31.el8_10.alma.1", "Status": "fixed",
                "DataSource": {"ID": "AlmaLinux OVAL ALSA-2026:98115"}, "Copy": dict(item),
                "Evidence": {"vendor": "https://security.almalinux.org/oval/org.almalinux.alsa-8.xml.bz2",
                             "sha256": "52da040a91fa228031d094d29eb45ed87b1c76f7975a885e863269f554c319b4",
                             "cna_sha256": cna_hash},
                "ImplementationDisposition": {"status": "unaffected", "copy": dict(item),
                    "source": "Red Hat CNA identifies libstdc++; exact libgfortran lacks this implementation"}})
        item["disposition"] = "matched AlmaLinux RPM"
        return True
    return False


def swi_browser(files, read, result):
    if SWI_BROWSER not in files:
        result["errors"].append("missing SWI browser library")
        return
    digest = hashlib.sha256(read(SWI_BROWSER)).hexdigest()
    copy = dict(origin=SWI_BROWSER, sha256=digest)
    row = {"VulnerabilityID": "CVE-2017-17524", "PkgName": "SWI-Prolog", "InstalledVersion": "10.0.2",
           "Severity": "HIGH", "FixedVersion": "11.0.0", "Status": "fixed", "Copy": copy,
           "DataSource": {"ID": "SWI-Prolog and NVD"}, "Evidence": {
               "fix": "13adef3c445b5a2b276d962b40a9156d55b774b1",
               "nvd_sha256": "871d38e51dac44a09845fc8e5a11de24287ec7685cdecac48dafecd177f5f3e3"}}
    if digest == SWI_FIXED:
        row["ImplementationDisposition"] = dict(status="unaffected", copy=copy, source=row["Evidence"])
    elif digest != SWI_OLD:
        result["errors"].append("SWI browser library differs from reviewed source and backport")
    result["findings"].append(row)


def reviewed_imports(item):
    """DT_NEEDED entries already verified on the digest-matched supplier members."""
    wheel = WHEELS.get(item["sha256"])
    if wheel is None:
        return ()
    name = wheel[0]
    base = ["libgcc_s.so.1", "libpthread.so.0", "libc.so.6"]
    if name == "protobuf":
        return ["libstdc++.so.6", "libm.so.6", "libc.so.6"]
    if name == "rpds-py":
        return base + ["ld-linux-x86-64.so.2"]
    if name in ("hf-xet", "tokenizers"):
        shared = base + ["libm.so.6", "libdl.so.2"]
        if name == "tokenizers":
            shared.append("libstdc++.so.6")
        if item["sha256"] != "fa049ce975669d8a90fb48960f412e626fa54cf596c2f75d6820949f4888e910":
            shared.extend(["librt.so.1", "ld-linux-x86-64.so.2"])
        return shared
    return ()
