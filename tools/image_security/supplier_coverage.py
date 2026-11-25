"""Versioned OSV, PSF and Node checks. All requests are to fixed, public origins."""

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request

import advisories as adv

OSV = "https://api.osv.dev/v1/querybatch"
PSF = "https://codeload.github.com/psf/advisory-database/tar.gz/refs/heads/main"
NODE = "https://raw.githubusercontent.com/nodejs/security-wg/main/vuln/core/index.json"
ERRORS = (KeyError, TypeError, ValueError, OSError, EOFError, subprocess.SubprocessError, tarfile.TarError)


class Sources:
    def __init__(self, directory):
        self.directory = directory / "supplier"
        self.directory.mkdir()
        self.cache = {}

    def fetch(self, url, data=None):
        import gate
        payload = json.dumps(data, separators=(",", ":")).encode() if data is not None else None
        key = hashlib.sha256(url.encode() + b"\x00" + (payload or b"")).hexdigest()
        if key not in self.cache:
            request = Request(url, data=payload, headers={"User-Agent": "plugin-image-security",
                                                         "Content-Type": "application/json"})
            raw, effective = gate.fetch_advisory(request)
            meta = {"url": effective, "sha256": hashlib.sha256(raw).hexdigest(),
                    "fetched_at": datetime.now(timezone.utc).isoformat(), "request": data}
            (self.directory / (key + ".meta.json")).write_text(json.dumps(meta))
            (self.directory / key).write_bytes(raw)
            self.cache[key] = key
        return (self.directory / key).read_bytes(), key


def version_key(text):
    # Final releases, including the common PEP 440 pre-release spellings.
    m = re.fullmatch(r"v?(\d+(?:\.\d+){0,4})(?:(a|b|rc)(\d+))?", text)
    adv.demand(m, f"unsupported version syntax {text!r}")
    release = tuple(int(s) for s in m[1].split("."))
    return release + (0,) * (5-len(release)) + ({None: 3, "rc": 2, "b": 1, "a": 0}[m[2]], int(m[3] or 0))


def evidence_row(identifier, component, severity, fix, source, proof, aliases=()):
    return {"VulnerabilityID": identifier, "VendorIDs": list(aliases), "PkgName": component["name"],
            "InstalledVersion": component["version"], "Severity": severity,
            "FixedVersion": fix, "Status": "fixed" if fix else "affected",
            "DataSource": {"ID": source}, "Evidence": proof}


def cna_record(cve, sources):
    adv.demand(adv.CVE.fullmatch(cve), "invalid CVE identity")
    _, year, serial = cve.split("-")
    raw, ref = sources.fetch(f"https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/{year}/{serial[:-3]}xxx/{cve}.json")
    record = json.loads(raw)
    adv.demand(record["cveMetadata"]["cveId"] == cve and record["cveMetadata"]["state"] == "PUBLISHED",
               f"{cve}: unavailable published CNA record")
    return record["containers"]["cna"], ref


def cna_severity(record):
    ratings = [metric["baseSeverity"] for entry in record.get("metrics", []) for metric in entry.values()
               if isinstance(metric, dict) and metric.get("baseSeverity") in adv.SEVERITIES]
    return max(ratings, key=["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"].index) if ratings else "UNKNOWN"


def fixed_version(record, package, installed):
    fixed = set()
    uncertain = False
    for affected in record["affected"]:
        desc = affected.get("package", {})
        if (desc.get("ecosystem"), desc.get("name", "").lower().replace("_", "-")) != (
                package["ecosystem"], package["name"].lower().replace("_", "-")):
            continue
        for interval in affected.get("ranges", []):
            if interval["type"] not in ("SEMVER", "ECOSYSTEM"):
                uncertain = True
                continue
            start = None
            for event in interval["events"]:
                if "introduced" in event:
                    start = event["introduced"]
                elif "fixed" in event and start is not None:
                    finish = event["fixed"]
                    if (start == "0" or version_key(start) <= version_key(installed)) and version_key(installed) < version_key(finish):
                        fixed.add(finish)
                    start = None
                elif not set(event) <= {"last_affected", "limit"}:
                    uncertain = True
    if not fixed and uncertain:
        raise ValueError("fix applicability requires a verified upstream release mapping")
    return ", ".join(sorted(fixed))


def same_osv_revision(batch, detail):
    # querybatch timestamps are microsecond precision; detailed records retain nanoseconds.
    return adv.timestamp(batch) == adv.timestamp(detail)


