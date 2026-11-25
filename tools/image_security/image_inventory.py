"""Qualify producer payloads from a stopped candidate. Never execute image code."""

import csv
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tomllib
import zipfile
from collections import defaultdict
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from advisories import demand

SOURCE = re.compile(r"([a-z0-9][a-z0-9.+-]*)(?: \(([^() ]+)\))?")
LIMIT = 32 * 1024 * 1024


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
                meta = json.loads(read(path))
                if meta.get("name") and meta.get("version"):
                    checked("npm", meta["name"], meta["version"], path, "node-pkg", "npm")
            elif "/pip/_vendor/" in path and path.endswith("/vendor.txt"):
                for line in read(path).decode().splitlines():
                    match = re.match(r"^([\w.-]+)==([\w.+-]+)", line.strip())
                    if match:
                        row = component("vendored in pip", match[1], match[2], path)
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
                            row = component("vendored in bootstrap wheel", match[1], match[2], path + "!" + manifest)
                            query(row, {"ecosystem": "PyPI", "name": match[1]})
        for required in (("opt/knowledge-atlas/.venv/", "opt/atlas-seed/python/") if atlas else ("usr/local/",)):
            if not any(root.startswith(required) for root in pyroots):
                errors.append("missing Python environment " + required)
        for required in ("numpy", "tokenizers", *(("onnxruntime",) if atlas or producer.endswith("embeddings") else ())):
            if required not in installed:
                errors.append("missing installed Python distribution " + required)
        native = result["native_outputs"]
        for path, member in files.items():
            if member.size < 4 or not (member.mode & 0o111 or ".so" in PurePosixPath(path).name
                                       or path.endswith(".a")):
                continue
            stream = tar.extractfile(member)
            header = stream.read(8)
            if not (header.startswith(b"\x7fELF") or header == b"!<arch>\n"):
                continue
            h = hashlib.sha256(header)
            for chunk in iter(lambda: stream.read(1024*1024), b""):
                h.update(chunk)
            item = {"path": path, "sha256": h.hexdigest(), "dpkg": owners.get(path) or owners.get(path.removeprefix("usr/")),
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
            component("interpreter", "CPython", version, sorted(python), advisory_source="PSF")
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
                    match = re.fullmatch(r"registry/src/[^/]+/(.+)-(\d+\.\d+\.\d+(?:[-+].+)?)", path)
                    if match and re.fullmatch("[0-9a-f]{64}", identity.get("sha256", "")):
                        row = component("compiled Cargo crate", match[1], match[2], material, input=path,
                                        crate_sha256=identity["sha256"], applicability="conservative build superset")
                        query(row, {"ecosystem": "crates.io", "name": match[1]})
                    elif path.startswith("git/checkouts/") and re.fullmatch("[0-9a-f]{40}", identity.get("revision", "")):
                        component("compiled Cargo Git checkout", path, identity["revision"], material)
                        result["queries"].append({"component": len(components)-1, "commit": identity["revision"]})
                    else:
                        component("Cargo build input", path, identity.get("revision"), material)
                        errors.append(f"unresolved Cargo build identity {path}")
                if not cargo:
                    errors.append("empty Rust output inventory")
            result["retained_source_locks"] = sorted(p for p in files if p.endswith("/Cargo.lock"))
        for path in files:
            if path.endswith(".json") and ".dist-info/sboms/" in path:
                document = json.loads(read(path))
                if document.get("bomFormat") != "CycloneDX":
                    errors.append(f"unrecognized wheel SBOM {path}")
                    continue
                for dep in document.get("components", []):
                    name, version = dep.get("name"), dep.get("version")
                    if not name or not version:
                        errors.append(f"missing bundled identity in {path}")
                        continue
                    row = component("wheel SBOM", name, version, path, purl=dep.get("purl"))
                    if dep.get("purl", "").startswith("pkg:cargo/") and "download_url=" not in dep["purl"]:
                        query(row, {"ecosystem": "crates.io", "name": name})
                    elif name == "openssl":
                        query(row, {"ecosystem": "GIT", "name": "https://github.com/openssl/openssl"}, "openssl-" + version)
                    else:
                        errors.append(f"{path}: unresolved upstream advisory identity {name} {version}")
        opens = [p for p in native_paths if re.search(r"(?i)openblas[^/]*\.so", p)]
        for path in opens:
            site = path.rpartition("/numpy.libs/")[0]
            config = site + "/numpy/__config__.py"
            match = re.search(rb'"openblas configuration":\s*r?"OpenBLAS ([\w.+-]+)', read(config)) if config in files else None
            if match:
                version = match[1].decode()
                row = component("bundled native library", "OpenBLAS", version, path, build_configuration=config)
                if re.fullmatch(r"\d+\.\d+\.\d+", version):
                    query(row, {"ecosystem": "GIT", "name": "https://github.com/OpenMathLib/OpenBLAS"}, "v" + version)
                else:
                    errors.append(f"{path}: scipy-openblas build {version} needs supplier revision/advisory mapping")
            else:
                errors.append(f"{path}: bundled OpenBLAS version unavailable")
        for item in native:
            if item["wheel"] and any(part in item["path"] for part in ("tokenizers/", "onnxruntime/")):
                name, version = item["wheel"]
                row = component("native wheel output", name, version, item["path"], sha256=item["sha256"])
                query(row, {"ecosystem": "PyPI", "name": name})
                site = item["path"].split("/site-packages/")[0] + "/site-packages/"
                if not any(p.startswith(site + name.replace("-", "_") + "-") and ".dist-info/sboms/" in p for p in files):
                    errors.append(f"{item['path']}: {name} {version} lacks build-matched bundled dependency inventory")
            elif ".libs/" in item["path"] and item["path"] not in opens:
                errors.append(f"{item['path']}: wheel-bundled native library needs supplier build identity")
        if atlas:
            node = "usr/local/bin/node"
            if node in native_paths and files[node].size <= 256*1024*1024:
                binary = tar.extractfile(files[node]).read()
                versions = set(re.findall(rb"https://nodejs.org/download/release/v([0-9.]+)/node-v\1\.tar\.gz\x00", binary))
                if len(versions) == 1:
                    component("interpreter", "Node.js", versions.pop().decode(), node)
                else:
                    errors.append(f"{node}: missing unambiguous embedded release URL")
            libs = [p for p in native_paths if re.search(r"/libswipl\.so\.\d+\.\d+\.\d+$", p)]
            for path in libs:
                version = path.rpartition(".so.")[2]
                row = component("SWI runtime", "SWI-Prolog", version, path)
                query(row, {"ecosystem": "GIT", "name": "https://github.com/SWI-Prolog/swipl-devel"}, "V" + version)
            pack_paths = [p for p in files if re.search(r"/lib/swipl/pack/[^/]+/pack.pl$", p)]
            if not pack_paths:
                errors.append("missing SWI pack manifests")
            for path in pack_paths:
                raw = read(path).decode()
                version = re.search(r"^version\('([^']+)'\)\.", raw, re.M)
                name = path.split("/")[-2]
                if version:
                    component("SWI pack", name, version[1], path)
                errors.append(f"{path}: exact SWI pack revision and supported advisory coverage required")
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
            for path in files:
                if re.search(r"/corresponding-source/gmp-mpfr-sys-.*\.crate$", path):
                    raw = read(path)
                    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as source:
                        names = [m.name for m in source if m.isfile() and m.name.endswith("/gmp-h.in")]
                    versions = {m[1] for p in names if (m := re.search(r"/gmp-(\d+\.\d+\.\d+)-c/", p))}
                    if len(versions) == 1:
                        component("static library source", "GMP", versions.pop(), path, sha256=hashlib.sha256(raw).hexdigest())
                        errors.append("GMP: verify shipped static build flags, backports and upstream advisory status")
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
        result["native_unowned"] = [n["path"] for n in native if not n["dpkg"]]
    return result
