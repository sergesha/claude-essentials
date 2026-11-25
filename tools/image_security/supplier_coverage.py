"""Versioned OSV, PSF and Node checks. All requests are to fixed, public origins."""

import hashlib
import io
import json
import math
import os
import re
import subprocess
import tarfile
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request
from urllib.parse import urlencode

import advisories as adv

OSV = "https://api.osv.dev/v1/querybatch"
PSF = "https://codeload.github.com/psf/advisory-database/tar.gz/refs/heads/main"
NODE = "https://raw.githubusercontent.com/nodejs/security-wg/main/vuln/core/index.json"
ERRORS = (KeyError, IndexError, AttributeError, TypeError, ValueError, OSError, EOFError, subprocess.SubprocessError, tarfile.TarError)


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
            if url.startswith("https://api.github.com/repos/"):
                token = os.environ.get("GITHUB_TOKEN")
                if token:
                    adv.demand(token.isascii() and token.isprintable() and not any(c.isspace() for c in token),
                               "invalid GitHub token encoding")
                    request.add_unredirected_header("Authorization", "Bearer " + token)
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
            "DataSource": {"ID": source}, "Evidence": proof,
            "Copy": {k: component[k] for k in ("kind", "name", "version", "origin", "sha256",
                                                "crate_sha256", "input", "implementation") if k in component}}


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


def package_version(value, ecosystem, range_type):
    if range_type == "SEMVER" or ecosystem in ("npm", "crates.io"):
        match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", value)
        adv.demand(match, f"unsupported semantic version {value!r}")
        pre = match[4]
        identifiers = tuple((0, int(v)) if v.isdigit() else (1, v) for v in pre.split(".")) if pre else ()
        return int(match[1]), int(match[2] or 0), int(match[3] or 0), pre is None, identifiers
    if ecosystem == "PyPI":
        match = re.fullmatch(r"(?i)(?:(\d+)!)?(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?", value)
        adv.demand(match, f"unsupported Python version {value!r}")
        release = [int(v) for v in match[2].split(".")]
        while release and release[-1] == 0:
            release.pop()
        phase = {"a": 0, "b": 1, "rc": 2}.get((match[3] or "").lower(), 3)
        if not match[3] and not match[5] and match[6]:
            phase = -1
        return (int(match[1] or 0), tuple(release), phase, int(match[4] or 0),
                int(match[5]) if match[5] else -1, match[6] is None, int(match[6] or 0))
    return version_key(value)


def fixed_version(record, package, installed):
    fixed, matched = set(), False
    unresolved, includes = False, False
    for affected in record["affected"]:
        desc = affected.get("package", {})
        if (desc.get("ecosystem"), desc.get("name", "").lower().replace("_", "-")) != (
                package["ecosystem"], package["name"].lower().replace("_", "-")):
            continue
        matched = True
        listed = affected.get("versions", [])
        adv.demand(isinstance(listed, list) and all(adv.string(v) for v in listed), "invalid OSV versions")
        includes |= installed in listed
        for interval in affected.get("ranges", []):
            if interval["type"] not in ("SEMVER", "ECOSYSTEM"):
                unresolved = True
                continue
            def order(value):
                return package_version(value, package["ecosystem"], interval["type"])
            actual = order(installed)
            start = None
            for event in interval["events"]:
                adv.demand(isinstance(event, dict) and len(event) == 1, "invalid OSV range event")
                kind, bound = next(iter(event.items()))
                if kind == "introduced":
                    adv.demand(start is None, "unterminated OSV interval")
                    start = bound
                    continue
                adv.demand(start is not None and kind in ("fixed", "last_affected", "limit"), "invalid OSV interval")
                if (start == "0" or order(start) <= actual) and (
                        actual <= order(bound) if kind == "last_affected" else actual < order(bound)):
                    includes = True
                    if kind == "fixed":
                        fixed.add(bound)
                start = None
            if start is not None and (start == "0" or order(start) <= actual):
                includes = True
    adv.demand(matched and includes and (fixed or not unresolved),
               "matched version or applicable fix requires verified upstream mapping")
    return ", ".join(sorted(fixed))


def same_osv_revision(batch, detail):
    # querybatch timestamps are microsecond precision; detailed records retain nanoseconds.
    return adv.timestamp(batch) == adv.timestamp(detail)