def osv_checked(result, sources):
    by_query = defaultdict(list)
    for query in result["queries"]:
        request = {k: v for k, v in query.items() if k != "component"}
        by_query[json.dumps(request, sort_keys=True)].append(query["component"])
    requests = list(by_query)
    for begin in range(0, len(requests), 200):
        group = requests[begin:begin+200]
        pending = [(k, json.loads(k)) for k in group]
        try:
            for _ in range(32):
                if not pending:
                    break
                raw, ref = sources.fetch(OSV, {"queries": [request for _, request in pending]})
                answers = json.loads(raw)["results"]
                adv.demand(isinstance(answers, list) and len(answers) == len(pending), "incomplete OSV batch response")
                following = []
                for (key, request), answer in zip(pending, answers):
                    adv.demand(isinstance(answer, dict) and not (set(answer)-{"vulns", "next_page_token"}),
                               "invalid OSV batch result")
                    indices = by_query[key]
                    for index in indices:
                        result["components"][index].setdefault("advisory_results", []).append(ref)
                    for item in answer.get("vulns", []):
                        identifier = adv.advisory_id(item["id"])
                        try:
                            raw_record, detail = sources.fetch(adv.SOURCES["osv"] + identifier)
                            record = json.loads(raw_record)
                            adv.demand(record["id"] == identifier and same_osv_revision(item["modified"], record["modified"])
                                       and not record.get("withdrawn"), "OSV detail differs from matched result")
                            severity = record.get("database_specific", {}).get("severity", "UNKNOWN").upper()
                            severity = {"MODERATE": "MEDIUM"}.get(severity, severity)
                            adv.demand(severity in adv.SEVERITIES, "invalid OSV severity")
                            if severity == "UNKNOWN":
                                for alias in [identifier, *record.get("aliases", [])]:
                                    if adv.CVE.fullmatch(alias):
                                        severity = cna_severity(cna_record(alias, sources)[0])
                                        if severity != "UNKNOWN":
                                            break
                            fixed = ""
                            try:
                                if "package" in request:
                                    fixed = fixed_version(record, request["package"], request["version"])
                                elif any("fixed" in event for affected in record["affected"]
                                         for interval in affected.get("ranges", []) for event in interval["events"]):
                                    raise ValueError("fixed Git revision needs release mapping")
                            except (KeyError, ValueError) as exc:
                                result["errors"].append(f"{identifier} {request}: {exc}")
                            if severity == "UNKNOWN":
                                result["errors"].append(f"{identifier}: missing supplier severity")
                            for index in indices:
                                component = result["components"][index]
                                result["findings"].append(evidence_row(identifier, component, severity, fixed,
                                                                     "osv", detail, record.get("aliases", [])))
                        except ERRORS as exc:
                            result["errors"].append(f"{identifier} {request}: {exc}")
                    if answer.get("next_page_token"):
                        following.append((key, dict(request, page_token=answer["next_page_token"])))
                pending = following
            adv.demand(not pending, "OSV batch pagination limit reached")
        except ERRORS as exc:
            result["errors"].append(f"OSV version checks {begin}..{begin+len(group)-1}: {exc}")
            # A service-wide failure is one failed required source; retain the complete query list.
            break


def node_range(value, expression):
    actual = version_key(value)
    for clause in expression.split("||"):
        ok = True
        tokens = re.sub(r"(<=|>=|<|>)\s+(?=\d)", r"\1", clause).split()
        adv.demand(tokens, "empty Node range")
        for token in tokens:
            match = re.fullmatch(r"(\^|<=|>=|<|>)?(\d+(?:\.(?:\d+|x)){0,2})", token)
            adv.demand(match, f"unrecognized Node range {token!r}")
            op, target = match.groups()
            parts = target.split(".")
            if "x" in parts:
                parts = parts[:parts.index("x")]
            low = version_key(".".join(parts))
            if op == "^":
                n = next((i for i, v in enumerate(low[:3]) if v), 2)
                upper = list(low[:3])
                upper[n] += 1
                upper[n+1:] = [0] * (2-n)
                ok &= low <= actual < version_key(".".join(map(str, upper)))
            elif op:
                ok &= {"<=": actual <= low, "<": actual < low, ">=": actual >= low, ">": actual > low}[op]
            else:
                ok &= actual[:len(parts)] == low[:len(parts)]
        if ok:
            return True
    return False


