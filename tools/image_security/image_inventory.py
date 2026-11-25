"""Qualify producer payloads from a stopped candidate. Never execute image code."""

import csv
import hashlib
import io
import json
import posixpath
import re
import subprocess
import tarfile
import tomllib
import zipfile
from collections import defaultdict
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from advisories import demand
import native_evidence as native_proof

SOURCE = re.compile(r"([a-z0-9][a-z0-9.+-]*)(?: \(([^() ]+)\))?")
NODE_LIMIT = 256 * 1024 * 1024
LIMIT = 32 * 1024 * 1024

COPY_ADVISORIES = {
    "setuptools/package_index": {"CVE-2025-47273", "GHSA-5rjg-fvgr-3xxf", "PYSEC-2025-49"},
    "msgpack C extension": {"CVE-2026-114613", "GHSA-j586-36cw-2gc2", "GHSA-6v7p-g79w-8964"},
}
PYTHON_IMPLEMENTATIONS = {"setuptools": "setuptools/package_index", "msgpack": "msgpack C extension"}
SOURCE_ONLY_TARGETS = {"opt/atlas-seed/vectorlink/Cargo.lock", "usr/share/doc/knowledge-atlas/lance-7.0.0/Cargo.lock"}


def cargo_registry_component(path, identity, material):
    match = re.fullmatch(r"registry/src/[^/]+/([A-Za-z0-9_-]+)-(\d+\.\d+\.\d+(?:[-+].+)?)", path)
    if match and re.fullmatch("[0-9a-f]{64}", identity.get("sha256", "")):
        return dict(kind="compiled Cargo crate", name=match[1], version=match[2], origin=material, input=path,
                    crate_sha256=identity["sha256"], applicability="conservative build superset")
    return None


def valid_cargo_checkout(path, identity):
    return path.startswith("git/checkouts/") and re.fullmatch("[0-9a-f]{40}", identity.get("revision", ""))


def export_rootfs(candidate, destination):
    import gate
    def docker(*args, timeout=60):
        process = subprocess.run(["docker", *args], env=gate.docker_env(), capture_output=True,
                                 text=True, timeout=timeout)
        demand(process.returncode == 0, f"docker {args[0]}: {process.stderr.strip()}")
        return process.stdout.strip()
    ident = docker("create", "--network=none", "--entrypoint=/bin/false", candidate["id"])
    demand(re.fullmatch(r"[0-9a-f]{64}", ident), "invalid disposable container ID")
    try:
        demand(docker("inspect", "--format", "{{.Image}} {{.State.Status}}", ident)
               == candidate["id"] + " created", "stopped inventory container identity changed")
        docker("export", "--output", str(destination), ident, timeout=900)
        return destination
    finally:
        docker("rm", "--volumes", ident)


def trivy_inventory(report):
    packages = defaultdict(set)
    for result in (report or {}).get("Results", []):
        for package in result.get("Packages", []):
            if package.get("Name") and package.get("Version"):
                version = package["Version"]
                if result.get("Class") == "os-pkgs":
                    if package.get("Epoch"):
                        version = f'{package["Epoch"]}:{version}'
                    if package.get("Release"):
                        version += "-" + package["Release"]
                packages[result["Type"]].add((package["Name"].lower().replace("_", "-"),
                                              version, package.get("FilePath")))
    return packages


