"""Validate CISA and structured identifier evidence, then apply the fixed release policy."""

import hashlib
from http.client import HTTPException
import json
import re
from collections import deque
from datetime import datetime, timezone
from urllib.error import HTTPError
from xml.etree import ElementTree

CVE = re.compile(r"CVE-[0-9]{4}-[0-9]{4,}")
GHSA = re.compile(r"GHSA-[23456789cfghjmpqrvwx]{4}(?:-[23456789cfghjmpqrvwx]{4}){2}")
ADVISORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,199}")
# VendorIDs can also name multi-vulnerability distribution bulletins. Only independent
# vulnerability identifiers from this field establish equivalence.
BULLETIN = re.compile(r"(?:RHSA|USN|DSA|DLA|ALAS(?:[0-9A-Z]*)?|ELSA|FEDORA|SUSE-SU)[-:].+", re.I)
VENDOR_ALIAS = re.compile(r"(?:PYSEC|RUSTSEC|GO|OSV|MAL)-[0-9]{4}-[0-9]+")
TEMP = re.compile(r"TEMP-[0-9]{7}-[A-F0-9]{6}")
REPO = r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
REPOSITORY_ADVISORY = re.compile(REPO + "/security-advisories/" + GHSA.pattern)
WEB_ADVISORY = re.compile(r"https://github\.com/(?P<repo>" + REPO + ")/security/advisories/(?P<ghsa>" + GHSA.pattern + ")")
STRUCTURED_ID = re.compile(r"^(?:[A-Z][A-Z0-9]+-[A-Za-z0-9-]+\b|(?i:(?:alias(?:es)?|identifiers?|references?)\s*:))")
DEBIAN_SOURCE = "https://salsa.debian.org/security-tracker-team/security-tracker"
SOURCES = {"debian": "https://security-tracker.debian.org/tracker/", "github-repository": "https://api.github.com/repos/",
           "osv": "https://api.osv.dev/v1/vulns/", "github": "https://api.github.com/advisories/"}
SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
STATUSES = {"unknown", "affected", "fixed", "will_not_fix", "fix_deferred", "end_of_life",
            "under_investigation"}


def demand(condition, message):
    if not condition:
        raise ValueError(message)


def string(value):
    return isinstance(value, str) and bool(value.strip())