def check_node(component, result, sources):
    raw, ref = sources.fetch(NODE)
    records = json.loads(raw)
    adv.demand(isinstance(records, dict) and records, "empty Node security index")
    component["advisory_results"] = [ref]
    for key, record in records.items():
        try:
            if not ({"linux", "all"} & set(record["affectedEnvironments"])):
                continue
            if node_range(component["version"], record["vulnerable"]) and not node_range(component["version"], record["patched"]):
                for cve in record["cve"]:
                    adv.demand(adv.CVE.fullmatch(cve), "invalid Node CVE")
                    result["findings"].append(evidence_row(cve, component, record["severity"].upper(), record["patched"],
                                                           "node-security-wg", ref))
        except ERRORS as exc:
            result["errors"].append(f"Node security record {key}: {exc}")


def check_python(component, result, sources):
    # The PSF publishes introduced/fixed CPython commits. Test ancestry of the exact release tag.
    raw, ref = sources.fetch(PSF)
    version = component["version"]
    adv.demand(re.fullmatch(r"\d+\.\d+\.\d+", version), "invalid CPython release")
    with tempfile.TemporaryDirectory(dir=sources.directory.parent.parent) as tmp:
        repo = Path(tmp) / "cpython"
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
        subprocess.run(["git", "-c", "http.followRedirects=false", "clone", "--quiet", "--bare", "--filter=tree:0",
                        "--no-tags", "--single-branch", "--branch", "v" + version, "--",
                        "https://github.com/python/cpython", str(repo)], check=True, env=env, timeout=420)
        history = subprocess.check_output(["git", "-C", str(repo), "rev-list", "HEAD"], env=env,
                                         text=True, timeout=60).splitlines()
    adv.demand(history and all(re.fullmatch("[0-9a-f]{40}", h) for h in history), "missing CPython history")
    component.update(advisory_results=[ref], git_tag="v" + version, release_commit=history[0])
    commits = set(history)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or "/advisories/python/" not in member.name:
                continue
            count += 1
            record = json.load(archive.extractfile(member))
            if record.get("withdrawn"):
                continue
            try:
                affected = False
                for item in record["affected"]:
                    for interval in item["ranges"]:
                        events = interval["events"]
                        adv.demand(interval.get("repo") == "https://github.com/python/cpython" and interval["type"] == "GIT"
                                   and len(events) and set(events[0]) == {"introduced"} and
                                   all(set(event) == {"fixed"} for event in events[1:]), "unsupported PSF range")
                        begin = events[0]["introduced"]
                        affected |= (begin == "0" or begin in commits) and not any(e["fixed"] in commits for e in events[1:])
                if affected:
                    cves = [cve for cve in record["aliases"] if adv.CVE.fullmatch(cve)]
                    adv.demand(cves, "missing PSF CVE")
                    for cve in cves:
                        cna, detail = cna_record(cve, sources)
                        fixed = set()
                        for product in cna.get("affected", []):
                            if product.get("product", "").lower() not in ("cpython", "python"):
                                continue
                            for rule in product["versions"]:
                                if rule["status"] == "affected" and rule.get("lessThan") not in (None, "*"):
                                    if version_key(rule["version"]) <= version_key(version) < version_key(rule["lessThan"]):
                                        fixed.add(rule["lessThan"])
                        if not fixed and any("fixed" in ev for a in record["affected"] for r in a["ranges"] for ev in r["events"]):
                            result["errors"].append(f"{cve} CPython {version}: fixed commit lacks a release mapping")
                        severity = cna_severity(cna)
                        if severity == "UNKNOWN":
                            result["errors"].append(f"{cve}: CPython CNA severity unavailable")
                        result["findings"].append(evidence_row(cve, component, severity, ", ".join(sorted(fixed)), "psf", [ref, detail]))
            except ERRORS as exc:
                result["errors"].append(f"{record.get('id')} CPython {version}: {exc}")
    adv.demand(count, "empty PSF CPython directory")
    component["supplier_records"] = count


def check_suppliers(result, reports):
    sources = Sources(reports)
    for component in result["components"]:
        check = {"CPython": check_python, "Node.js": check_node}.get(component["name"])
        if check and component["kind"] == "interpreter":
            try:
                check(component, result, sources)
            except ERRORS as exc:
                result["errors"].append(f"{component['name']} {component['version']}: {exc}")
    osv_checked(result, sources)