def workspace_evidence(target, packages, files, read):
    """Match Trivy's anonymous grouping to an exact virtual Cargo workspace."""
    root = PurePosixPath(target).parent
    manifest = str(root / "Cargo.toml")
    if manifest not in files or target not in files:
        return None
    config = tomllib.loads(read(manifest).decode())
    members = config.get("workspace", {}).get("members", [])
    if "package" in config or not members:
        return None
    lock = tomllib.loads(read(target).decode())
    locked = {(v["name"], v["version"]) for v in lock.get("package", []) if "source" not in v}
    named = {(p.get("Name"), p.get("Version")): p.get("ID") for p in packages}
    ids = set()
    inputs = {}
    for member in members:
        if not isinstance(member, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", member) or member in (".", ".."):
            return None
        path = str(root / member / "Cargo.toml")
        if path not in files:
            return None
        raw = read(path)
        pkg = tomllib.loads(raw.decode()).get("package", {})
        ident = pkg.get("name"), pkg.get("version")
        if ident not in locked or not named.get(ident):
            return None
        ids.add(named[ident])
        inputs[path] = hashlib.sha256(raw).hexdigest()
    roots = [(i, p) for i, p in enumerate(packages) if not p.get("Name") and not p.get("Version")
             and p.get("Relationship") == "root" and isinstance(p.get("DependsOn"), list)
             and sorted(p["DependsOn"]) == sorted(ids)]
    if len(roots) != 1:
        return None
    i, row = roots[0]
    return {"target": target, "package_index": i, "row": row, "member_manifests": inputs,
            "manifest_sha256": hashlib.sha256(read(manifest)).hexdigest(), "lock_sha256": hashlib.sha256(read(target)).hexdigest()}


def inventory(producer, rootfs, report, result=None):
    if result is None:
        result = {}
    result.update({"complete": False, "components": [], "errors": [], "findings": [],
              "ubuntu_sources": {}, "native_outputs": [], "os": {}, "queries": []})
    errors, components = result["errors"], result["components"]
    scanned = trivy_inventory(report)
    atlas = producer == "knowledge-atlas"
    with tarfile.open(rootfs, "r:") as tar:
        files = {m.name.removeprefix("./"): m for m in tar if m.isfile() or m.islnk()}
        def read(name):
            member = files[name]
            demand(member.size <= LIMIT, f"oversized metadata: {name}")
            content = tar.extractfile(member).read(LIMIT + 1)
            demand(len(content) <= LIMIT, f"oversized metadata: {name}")
            return content

        def component(kind, name, version, origin, **extra):
            row = dict(kind=kind, name=name, version=version, origin=origin, **extra)
            components.append(row)
            return row

        def query(row, package, version=None):
            result["queries"].append({"component": len(components)-1, "package": package,
                                      "version": version or row["version"]})

        def checked(kind, name, version, path, scanner, ecosystem):
            matches = scanned[scanner]
            ident = name.lower().replace("_", "-")
            listed = (ident, version, path) in matches
            row = component(kind, name, version, path, trivy_listed=listed)
            if not listed:
                query(row, {"ecosystem": ecosystem, "name": name})

        for name in ("usr/lib/os-release", "etc/os-release"):
            if name in files:
                result["os"] = dict((k, v.strip('"')) for k, sep, v in
                                    (line.partition("=") for line in read(name).decode().splitlines()) if sep)
                break
        distro = result["os"].get("ID")
        if distro != ("ubuntu" if atlas else "debian") or (atlas and result["os"].get("VERSION_CODENAME") != "noble"):
            errors.append(f"unexpected base OS: {result['os']}")
        sources = defaultdict(list)
        owners = {}
        for path in files:
            if path.startswith("var/lib/dpkg/info/") and path.endswith(".list"):
                owner = path.rsplit("/", 1)[1][:-5].split(":")[0]
                owners.update({line.lstrip("/"): owner for line in read(path).decode().splitlines()})
        for paragraph in read("var/lib/dpkg/status").split(b"\n\n") if "var/lib/dpkg/status" in files else []:
            pkg = BytesParser().parsebytes(paragraph)
            if pkg.get("Status") != "install ok installed":
                continue
            name, version, arch = (pkg.get(k) for k in ("Package", "Version", "Architecture"))
            source = SOURCE.fullmatch(pkg.get("Source", name or ""))
            demand(source and name and version and arch, "invalid installed dpkg identity")
            binary = dict(name=name, version=version, architecture=arch, source_version=source[2] or version)
            sources[source[1]].append(binary)
            listed = (name, version, None) in scanned[distro]
            component("dpkg", name, version, "var/lib/dpkg/status", source=source[1],
                      source_version=binary["source_version"], architecture=arch, trivy_listed=listed)
            if not listed:
                errors.append(f"dpkg {name} {version} absent from {distro} scanner inventory")
        if not sources:
            errors.append("missing installed dpkg inventory")
        if atlas:
            result["ubuntu_sources"] = dict(sources)
        pyroots, installed = set(), set()
        wheel_owners = {}
        sboms = {}
        npm_root = "usr/local/lib/node_modules/npm/package.json"
        for path in files:
            if path.endswith(".dist-info/METADATA") and "/site-packages/" in path:
                meta = BytesParser().parsebytes(read(path))
                name, version = meta.get("Name"), meta.get("Version")
                demand(name and version, f"invalid Python distribution {path}")
                root = path.split("/site-packages/")[0]
                pyroots.add(root)
                installed.add(name.lower())
                checked("python", name, version, path, "python-pkg", "PyPI")
                record = path.rsplit("/", 1)[0] + "/RECORD"
                if record in files:
                    for row in csv.reader(io.StringIO(read(record).decode())):
                        if row:
                            wheel_owners[root + "/site-packages/" + row[0]] = (name, version)
            elif path.startswith("usr/local/lib/node_modules/npm/") and path.endswith("/package.json"):
                # Only package roots. npm also ships test fixtures, tool configuration and nested subpaths.
                if not re.search(r"/node_modules/(?:@[^/]+/)?[^/]+/package\.json$", path):
                    continue
                try:
                    meta = json.loads(read(path))
                    demand(isinstance(meta, dict) and all(isinstance(meta.get(k), str) and meta[k].strip()
                           for k in ("name", "version")), "invalid npm package identity")
                    demand(path != npm_root or meta["name"] == "npm", "wrong npm root")
                    checked("npm", meta["name"], meta["version"], path, "node-pkg", "npm")
                except (UnicodeError, ValueError) as exc:
                    errors.append(f"{path}: {exc}")
            elif "/pip/_vendor/" in path and path.endswith("/vendor.txt"):
                for line in read(path).decode().splitlines():
                    match = re.match(r"^([\w.-]+)==([\w.+-]+)", line.strip())
                    if match:
                        prefix = path.removesuffix("vendor.txt")
                        bom = prefix + "bom.cdx.json"
                        impl = pip_implementation(match[1], (p[len(prefix):] for p in files if p.startswith(prefix)),
                                                  read(path), read(bom) if bom in files else None,
                                                  lambda p: read(prefix + p))
                        row = component("vendored in pip", match[1], match[2], path, implementation=impl)
                        query(row, {"ecosystem": "PyPI", "name": match[1]})
            elif "/ensurepip/_bundled/" in path and path.endswith(".whl"):
                with zipfile.ZipFile(io.BytesIO(read(path))) as wheel:
                    manifest = "pip/_vendor/vendor.txt"
                    if manifest not in wheel.namelist():
                        errors.append(f"{path}: missing bootstrap vendor manifest")
                        continue
                    for line in wheel.read(manifest).decode().splitlines():
                        match = re.match(r"^([\w.-]+)==([\w.+-]+)", line.strip())
                        if match:
                            prefix = "pip/_vendor/"
                            bom = prefix + "bom.cdx.json"
                            impl = pip_implementation(match[1], (p[len(prefix):] for p in wheel.namelist() if p.startswith(prefix)),
                                                      wheel.read(manifest), wheel.read(bom) if bom in wheel.namelist() else None,
                                                      lambda p: wheel.read(prefix + p))
                            row = component("vendored in bootstrap wheel", match[1], match[2], path + "!" + manifest,
                                            sha256=hashlib.sha256(read(path)).hexdigest(), implementation=impl)
                            query(row, {"ecosystem": "PyPI", "name": match[1]})
        for required in (("opt/knowledge-atlas/.venv/", "opt/atlas-seed/python/") if atlas else ("usr/local/",)):
            if not any(root.startswith(required) for root in pyroots):
                errors.append("missing Python environment " + required)
        for required in ("numpy", "tokenizers", *(("onnxruntime",) if atlas or producer.endswith("embeddings") else ())):
            if required not in installed:
                errors.append("missing installed Python distribution " + required)
        native = result["native_outputs"]
        for path, member in files.items():
            if member.islnk():
                member = files.get(member.linkname.removeprefix("./"), member)
            if member.islnk():
                errors.append(f"{path}: unresolved hardlink target {member.linkname}")
                continue
            if member.size < 4:
                continue
            stream = tar.extractfile(member)
            header = stream.read(8)
            if not (header.startswith(b"\x7fELF") or header == b"!<arch>\n"):
                continue
            h = hashlib.sha256(header)
            for chunk in iter(lambda: stream.read(1024*1024), b""):
                h.update(chunk)
            item = {"path": path, "sha256": h.hexdigest(), "size": member.size, "dpkg": owners.get(path) or owners.get(path.removeprefix("usr/")),
                    "wheel": wheel_owners.get(path)}
            native.append(item)
        native_paths = {n["path"] for n in native}
        python = [p for p in native_paths if re.search(r"/bin/python3\.\d+$", p) and (p.startswith("opt/uv/") if atlas else p.startswith("usr/local/"))]
        versions = set()
        for header in (p for p in files if p.endswith("/patchlevel.h") and
                       (p.startswith("opt/uv/") if atlas else p.startswith("usr/local/include/"))):
            match = re.search(rb'^#define\s+PY_VERSION\s+"(\d+\.\d+\.\d+)"', read(header), re.M)
            if match:
                versions.add(match[1].decode())
        if not python or len(versions) != 1:
            errors.append("copied CPython binary or exact version header missing")
        else:
            version = versions.pop()
            for item in native:
                if item["path"] in python:
                    component("interpreter", "CPython", version, item["path"], sha256=item["sha256"],
                              advisory_source="PSF")
                    item["disposition"] = "identified CPython interpreter for PSF release check"
            result["python_version"] = version
        for required, pattern in (("OpenBLAS", r"(?i)openblas[^/]*\.so"), ("tokenizers", r"/tokenizers/[^/]+\.so"),
                                  ("numpy", r"/numpy/_core/[^/]+\.so")):
            if not any(re.search(pattern, p) for p in native_paths):
                errors.append(f"missing bundled {required} output")
        if (atlas or producer.endswith("embeddings")) and not any("/onnxruntime/" in p for p in native_paths):
            errors.append("missing ONNX runtime native output")
        if atlas:
            for label, pattern in (("Node", r"^usr/local/bin/node$"), ("SWI", r"/libswipl\.so\.\d"),
                                   ("extractor", r"/poc9-extract$"), ("TerminusDB/VectorLink/Lance", r"/librust\.so$")):
                if not any(re.search(pattern, p) for p in native_paths):
                    errors.append("missing " + label + " output")
            material = "usr/share/doc/knowledge-atlas/cargo-inventory.json"
            if material not in files:
                errors.append("missing Rust compiler/output inventory including static GMP")
            else:
                cargo = json.loads(read(material))
                for path, identity in cargo.items():
                    row = cargo_registry_component(path, identity, material)
                    if row:
                        components.append(row)
                        query(row, {"ecosystem": "crates.io", "name": row["name"]})
                    elif valid_cargo_checkout(path, identity):
                        component("compiled Cargo Git checkout", path, identity["revision"], material)
                        result["queries"].append({"component": len(components)-1, "commit": identity["revision"]})
                    else:
                        component("Cargo build input", path, identity.get("revision"), material)
                        errors.append(f"unresolved Cargo build identity {path}")
                if not cargo:
                    errors.append("empty Rust output inventory")
            result["retained_source_locks"] = sorted(p for p in files if p.endswith("/Cargo.lock"))
            result["source_lock_packages"] = {}
            for path in result["retained_source_locks"]:
                lock = tomllib.loads(read(path).decode())
                result["source_lock_packages"][path] = [[p["name"], p["version"]] for p in lock.get("package", [])]
        for path in files:
            if path.endswith(".json") and ".dist-info/sboms/" in path:
                try:
                    site, name = path.rsplit("/site-packages/", 1)
                    dist = name.split(".dist-info/", 1)[0]
                    project, version = dist.rsplit("-", 1)
                    owner = project.lower().replace("_", "-"), version
                    raw = read(path)
                    digest = hashlib.sha256(raw).hexdigest()
                    result.setdefault("wheel_sboms", []).append({"path": path, "sha256": digest, "owner": owner})
                    matched_member = any(item["path"].startswith(site + "/site-packages/") and
                                         (binding := native_proof.WHEELS.get(item["sha256"])) and
                                         binding[:2] == owner and binding[-1] == digest and
                                         item["path"].endswith("/" + binding[2]) for item in native)
                    if not matched_member:
                        document = json.loads(raw)
                        entries = document.get("components")
                        demand(isinstance(entries, list) and entries, "empty or malformed SBOM")
                        for dep in entries:
                            if not isinstance(dep, dict) or not all(isinstance(dep.get(k), str) and dep[k]
                                                                  for k in ("name", "version", "purl")):
                                errors.append(f"{path}: malformed supplier-declared component")
                                continue
                            row = component("wheel SBOM", dep["name"], dep["version"], path, sha256=digest,
                                            purl=dep["purl"], applicability="requires matching native output")
                            if dep["purl"].startswith("pkg:cargo/") and "download_url=" not in dep["purl"]:
                                query(row, {"ecosystem": "crates.io", "name": dep["name"]})
                            elif dep["name"] == "openssl":
                                result["queries"].append({"component": len(components)-1, "repository": "openssl/openssl",
                                                          "tag": "openssl-" + dep["version"]})
                            else:
                                errors.append(f"{path}: unresolved upstream identity {dep['purl']}")
                    native_proof.sbom_packages(raw, owner)
                    sboms[(site + "/site-packages/", *owner, digest)] = raw
                except (KeyError, ValueError, TypeError) as exc:
                    errors.append(f"{path}: invalid wheel SBOM: {exc}")
        for item in native:
            try:
                native_proof.wheel_inputs(item, sboms, result) or native_proof.runtime_inputs(item, result)
            except (TypeError, ValueError, KeyError) as exc:
                errors.append(f"{item['path']}: {exc}")
        if atlas and not any(c["origin"] == npm_root and c["kind"] == "npm" for c in components):
            errors.append(f"missing checked npm root {npm_root}")
        if atlas:
            node = "usr/local/bin/node"
            node_item = next((i for i in native if i["path"] == node), None)
            if node_item and node_item["size"] <= NODE_LIMIT:
                binary = tar.extractfile(files[node]).read(NODE_LIMIT + 1)
                demand(len(binary) <= NODE_LIMIT, "oversized Node interpreter")
                versions = set(re.findall(rb"https://nodejs.org/download/release/v([0-9.]+)/node-v\1\.tar\.gz\x00", binary))
                if len(versions) == 1:
                    component("interpreter", "Node.js", versions.pop().decode(), node, sha256=node_item["sha256"],
                              advisory_source="Node Security WG and official security releases")
                    node_item["disposition"] = "identified Node interpreter for supplier release check"
                else:
                    errors.append(f"{node}: missing unambiguous embedded release URL")
            elif node in native_paths:
                errors.append(f"{node}: exceeds Node version inspection limit")
            libs = [p for p in native_paths if re.search(r"/libswipl\.so\.\d+\.\d+\.\d+$", p)]
            if len(libs) != 1:
                errors.append("missing unambiguous SWI release library")
            elif libs[0].endswith(".so.10.0.2"):
                native_proof.swi_browser(files, read, result)
            for path in libs:
                version = path.rpartition(".so.")[2]
                lib = next(i for i in native if i["path"] == path)
                row = component("SWI runtime", "SWI-Prolog", version, path, sha256=lib["sha256"])
                source = "usr/share/doc/knowledge-atlas/swipl-10.0.2.tar.gz"
                if version == "10.0.2" and source in files and hashlib.sha256(read(source)).hexdigest() == (
                        "e42cc098f7b8a6051c4f79a99b55162d467098aba60f69649bdc7583f0734b57"):
                    result["queries"].append({"component": len(components)-1,
                                              "commit": "69e12af81c17ae5925f7555234c6575adf3a1e85"})
                    if lib["sha256"] in ("9f76b6c7b6de7c30523513989c3a41a54205b0fb0560fcd6a47df39d0ad72e33",
                                         "870cce6b87fd9b314b521cbd54af636cddc7127eec0e95b0c887295bfdfa8eef"):
                        lib["disposition"] = "matched SWI build and release source"
                    row["cpe"] = "swi-prolog:swi-prolog:10.0.2"
                else:
                    errors.append(f"{path}: source archive or release mapping unavailable")
            pack_paths = [p for p in files if re.search(r"/lib/swipl/pack/[^/]+/pack.pl$", p)]
            if not any(p.endswith("/pack/tus/pack.pl") for p in pack_paths):
                errors.append("missing expected SWI tus pack root")
            for path in pack_paths:
                raw = read(path).decode()
                version = re.search(r"^version\('([^']+)'\)\.", raw, re.M)
                name = path.split("/")[-2]
                if version:
                    component("SWI pack", name, version[1], path, sha256=hashlib.sha256(read(path)).hexdigest())
                prefix = path.removesuffix("pack.pl")
                members = {p[len(prefix):]: hashlib.sha256(read(p)).hexdigest() for p in files if p.startswith(prefix)}
                if name == "tus" and version and version[1] == "0.0.17" and members == native_proof.MATERIAL["tus"]:
                    components[-1]["advisory_repository"] = "terminusdb-org/tus"
                    result["queries"].append({"component": len(components)-1,
                                              "commit": "033ab8fd46db0b7b29bdfc7f038cde30eac22a1d"})
                else:
                    errors.append(f"{path}: unresolved SWI pack source")
            manifest_path = "opt/atlas-seed/runtime.json"
            if manifest_path in files:
                manifest = json.loads(read(manifest_path))
                result["build_revision"] = manifest.get("source_revision")
                for key, version in manifest.get("components", {}).items():
                    component("runtime source", key, version, manifest_path)
                    if re.fullmatch("[0-9a-f]{40}", version):
                        result["queries"].append({"component": len(components)-1, "commit": version})
                    else:
                        errors.append(f"missing runtime source revision {key}")
                outputs = {n["path"]: n["sha256"] for n in native}
                for suffix in ("terminusdb/src/rust/librust.so", "extract/target/debug/poc9-extract"):
                    expected = manifest.get("artifacts", {}).get(suffix)
                    if not expected or outputs.get("opt/atlas-seed/" + suffix) != expected:
                        errors.append("Rust output SHA-256 differs from runtime build manifest: " + suffix)
            else:
                errors.append("missing runtime output/build manifest")
            if material in files and manifest_path in files:
                qualify_cargo(result, files, read, cargo, material, manifest)
            for path in files:
                if re.search(r"/corresponding-source/gmp-mpfr-sys-.*\.crate$", path):
                    raw = read(path)
                    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as source:
                        names = [m.name for m in source if m.isfile() and m.name.endswith("/gmp-h.in")]
                    versions = {m[1] for p in names if (m := re.search(r"/gmp-(\d+\.\d+\.\d+)-c/", p))}
                    if len(versions) == 1:
                        gmp = component("static library source", "GMP", versions.pop(), path,
                                        sha256=hashlib.sha256(raw).hexdigest())
                        graph = next((i for i in native if i["path"].endswith("/terminusdb/src/rust/librust.so")), None)
                        if (graph and result.get("cargo_outputs", {}).get("static_gmp") and
                                hashlib.sha256(raw).hexdigest() == "13eabc29d16e4a621b495e3919c71ebb7caaed24380955671e7d417370fea95d"):
                            gmp.update(cpe="gmplib:gmp:6.2.1", origin=graph["path"], sha256=graph["sha256"],
                                       input=path, input_sha256=hashlib.sha256(raw).hexdigest(),
                                       patched_gmp="07a4142e4c5b476c09f89782be35fe13f955d18c6312f934d1b52340ffa4535d")
                            copy = {"origin": graph["path"], "sha256": graph["sha256"], "input": path}
                            result["findings"].append({"VulnerabilityID": "CVE-2021-43618", "PkgName": "GMP",
                                "InstalledVersion": "6.2.1", "Severity": "HIGH", "Status": "unknown",
                                "Evidence": {"cna_sha256": "7a6c68aa659b3d7c1aa4c72ed2dd355da80fa931ae06238e6dffaa4f6830cca6",
                                             "patch_sha256": "278560197b7a9bfa1289ef1e555ef656aaff74fe59f6da086eab593846ab765f"},
                                "DataSource": {"ID": "GMP supplier source"}, "Copy": copy,
                                "ImplementationDisposition": {"status": "unaffected", "copy": copy,
                                    "source": "mpz/inp_raw.c sha256 07a4142e4c5b476c09f89782be35fe13f955d18c6312f934d1b52340ffa4535d"}})
                        else:
                            errors.append("GMP: source and static link not matched to graph output")
                    else:
                        errors.append("GMP: unrecognized retained native source")
            if not any(c["name"] == "GMP" for c in components):
                errors.append("missing GMP source/build identity")
        result["virtual_workspaces"] = []
        for target in (report or {}).get("Results", []):
            if target.get("Type") == "cargo" and any(not p.get("Name") for p in target.get("Packages", [])):
                proof = workspace_evidence(target["Target"], target["Packages"], files, read)
                if proof:
                    result["virtual_workspaces"].append(proof)
        checked_packages = {c["name"] for c in components if c["kind"] == "dpkg" and c["trivy_listed"]}
        # Resolve shared imports inside the candidate, never against host libraries.
        native_by_path = {i["path"]: i for i in native}
        library_names = defaultdict(list)
        links = {m.name.removeprefix("./"): m.linkname for m in tar.getmembers() if m.issym()}
        for path in native_by_path.keys() | links.keys():
            if not re.fullmatch(r"(?:usr/)?lib(?:64)?/(?:[a-zA-Z0-9_-]+-linux-gnu/)?[^/]+", path):
                continue
            target = path
            for _ in range(16):
                if target not in links:
                    break
                link = links[target]
                target = posixpath.normpath(posixpath.join(posixpath.dirname(target), link)).lstrip("/")
                if target.startswith(("lib/", "lib64/")) and target not in native_by_path and target not in links:
                    target = "usr/" + target
            library = native_by_path.get(target)
            if library and library["dpkg"] in checked_packages:
                library_names[posixpath.basename(path)].append({k: library[k] for k in ("path", "sha256", "dpkg")})
        for item in native:
            needed = native_proof.reviewed_imports(item)
            if needed:
                item["dynamic_imports"] = {name: library_names[name] for name in needed}
                missing = [name for name in needed if not library_names[name]]
                if missing:
                    errors.append(f"{item['path']}: missing inventoried OS imports {missing}")
            if item["dpkg"] in checked_packages:
                item.setdefault("disposition", "installed dpkg package in scanner inventory")
            if "disposition" not in item:
                item["disposition"] = "unresolved"
                errors.append(f"{item['path']}: unresolved native output/build inputs")
        result["native_unowned"] = [n["path"] for n in native if not n["dpkg"]]
    return result


def pip_implementation(name, paths, manifest, bom, read_member):
    """Describe the implementation inside just this installed or zipped pip vendor root."""
    normalized = {p.removeprefix("./") for p in paths}
    proof = {"manifest_sha256": hashlib.sha256(manifest).hexdigest()}
    if bom:
        document = json.loads(bom)
        proof["bom_sha256"] = hashlib.sha256(bom).hexdigest()
        proof["bom_packages"] = [[d.get("name"), d.get("version"), d.get("purl")]
                                 for d in document.get("components", []) if d.get("name") == name]
    if name == "setuptools":
        members = sorted(p for p in normalized if p.startswith(("pkg_resources/", "setuptools/")))
        if "pkg_resources/__init__.py" in members and not any(p.startswith("setuptools/") for p in members):
            proof.update(absent="setuptools/package_index", members=members, member_sha256={
                "pkg_resources/__init__.py": hashlib.sha256(read_member("pkg_resources/__init__.py")).hexdigest()})
    if name == "msgpack":
        members = sorted(p for p in normalized if p.startswith("msgpack/"))
        if "msgpack/fallback.py" in members and not any("_cmsgpack" in p or p.endswith((".so", ".pyd"))
                                                        for p in members):
            proof.update(absent="msgpack C extension", members=members, member_sha256={
                "msgpack/fallback.py": hashlib.sha256(read_member("msgpack/fallback.py")).hexdigest()})
    return proof


def finding_ids(row):
    vendors = row.get("VendorIDs", [])
    return {v for v in [row.get("VulnerabilityID"), *(vendors if isinstance(vendors, list) else [])]
            if isinstance(v, str)}


def copy_disposition(row, copy):
    absent = copy.get("implementation", {}).get("absent")
    identifiers = finding_ids(row)
    if (copy.get("name") == "libgfortran" and copy.get("sha256") ==
            "25899a29a06d3f2927021b4823d858558839fea0e7e759eedb8926fd0a339ea4" and
            str(copy.get("origin")).endswith("/numpy.libs/libgfortran-e1b7dfc8-d8198c01.so.5.0.0") and
            row.get("VulnerabilityID") in {"CVE-2026-114690", "CVE-2026-114691"}):
        return {"status": "unaffected", "source": "CNA identifies libstdc++ implementation", "copy": copy}
    expected = COPY_ADVISORIES.get(absent, set())
    if identifiers and identifiers <= expected:
        return {"status": "unaffected", "source": "identified implementation absent", "copy": copy,
                "advisory_ids": sorted(expected)}
    return None


def trivy_disposition(row, origin, inventory):
    name, version = row.get("PkgName"), row.get("InstalledVersion")
    if origin["type"] == "cargo":
        evidence = inventory.get("cargo_outputs")
        lock = inventory.get("source_lock_packages", {}).get(origin["target"], [])
        if (origin["target"] in SOURCE_ONLY_TARGETS and evidence and [name, version] in lock and [name, version] not in evidence["compiled"]
                and [name, version] not in evidence["active_lock_packages"]):
            return {"status": "source only", "source": origin["target"], "package": [name, version],
                    "output_hashes": evidence["output_hashes"], "inventory_sha256": evidence["inventory_sha256"]}
        return None
    package = origin.get("package") or {}
    if origin["type"] != "python-pkg" or package.get("AnalyzedBy") != "sbom":
        return None
    ident = row.get("PkgIdentifier", {})
    matches = [c for c in inventory.get("components", []) if c["name"].lower() == str(name).lower()
               and c["version"] == version and c["kind"] in ("python", "wheel SBOM", "vendored in pip", "vendored in bootstrap wheel")]
    if not matches or not ident.get("UID") or package.get("Identifier") != ident:
        return None
    # A grouped SBOM finding can only lose its release reason when every possible
    # installed copy has matching BOM identity and proves the same implementation absent.
    dispositions = [copy_disposition(row, c) for c in matches]
    if (any([name, version, ident.get("PURL")] in c.get("implementation", {}).get("bom_packages", [])
            for c in matches) and all(dispositions)):
        return {"status": "unaffected", "source": "matched pip BOM components and all candidate copies",
                "copies": matches, "advisory_ids": dispositions[0]["advisory_ids"]}
    return None


def qualify_cargo(result, files, read, cargo, material, manifest):
    digest = hashlib.sha256(read(material)).hexdigest()
    roots = ["extract/target/debug/poc9-extract", "terminusdb/src/rust/librust.so"]
    native = {r["path"]: r for r in result["native_outputs"]}
    outputs = {s: native.get("opt/atlas-seed/" + s, {}).get("sha256") for s in roots}
    receipt_path = "usr/share/doc/knowledge-atlas/cargo-outputs.json"
    historical = {"6d86df36625188c85359bcaf0bcd8868a22d5e0dae9f236715b068cef9cc5b93":
                  "6f55884b6e3d0ed1ba7bc2d1d525f2337571640792ddfa106f6a844ae0581d4f",
                  "ea03a77ae21244f8eed5b2d0d5cb8753fd6ca5a8562bbe30a0120327e975465f":
                  "9841fc17d68e619114f5185a79a699bcb96b5ff0c79c586a6e5b2277d339151d"}
    try:
        demand(cargo and all(v and manifest["artifacts"].get(k) == v for k,v in outputs.items()),
               "missing or inconsistent Cargo output identities")
        if receipt_path in files:
            receipt = json.loads(read(receipt_path))
            recipe = "opt/knowledge-atlas/docker/build-seed.py"
            trusted_recipe = Path(__file__).resolve().parents[2] / "knowledge-atlas/docker/build-seed.py"
            demand(recipe in files and hashlib.sha256(read(recipe)).hexdigest() == receipt["recipe_sha256"]
                   == hashlib.sha256(trusted_recipe.read_bytes()).hexdigest(), "unverified compiler receipt producer")
            demand(receipt["inventory_sha256"] == digest and receipt["compiled_count"] == len(cargo)
                   and set(receipt["outputs"]) == set(roots), "partial Cargo receipt")
            for i, relative in enumerate(roots):
                record = receipt["outputs"][relative]
                name, kind = [("poc9-extract", "bin"), ("terminusdb_dylib", "cdylib")][i]
                demand((record["name"], record["kind"], record["sha256"]) == (name, kind, outputs[relative])
                       and record["filenames"] and record["manifest"].endswith("/Cargo.toml"),
                       "mismatched compiler target")
            static_gmp = any(r["package_id"].endswith("#gmp-mpfr-sys@1.5.3") and "static=gmp" in r["linked_libs"]
                             for r in receipt["native_links"])
        else:
            demand(digest in historical and outputs[roots[1]] == historical[digest]
                   and outputs[roots[0]] == "56ad65efc47ceeff5573cd27eb01b0006014e971f32e446d93b498d695351c70",
                   "no matching output-bound compiler inventory")
            static_gmp = True
        locks = result["source_lock_packages"]
        active = [p for folder in ("extract", "terminusdb/src/rust")
                  for p in locks["opt/atlas-seed/" + folder + "/Cargo.lock"]]
        demand(active and not any(c["kind"] == "Cargo build input" for c in result["components"]),
               "unresolved Cargo build inputs")
        compiled = [[c["name"], c["version"]] for c in result["components"] if c["kind"] == "compiled Cargo crate"]
        demand("lance" in {n for n, _ in compiled} and "gmp-mpfr-sys" in {n for n, _ in compiled},
               "compiled graph-library inputs absent")
        demand(all(p in active for p in compiled), "compiled crate absent from both active build locks")
        result["cargo_outputs"] = {"output_hashes": outputs, "inventory_sha256": digest, "compiled": compiled,
                                   "active_lock_packages": active, "static_gmp": static_gmp}
        for relative in roots:
            native["opt/atlas-seed/" + relative]["disposition"] = "output-bound compiler inventory"
    except (KeyError, TypeError, ValueError) as exc:
        result["errors"].append(f"Cargo compiler/output coverage: {exc}")


def finding_targets(report):
    """Select the reported findings for which this project has applicability evidence."""
    python, cargo = set(), set()
    results = report.get("Results") if isinstance(report, dict) else None
    for target in results if isinstance(results, list) else ():
        if not isinstance(target, dict) or not isinstance(target.get("Vulnerabilities"), list):
            continue
        for row in target["Vulnerabilities"]:
            if not isinstance(row, dict):
                continue
            name, version = row.get("PkgName"), row.get("InstalledVersion")
            if not isinstance(name, str) or not isinstance(version, str):
                continue
            kind, path = target.get("Type"), target.get("Target")
            if kind == "cargo" and isinstance(path, str) and path in SOURCE_ONLY_TARGETS:
                cargo.add(path)
            if kind == "python-pkg" and name in PYTHON_IMPLEMENTATIONS:
                if finding_ids(row) & COPY_ADVISORIES[PYTHON_IMPLEMENTATIONS[name]]:
                    python.add((name, version))
    return python, cargo


def targeted_inventory(rootfs, inspect_swi, python, cargo, result):
    with tarfile.open(rootfs, "r:") as tar:
        files = {m.name.removeprefix("./"): m for m in tar if m.isfile() or m.islnk() or m.issym()}

        def read(path):
            member = files[path]
            demand(member.size <= LIMIT, f"oversized metadata: {path}")
            with tar.extractfile(member) as stream:
                raw = stream.read(LIMIT + 1)
            demand(len(raw) <= LIMIT, f"oversized metadata: {path}")
            return raw

        if inspect_swi and any(path.endswith("/libswipl.so.10.0.2") for path in files):
            swi = {"findings": result["findings"], "errors": result["limitations"]}
            native_proof.swi_browser(files, read, swi)
        if python:
            result["applicability"]["components"] = python_copy_evidence(files, read, python)
        if cargo:
            outputs = []
            for name in ("extract/target/debug/poc9-extract", "terminusdb/src/rust/librust.so"):
                path = "opt/atlas-seed/" + name
                with tar.extractfile(files[path]) as stream:
                    outputs.append({"path": path, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()})
            collect_cargo_evidence(files, read, cargo, outputs, result)


def python_copy_evidence(files, read, requested):
    copies = []

    def vendor_copies(manifest, names, read_member, origin, kind, **identity):
        bom = "bom.cdx.json"
        for line in manifest.decode().splitlines():
            match = re.match(r"^([\w.-]+)==([\w.+-]+)", line.strip())
            if match and (match[1], match[2]) in requested:
                impl = pip_implementation(match[1], names, manifest, read_member(bom) if bom in names else None,
                                          read_member)
                copies.append(dict(kind=kind, name=match[1], version=match[2], origin=origin,
                                   implementation=impl, **identity))

    requested_names = {name for name, _ in requested}
    for path in files:
        if path.endswith("/pip/_vendor/vendor.txt"):
            prefix = path.removesuffix("vendor.txt")
            names = [p[len(prefix):] for p in files if p.startswith(prefix)]
            vendor_copies(read(path), names, lambda name: read(prefix + name), path, "vendored in pip")
        elif "/ensurepip/_bundled/" in path and path.rsplit("/", 1)[-1].startswith("pip-") and path.endswith(".whl"):
            raw = read(path)
            with zipfile.ZipFile(io.BytesIO(raw)) as wheel:
                prefix = "pip/_vendor/"
                manifest = prefix + "vendor.txt"
                paths = wheel.namelist()
                demand(manifest in paths, f"missing bootstrap vendor manifest: {path}")
                names = [p[len(prefix):] for p in paths if p.startswith(prefix)]
                vendor_copies(wheel.read(manifest), names, lambda name: wheel.read(prefix + name),
                              path + "!" + manifest, "vendored in bootstrap wheel",
                              sha256=hashlib.sha256(raw).hexdigest())
        elif re.search(r"/(?:site|dist)-packages/[^/]+\.(?:dist-info/METADATA|egg-info/PKG-INFO|egg-info)$", path):
            distribution = re.split(r"\.(?:dist-info|egg-info)", path.rsplit("-packages/", 1)[1])[0]
            if any(distribution.lower() == name or distribution.lower().startswith(name + "-")
                   for name in requested_names):
                metadata = BytesParser().parsebytes(read(path))
                name, version = metadata.get("Name"), metadata.get("Version")
                demand(name and version, f"invalid Python distribution: {path}")
                if (name.lower(), version) in requested:
                    copies.append(dict(kind="python", name=name, version=version, origin=path))
        elif ".dist-info/sboms/" in path and path.endswith(".json"):
            raw = read(path)
            for row in json.loads(raw).get("components", []):
                if (row.get("name"), row.get("version")) in requested:
                    copies.append(dict(kind="wheel SBOM", name=row["name"], version=row["version"], origin=path,
                                       sha256=hashlib.sha256(raw).hexdigest()))
    return copies


def collect_cargo_evidence(files, read, targets, outputs, result):
    material = "usr/share/doc/knowledge-atlas/cargo-inventory.json"
    cargo = json.loads(read(material))
    source_locks = targets | {"opt/atlas-seed/extract/Cargo.lock", "opt/atlas-seed/terminusdb/src/rust/Cargo.lock"}
    findings = {"components": [cargo_registry_component(path, identity, material) or {"kind": "Cargo build input"}
                             for path, identity in cargo.items() if not valid_cargo_checkout(path, identity)],
                "native_outputs": outputs, "errors": [], "source_lock_packages": {
                    path: [[p["name"], p["version"]] for p in tomllib.loads(read(path).decode()).get("package", [])]
                    for path in source_locks}}
    qualify_cargo(findings, files, read, cargo, material, json.loads(read("opt/atlas-seed/runtime.json")))
    if "cargo_outputs" in findings:
        result["applicability"].update(cargo_outputs=findings["cargo_outputs"],
                                      source_lock_packages=findings["source_lock_packages"])
    else:
        result["limitations"].extend(findings["errors"])