def osv_severity(record):
    ratings = []
    severity = record.get("database_specific", {}).get("severity")
    if severity:
        ratings.append({"MODERATE": "MEDIUM"}.get(severity.upper(), severity.upper()))
    for entry in record.get("severity", []):
        score = entry["score"]
        if isinstance(score, str) and score.startswith(("CVSS:3.0/", "CVSS:3.1/")):
            fields = dict(metric.split(":") for metric in score.split("/")[1:])
            changed = {"C": True, "U": False}[fields["S"]]
            impact = 1 - math.prod(1 - {"N": 0, "L": .22, "H": .56}[fields[k]] for k in "CIA")
            impact = (7.52*(impact-.029) - 3.25*(impact-.02)**15) if changed else 6.42*impact
            exploit = 8.22 * {"N": .85, "A": .62, "L": .55, "P": .2}[fields["AV"]]
            exploit *= {"L": .77, "H": .44}[fields["AC"]] * {"N": .85, "R": .62}[fields["UI"]]
            exploit *= {"N": .85, "L": .68 if changed else .62, "H": .5 if changed else .27}[fields["PR"]]
            score = 0 if impact <= 0 else math.ceil(round(min(10, (impact+exploit)*(1.08 if changed else 1)), 6)*10)/10
        elif not re.fullmatch(r"(?:10(?:\.0)?|[0-9](?:\.[0-9])?)", str(score)):
            continue
        value = float(score)
        ratings.append("CRITICAL" if value >= 9 else "HIGH" if value >= 7 else "MEDIUM" if value >= 4 else "LOW")
    demand_ratings = all(r in adv.SEVERITIES for r in ratings)
    adv.demand(demand_ratings, "invalid OSV severity")
    return max(ratings or ["UNKNOWN"], key=["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"].index)


def osv_match(result, sources, request, indices, item, ref):
    identifier = adv.advisory_id(item["id"])
    rows = [evidence_row(identifier, result["components"][i], "UNKNOWN", "", "osv", [ref]) for i in indices]
    for row in rows:
        row["Status"] = "unknown"
    result["findings"].extend(rows)
    raw, detail = sources.fetch(adv.SOURCES["osv"] + identifier)
    record = json.loads(raw)
    adv.demand(record["id"] == identifier and same_osv_revision(item["modified"], record["modified"])
               and not record.get("withdrawn"), "OSV detail differs from matched result")
    for row in rows:
        row["Evidence"].append(detail)
    aliases = record.get("aliases", [])
    if not isinstance(aliases, list):
        result["errors"].append(f"{identifier}: malformed OSV aliases")
        aliases = []
    for alias in aliases:
        try:
            alias = adv.advisory_id(alias)
            for row in rows:
                row["VendorIDs"].append(alias)
        except ERRORS as exc:
            result["errors"].append(f"{identifier} alias: {exc}")
    try:
        severity = osv_severity(record)
        if severity == "UNKNOWN":
            for cve in [identifier, *rows[0]["VendorIDs"]]:
                if adv.CVE.fullmatch(cve):
                    cna, cna_ref = cna_record(cve, sources)
                    severity = cna_severity(cna)
                    for row in rows:
                        row["Evidence"].append(cna_ref)
                    if severity != "UNKNOWN":
                        break
        for row in rows:
            row["Severity"] = severity
        adv.demand(severity != "UNKNOWN", "missing supplier severity")
    except ERRORS as exc:
        result["errors"].append(f"{identifier} severity: {exc}")
    if "package" in request:
        fixed = fixed_version(record, request["package"], request["version"])
    else:
        adv.demand(not any("fixed" in event for affected in record["affected"]
                           for interval in affected.get("ranges", []) for event in interval["events"]),
                   "fixed Git revision needs release mapping")
        fixed = ""
    for row in rows:
        row.update(FixedVersion=fixed, Status="fixed" if fixed else "affected")