def timestamp(value):
    demand(string(value), "missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    demand(parsed.utcoffset() is not None, "timestamp lacks offset")
    return parsed.astimezone(timezone.utc)


def catalog_cves(catalog, at):
    rows = catalog["vulnerabilities"]
    demand(string(catalog["catalogVersion"]) and timestamp(catalog["dateReleased"]) <= at,
           "invalid KEV version or release date")
    demand(isinstance(rows, list) and type(catalog["count"]) is int and
           len(rows) == catalog["count"] > 0, "invalid KEV count")
    ids = {row["cveID"] for row in rows}
    demand(len(ids) == len(rows) and all(CVE.fullmatch(i) for i in ids), "invalid or duplicate KEV CVE")
    return ids


def advisory_id(value):
    demand(string(value) and ADVISORY.fullmatch(value), f"invalid advisory identifier: {value!r}")
    if value.upper().startswith("CVE-"):
        value = value.upper()
        demand(CVE.fullmatch(value), f"invalid CVE: {value}")
    if value.upper().startswith("GHSA-"):
        value = "GHSA-" + value[5:].lower()
        demand(GHSA.fullmatch(value), f"invalid GHSA: {value}")
    return value


def lookup_url(source, identifier):
    if source == "osv":
        identifier = advisory_id(identifier)
    elif source == "debian":
        demand(TEMP.fullmatch(identifier), "invalid Debian temporary name")
    elif source == "github-repository":
        demand(REPOSITORY_ADVISORY.fullmatch(identifier), "invalid GitHub repository advisory lookup")
    else:
        demand(source == "github" and GHSA.fullmatch(identifier), "invalid GitHub advisory lookup")
    return SOURCES[source] + identifier


def repository_web_url(path):
    return "https://github.com/" + path.replace("/security-advisories/", "/security/advisories/")


def debian_issue(raw, identifier):
    """Read the identity table and bare advisory links from this generated tracker issue."""
    try:
        page = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("malformed Debian tracker issue") from exc
    demand(page.findtext("head/title") == page.findtext("body/header/h1") == identifier,
           "Debian tracker heading does not match requested name")
    fields = {}
    for row in page.findall("body/table[1]/tr"):
        cells = row.findall("td")
        label = cells[0].findtext("b") if len(cells) == 2 else None
        demand(label in {"Name", "Description", "Source", "Debian Bugs"} and label not in fields,
               "unrecognized Debian issue identity field")
        demand(label == "Debian Bugs" or len(cells[1]) == 0, "unrecognized Debian identity field reference")
        fields[label] = "".join(cells[1].itertext())
    demand(fields.get("Name") == identifier and string(fields.get("Description")) and
           " ".join(fields.get("Source", "").split()) ==
           "Automatically generated temporary name. Not for external reference.", "unverified Debian temporary name")
    description = fields["Description"]
    leading = re.match(r"(" + GHSA.pattern + r"): ", description)
    demand(leading or not STRUCTURED_ID.match(description), "unrecognized Debian description identifier")
    body = list(page.find("body"))
    demand(all(el.text in {"Vulnerable and fixed packages", "Notes"} for el in body if el.tag == "h2"),
           "unrecognized Debian issue section")
    headings = [i for i, el in enumerate(body) if el.tag == "h2" and el.text == "Notes"]
    demand(len(headings) == 1 and headings[0] + 1 < len(body) and body[headings[0] + 1].tag == "pre",
           "missing Debian issue notes")
    notes = body[headings[0] + 1]
    lines = []
    line, links = notes.text or "", []
    for child in notes:
        if child.tag == "br":
            lines.append((line.strip(), links))
            line, links = "", []
        else:
            line += "".join(child.itertext())
            links.extend((a.get("href", ""), "".join(a.itertext())) for a in child.iter("a"))
        line += child.tail or ""
    lines.append((line.strip(), links))
    upstream = []
    for line, links in lines:
        demand(not STRUCTURED_ID.match(line), "unrecognized Debian notes identifier")
        for link, label in links:
            match = WEB_ADVISORY.fullmatch(link)
            if match and line == label == link and len(links) == 1 and leading and match["ghsa"] == leading[1]:
                upstream.append(match["repo"] + "/security-advisories/" + match["ghsa"])
            else:
                demand(re.fullmatch(r"https://github\.com/" + REPO + r"/commit/[0-9a-f]{40}", link) or
                       re.fullmatch(r"https://lists\.gnu\.org/archive/html/[^/]+/[0-9-]+/msg[0-9]+\.html", link),
                       "unrecognized or ambiguous Debian issue reference")
    demand(len(upstream) == (1 if leading else 0), "Debian description lacks a matching unique advisory link")
    return {"name": identifier, "description": description, "repository_advisory": upstream[0] if upstream else None}


def equivalents(source, identifier, record):
    if source == "osv":
        demand(record["id"] == identifier, "OSV id does not match request")
        values = record.get("aliases", [])
        demand(isinstance(values, list), "invalid OSV aliases")
        return [advisory_id(value) for value in values]
    if source == "github-repository":
        path = identifier
        identifier = path.rsplit("/", 1)[1]
        demand(record["state"] == "published" and record["url"] == lookup_url(source, path) and
               record["html_url"] == repository_web_url(path),
               "wrong or unpublished repository advisory")
    demand(record["ghsa_id"] == identifier, "GitHub ghsa_id does not match request")
    values = record["identifiers"]
    demand(isinstance(values, list) and values, "invalid GitHub identifiers")
    result = []
    for item in values:
        kind, value = item["type"], advisory_id(item["value"])
        demand(kind in {"GHSA", "CVE"} and value.startswith(kind + "-"), "invalid typed GitHub identifier")
        result.append(value)
    if source == "github-repository":
        demand({v for v in result if GHSA.fullmatch(v)} == {identifier}, "missing or conflicting typed repository GHSA")
    cve = record["cve_id"]
    if cve is not None:
        demand(isinstance(cve, str) and CVE.fullmatch(cve), "invalid GitHub cve_id")
        result.append(cve)
    return result


class Aliases:
    """Fetch each source/ID once per scan. Bound work per finding and retain lookup receipts."""

    def __init__(self, lookup, limit=128):
        self.lookup = lookup
        self.limit = limit
        self.records = {}
        self.rate_limits = {}

    def record(self, source, identifier):
        key = (source, identifier)
        if key not in self.records:
            entry = {"source": source, "id": identifier, "url": SOURCES[source] + identifier,
                     "fetched_at": datetime.now(timezone.utc).isoformat()}
            self.records[key] = entry
            rate_source = "github" if source == "github-repository" else source
            if rate_source in self.rate_limits:
                entry.pop("fetched_at")
                entry["error"] = f"{source} lookup {identifier} skipped after rate limit: {self.rate_limits[rate_source]}"
                return entry
            try:
                raw, entry["effective_url"] = self.lookup(source, identifier)
                entry["sha256"] = hashlib.sha256(raw).hexdigest()
                if source == "debian":
                    entry["record"] = debian_issue(raw, identifier)
                else:
                    entry["record"] = json.loads(raw)
                    entry["equivalents"] = equivalents(source, identifier, entry["record"])
            except (HTTPException, OSError, ValueError, TypeError, KeyError) as exc:
                entry["error"] = f"{source} lookup {identifier}: {exc}"
                if isinstance(exc, HTTPError):
                    headers = exc.headers or {}
                    exc.close()
                    entry["http_status"] = exc.code
                    if exc.code == 429 or (exc.code == 403 and (headers.get("X-RateLimit-Remaining") == "0"
                                                            or "rate limit" in str(exc).lower())):
                        reset = headers.get("X-RateLimit-Reset") or headers.get("Retry-After")
                        self.rate_limits[rate_source] = f"{identifier}: HTTP {exc.code}, reset/retry {reset}"
        return self.records[key]

    def resolve(self, primary, vendors, finding=None):
        errors, edges, cves = [], [], set()
        roots = []
        def add_root(value, field):
            try:
                value = advisory_id(value)
                if field == "VendorIDs" and not (CVE.fullmatch(value) or GHSA.fullmatch(value)
                                                or VENDOR_ALIAS.fullmatch(value)):
                    if not BULLETIN.fullmatch(value):
                        errors.append(f"unclassified vendor identifier: {value}")
                    return
                if BULLETIN.fullmatch(value):
                    errors.append(f"bulletin cannot establish vulnerability identity: {value}")
                    return
                roots.append(value)
                edges.append({"from": field, "to": value, "source": "finding"})
            except ValueError as exc:
                errors.append(str(exc))
        add_root(primary, "VulnerabilityID")
        if isinstance(vendors, list):
            for vendor in vendors:
                add_root(vendor, "VendorIDs")
        else:
            errors.append("invalid VendorIDs")
        repository_path = repository_ghsa = None
        if isinstance(primary, str) and TEMP.fullmatch(primary):
            roots.remove(primary)
            try:
                source = finding.get("DataSource") if finding else None
                issue_url = lookup_url("debian", primary)
                demand(isinstance(source, dict) and source.get("ID") == "debian" and
                       source.get("URL") == DEBIAN_SOURCE and source.get("Name") == "Debian Security Tracker" and
                       finding.get("PrimaryURL") == issue_url,
                       "unverified Trivy Debian temporary name source")
                entry = self.record("debian", primary)
                if "error" in entry:
                    raise ValueError(entry["error"])
                issue = entry["record"]
                demand(finding.get("Title") == "[" + issue["description"] + "]", "Debian issue title mismatch")
                repository_path = issue["repository_advisory"]
                allowed_refs = {issue_url}
                if repository_path:
                    repository_ghsa = repository_path.rsplit("/", 1)[1]
                    roots.append(repository_ghsa)
                    edges.append({"from": primary, "to": repository_ghsa, "source": "debian", "sha256": entry["sha256"]})
                    allowed_refs.add(repository_web_url(repository_path))
                refs = finding.get("References", [])
                demand(isinstance(refs, list) and all(isinstance(ref, str) and ref in allowed_refs for ref in refs),
                       "unclassified Trivy Debian issue reference")
            except ValueError as exc:
                errors.append(str(exc))
        work, visited = deque(), set()
        repository_confirmed = False
        absences = []

        def enqueue(identifier):
            if CVE.fullmatch(identifier):
                cves.add(identifier)
                return
            keys = [("osv", identifier)]
            if identifier == repository_ghsa:
                # Check the verified repository endpoint before interpreting the secondary OSV response.
                keys.insert(0, ("github-repository", repository_path))
            elif GHSA.fullmatch(identifier):
                keys.append(("github", identifier))
            for key in keys:
                if key not in visited:
                    visited.add(key)
                    work.append(key)

        for root in roots:
            enqueue(root)
        steps = 0
        while work and steps < self.limit:
            source, identifier = work.popleft()
            steps += 1
            entry = self.record(source, identifier)
            if "error" in entry:
                if (source == "osv" and identifier == repository_ghsa and repository_confirmed
                        and entry.get("http_status") == 404):
                    absences.append({"url": entry["url"], "id": identifier, "http_status": 404,
                                     "confirmed_by": lookup_url("github-repository", repository_path)})
                else:
                    errors.append(entry["error"])
                continue
            if source == "github-repository":
                identifier = identifier.rsplit("/", 1)[1]
                repository_confirmed = True
            for other in entry["equivalents"]:
                edges.append({"from": identifier, "to": other, "source": source, "sha256": entry["sha256"]})
                if BULLETIN.fullmatch(other):
                    errors.append(f"bulletin in alias record cannot establish identity: {other}")
                else:
                    enqueue(other)
        if work:
            errors.append(f"alias traversal limit with {len(work)} pending lookups")
        state = "incomplete" if errors else ("resolved" if cves else "no_published_cve")
        result = {"cves": sorted(cves), "edges": edges, "status": state, "errors": errors}
        if absences:
            result["absent_records"] = absences
        return result


def evaluate(finding, kev, resolver, supplier_identity=False):
    errors, reasons = [], []
    fields = finding if isinstance(finding, dict) else {}
    primary = fields.get("VulnerabilityID")
    local = fields.get("SupplierIdentity", {})
    if (supplier_identity and isinstance(local, dict) and local.get("cves") == [] and
            fields.get("DataSource", {}).get("ID") == "node-security-wg" and
            str(local.get("record", "")).isdigit() and primary == "NODE-SECURITY-WG-" + local["record"]):
        aliases = {"cves": [], "edges": [], "status": "no_published_cve", "errors": []}
    else:
        aliases = resolver.resolve(primary, fields.get("VendorIDs", []), None if supplier_identity else fields)
    errors.extend(aliases["errors"])
    matches = sorted(set(aliases["cves"]) & kev)
    severity = fields.get("Severity")
    fixed = fields.get("FixedVersion", "")
    status = fields.get("Status", "unknown")
    if not isinstance(severity, str) or severity not in SEVERITIES:
        errors.append("invalid severity")
    if not isinstance(fixed, str) or (fixed and not fixed.strip()):
        errors.append("invalid FixedVersion")
    if not isinstance(status, str) or status not in STATUSES or (status != "unknown" and (status == "fixed") != bool(fixed)):
        errors.append("contradictory or invalid vulnerability status")
    if not all(string(fields.get(key)) for key in ("PkgName", "InstalledVersion")):
        errors.append("missing installed package identity")
    if not isinstance(fields.get("DataSource"), dict) or not string(fields["DataSource"].get("ID")):
        errors.append("missing advisory data source")
    if severity == "CRITICAL":
        reasons.append("CRITICAL")
    if severity == "HIGH" and string(fixed):
        reasons.append("HIGH with fix")
    if matches:
        reasons.append("CISA KEV")
    return {"finding": finding, "aliases": aliases, "matched_kev": matches, "reasons": reasons, "errors": errors}