def osv_checked(result, sources):
    by_query = defaultdict(list)
    tag_cache = {}
    for query in result["queries"]:
        request = {k: v for k, v in query.items() if k != "component"}
        if "repository" in request:
            repo, tag = request["repository"], request["tag"]
            try:
                if (repo, tag) not in tag_cache:
                    adv.demand(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                               and re.fullmatch(r"[A-Za-z0-9_.-]+", tag), "invalid supplier tag")
                    base = "https://api.github.com/repos/" + repo + "/git/"
                    data, ref = sources.fetch(base + "ref/tags/" + tag)
                    entry = json.loads(data)
                    adv.demand(entry["ref"] == "refs/tags/" + tag, "wrong tag reference")
                    obj, refs = entry["object"], [ref]
                    if obj["type"] == "tag":
                        adv.demand(re.fullmatch("[0-9a-f]{40}", obj["sha"]), "invalid tag object")
                        raw, ref = sources.fetch(base + "tags/" + obj["sha"])
                        entry = json.loads(raw)
                        adv.demand(entry["sha"] == obj["sha"] and entry["tag"] == tag, "tag object mismatch")
                        obj = entry["object"]
                        refs.append(ref)
                    adv.demand(obj["type"] == "commit" and re.fullmatch("[0-9a-f]{40}", obj["sha"]),
                               "missing supplier commit")
                    tag_cache[(repo, tag)] = obj["sha"], refs
                revision, refs = tag_cache[(repo, tag)]
                result["components"][query["component"]].update(source_revision=revision, source_records=refs)
                request = {"commit": revision}
            except ERRORS as exc:
                result["errors"].append(f"{repo}@{tag}: {exc}")
                continue
        by_query[json.dumps(request, sort_keys=True)].append(query["component"])
    keys = list(by_query)
    for begin in range(0, len(keys), 200):
        pending = [(k, json.loads(k)) for k in keys[begin:begin+200]]
        for _ in range(32):
            if not pending:
                break
            try:
                raw, ref = sources.fetch(OSV, {"queries": [request for _, request in pending]})
                answers = json.loads(raw)["results"]
                adv.demand(isinstance(answers, list) and len(answers) == len(pending), "incomplete OSV batch")
            except ERRORS as exc:
                result["errors"].append(f"OSV batch {begin}: {exc}")
                break
            following = []
            for (key, request), answer in zip(pending, answers):
                try:
                    adv.demand(isinstance(answer, dict) and not (set(answer)-{"vulns", "next_page_token"}),
                               "invalid OSV batch result")
                    indices = by_query[key]
                    for i in indices:
                        result["components"][i].setdefault("advisory_results", []).append(ref)
                    items = answer.get("vulns", [])
                    adv.demand(isinstance(items, list), "invalid OSV vulns array")
                    for item in items:
                        try:
                            osv_match(result, sources, request, indices, item, ref)
                        except ERRORS as exc:
                            result["errors"].append(f"{request} {item}: {exc}")
                    if "next_page_token" in answer:
                        token = answer["next_page_token"]
                        adv.demand(adv.string(token), "invalid page token")
                        following.append((key, dict(request, page_token=token)))
                except ERRORS as exc:
                    result["errors"].append(f"{request}: {exc}")
            pending = following
        else:
            if pending:
                result["errors"].append("OSV pagination limit reached")


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
                n = next((i for i, v in enumerate(low[:len(parts)]) if v), len(parts)-1)
                upper = list(low[:3])
                upper[n] += 1
                upper[n+1:] = [0] * (2-n)
                ok &= low <= actual < version_key(".".join(map(str, upper)))
            elif len(parts) < 3 and op in ("<=", ">"):
                following = parts[:-1] + [str(int(parts[-1]) + 1)]
                boundary = version_key(".".join(following))
                ok &= actual < boundary if op == "<=" else actual >= boundary
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
            platforms = record["affectedEnvironments"]
            adv.demand(isinstance(platforms, list) and all(adv.string(v) for v in platforms),
                       "invalid Node environments")
            if not ({"linux", "all"} & set(platforms)) or not node_range(component["version"], record["vulnerable"]):
                continue
            patch = record.get("patched")
            fixed = ""
            try:
                adv.demand(isinstance(patch, str), "invalid Node patched range")
                if patch and node_range(component["version"], patch):
                    continue
                fixed = patch
            except ERRORS as exc:
                result["errors"].append(f"Node security record {key} fix: {exc}")
            rating = str(record.get("severity", "")).upper()
            if rating not in adv.SEVERITIES - {"UNKNOWN"}:
                result["errors"].append(f"Node security record {key}: invalid severity")
                rating = "UNKNOWN"
            ids = record.get("cve")
            if not isinstance(ids, list):
                result["errors"].append(f"Node security record {key}: invalid CVE list")
                ids = []
            valid = [c for c in ids if isinstance(c, str) and adv.CVE.fullmatch(c)]
            if len(valid) != len(ids):
                result["errors"].append(f"Node security record {key}: invalid CVE")
            if not valid:
                row = evidence_row("NODE-SECURITY-WG-" + key, component, rating, fixed, "node-security-wg", ref)
                if record.get("cve") == []:
                    row["SupplierIdentity"] = {"record": key, "cves": []}
                result["findings"].append(row)
            for cve in valid:
                result["findings"].append(evidence_row(cve, component, rating, fixed, "node-security-wg", ref))
        except ERRORS as exc:
            result["errors"].append(f"Node security record {key}: {exc}")


def check_node_releases(component, result, sources):
    raw, ref = sources.fetch("https://nodejs.org/dist/index.json")
    records = json.loads(raw)
    version = component["version"]
    adv.demand(isinstance(records, list) and any(r["version"] == "v" + version for r in records),
               "Node release absent from official release index")
    component.setdefault("advisory_results", []).append(ref)
    for release in records:
        other = release["version"].removeprefix("v")
        if other.split(".")[0] != version.split(".")[0] or version_key(other) < version_key(version):
            continue
        adv.demand(type(release["security"]) is bool, "missing Node release security flag")
        if not release["security"]:
            continue
        try:
            month = datetime.strptime(release["date"], "%Y-%m-%d").strftime("%B-%Y").lower()
            url = ("https://raw.githubusercontent.com/nodejs/nodejs.org/main/apps/site/pages/en/blog/vulnerability/"
                   + month + "-security-releases.md")
            data, bulletin = sources.fetch(url)
            page = data.decode()
            adv.demand(f"/blog/release/v{other}/" in page, f"no bulletin for Node {other}")
            component["advisory_results"].append(bulletin)
            if other == version:
                continue
            sections = [section for section in re.split(r"^#{2,3} ", page, flags=re.M)[1:]
                        if "CVE-" in section.splitlines()[0]]
            adv.demand(sections, f"missing Node {other} advisory sections")
            for section in sections:
                heading = section.splitlines()[0]
                ids = adv.CVE.findall(heading)
                rating = re.search(r"\((critical|high|medium|low)\)\s*$", heading, re.I)
                impact = re.search(r"(?:active release lines:|This vulnerability affects Node\.js)\s*([0-9.,x* and]+)",
                                   section, re.I)
                if not impact or not rating:
                    result["errors"].append(f"{bulletin}: unrecognized scope or severity for {ids}")
                if impact and version.split(".")[0] not in re.findall(r"(\d+)\.x", impact[1]):
                    continue
                for identifier in ids:
                    result["findings"].append(evidence_row(identifier, component, rating[1].upper() if rating else
                                                           "UNKNOWN", other, "node-release-bulletin", [ref, bulletin]))
        except ERRORS as exc:
            result["errors"].append(f"Node release {other}: {exc}")


def python_release_status(cna, version):
    actual = version_key(version)
    matches, defaults = [], []
    for product in cna.get("affected", []):
        if product.get("product", "").lower() not in ("cpython", "python"):
            continue
        defaults.append(product.get("defaultStatus"))
        for rule in product["versions"]:
            adv.demand(not rule.get("changes"), "unsupported CPython status changes")
            start = version_key(rule["version"])
            upper = rule.get("lessThan") or rule.get("lessThanOrEqual")
            if upper == "*":
                in_range = actual >= start
            elif upper:
                end = version_key(upper)
                in_range = start <= actual and (actual < end if "lessThan" in rule else actual <= end)
            else:
                in_range = actual == start
            if in_range:
                fix = rule.get("lessThan", "")
                same_branch = fix and fix != "*" and version_key(fix)[:2] == actual[:2]
                matches.append((rule["status"], fix if same_branch else ""))
    if matches:
        statuses = {s for s, _ in matches}
        adv.demand(len(statuses) == 1, "conflicting CPython release rules")
        return statuses.pop(), ", ".join(sorted({v for _, v in matches if v}))
    return ("unaffected" if defaults and set(defaults) == {"unaffected"} else "unknown"), ""


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
            record = {}
            try:
                record = json.load(archive.extractfile(member))
                adv.demand(isinstance(record, dict), "invalid PSF record")
                if record.get("withdrawn"):
                    continue
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
                    aliases = record["aliases"]
                    adv.demand(isinstance(aliases, list), "invalid PSF aliases")
                    cves = []
                    for alias in aliases:
                        try:
                            identifier = adv.advisory_id(alias)
                            if adv.CVE.fullmatch(identifier):
                                cves.append(identifier)
                        except ERRORS as exc:
                            result["errors"].append(f"{member.name}: {exc}")
                    adv.demand(cves, "missing PSF CVE")
                    for cve in cves:
                        row = evidence_row(cve, component, "UNKNOWN", "", "psf", [ref])
                        result["findings"].append(row)
                        try:
                            cna, detail = cna_record(cve, sources)
                            row["Evidence"].append(detail)
                            row["Severity"] = cna_severity(cna)
                            status, fixed = python_release_status(cna, version)
                            row.update(FixedVersion=fixed, Status="fixed" if fixed else "affected")
                            if status == "unaffected":
                                row["ImplementationDisposition"] = {"status": "unaffected", "source": detail,
                                                                     "release": version, "copy": row["Copy"]}
                            elif row["Severity"] == "UNKNOWN" or (not fixed and any(
                                    "fixed" in ev for a in record["affected"] for r in a["ranges"] for ev in r["events"])):
                                result["errors"].append(f"{cve} CPython {version}: missing branch fix or severity")
                        except ERRORS as exc:
                            result["errors"].append(f"{cve} CPython {version}: {exc}")
            except ERRORS as exc:
                result["errors"].append(f"{member.name} CPython {version}: {exc}")
    adv.demand(count, "empty PSF CPython directory")
    component["supplier_records"] = count


NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0"


def check_native_cpe(component, result, sources):
    """Check the vendor/product/version directly, retaining conditional matches."""
    product = component["cpe"]
    url = NVD + "?" + urlencode({"virtualMatchString": "cpe:2.3:a:" + product})
    raw, ref = sources.fetch(url)
    body = json.loads(raw)
    vulns = body["vulnerabilities"]
    adv.demand(isinstance(vulns, list) and body["startIndex"] == 0 and body["totalResults"] == len(vulns),
               "incomplete NVD result")
    component.setdefault("advisory_results", []).append(ref)
    for match in vulns:
        try:
            c = match["cve"]
            identifier = adv.advisory_id(c["id"])
            adv.demand(adv.CVE.fullmatch(identifier), "invalid NVD record ID")
            row = evidence_row(identifier, component, "UNKNOWN", "", "NVD", ref)
            result["findings"].append(row)
            ratings = [m["cvssData"]["baseSeverity"].upper() for group in c["metrics"].values()
                       for m in group if "baseSeverity" in m["cvssData"]]
            if ratings:
                adv.demand(all(v in adv.SEVERITIES for v in ratings), "unrecognized NVD severity")
                row["Severity"] = max(ratings, key=["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"].index)
            if identifier == "CVE-2021-43618" and component.get("patched_gmp"):
                row["ImplementationDisposition"] = dict(status="unaffected", source=component["patched_gmp"], copy=row["Copy"])
            else:
                result["errors"].append(f"{identifier} {product}: qualify NVD configuration, implementation and available fix")
        except ERRORS as exc:
            result["errors"].append(f"NVD {product}: {exc}")


def check_snapshot_advisories(component, result, sources):
    repo = component["advisory_repository"]
    adv.demand(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo), "invalid supplier repository")
    url = "https://api.github.com/repos/" + repo + "/security-advisories?state=published&per_page=100"
    for page in range(1, 11):
        raw, ref = sources.fetch(url + "&page=" + str(page))
        records = json.loads(raw)
        adv.demand(isinstance(records, list), "invalid published supplier advisory index")
        component.setdefault("advisory_results", []).append(ref)
        for record in records:
            try:
                ghsa = adv.advisory_id(record["ghsa_id"])
                adv.demand(adv.GHSA.fullmatch(ghsa), "invalid project advisory")
                row = evidence_row(ghsa, component, "UNKNOWN", "", "supplier-published-advisory", ref)
                result["findings"].append(row)
                cve = record.get("cve_id")
                if cve is not None:
                    adv.demand(isinstance(cve, str) and adv.CVE.fullmatch(cve), "invalid project CVE")
                    row["VendorIDs"] = [cve]
                severity = str(record.get("severity", "")).upper()
                row["Severity"] = {"MODERATE": "MEDIUM"}.get(severity, severity)
                result["errors"].append(f"{ghsa} {component['origin']}: map published ranges to bundled {repo} snapshot")
            except ERRORS as exc:
                result["errors"].append(f"{repo}: {exc}")
        if len(records) < 100:
            return
    raise ValueError(f"{repo}: supplier pagination limit reached")


def check_suppliers(result, reports):
    sources = Sources(reports)
    for component in result["components"]:
        if component.get("advisory_repository"):
            try:
                check_snapshot_advisories(component, result, sources)
            except ERRORS as exc:
                result["errors"].append(f"{component['advisory_repository']}: {exc}")
        if component.get("cpe"):
            try:
                check_native_cpe(component, result, sources)
            except ERRORS as exc:
                result["errors"].append(f"NVD {component['cpe']}: {exc}")
        check = {"CPython": check_python, "Node.js": check_node}.get(component["name"])
        if check and component["kind"] == "interpreter":
            try:
                check(component, result, sources)
                if check is check_node:
                    check_node_releases(component, result, sources)
            except ERRORS as exc:
                result["errors"].append(f"{component['name']} {component['version']}: {exc}")
    osv_checked(result, sources)
