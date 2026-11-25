"""Validate CISA and structured identifier evidence, then apply the fixed release policy."""

import hashlib
from http.client import HTTPException
import json
import re
from collections import deque
from datetime import datetime, timezone
from urllib.error import HTTPError

CVE = re.compile(r"CVE-[0-9]{4}-[0-9]{4,}")
GHSA = re.compile(r"GHSA-[23456789cfghjmpqrvwx]{4}(?:-[23456789cfghjmpqrvwx]{4}){2}")
ADVISORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,199}")
# VendorIDs can also name multi-vulnerability distribution bulletins. Only independent
# vulnerability identifiers from this field establish equivalence.
BULLETIN = re.compile(r"(?:RHSA|USN|DSA|DLA|ALAS(?:[0-9A-Z]*)?|ELSA|FEDORA|SUSE-SU)[-:].+", re.I)
VENDOR_ALIAS = re.compile(r"(?:PYSEC|RUSTSEC|GO|OSV|MAL)-[0-9]{4}-[0-9]+")
SOURCES = {"osv": "https://api.osv.dev/v1/vulns/", "github": "https://api.github.com/advisories/"}
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


def equivalents(source, identifier, record):
    if source == "osv":
        demand(record["id"] == identifier, "OSV id does not match request")
        values = record.get("aliases", [])
        demand(isinstance(values, list), "invalid OSV aliases")
        return [advisory_id(value) for value in values]
    demand(record["ghsa_id"] == identifier, "GitHub ghsa_id does not match request")
    values = record["identifiers"]
    demand(isinstance(values, list) and values, "invalid GitHub identifiers")
    result = []
    for item in values:
        kind, value = item["type"], advisory_id(item["value"])
        demand(kind in {"GHSA", "CVE"} and value.startswith(kind + "-"), "invalid typed GitHub identifier")
        result.append(value)
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
            if source in self.rate_limits:
                entry.pop("fetched_at")
                entry["error"] = f"{source} lookup {identifier} skipped after rate limit: {self.rate_limits[source]}"
                return entry
            try:
                raw, entry["effective_url"] = self.lookup(source, identifier)
                entry["sha256"] = hashlib.sha256(raw).hexdigest()
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
                        self.rate_limits[source] = f"{identifier}: HTTP {exc.code}, reset/retry {reset}"
        return self.records[key]

    def resolve(self, primary, vendors):
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
        work, visited = deque(), set()

        def enqueue(identifier):
            if CVE.fullmatch(identifier):
                cves.add(identifier)
                return
            for source in (("osv", "github") if GHSA.fullmatch(identifier) else ("osv",)):
                key = (source, identifier)
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
                errors.append(entry["error"])
                continue
            for other in entry["equivalents"]:
                edges.append({"from": identifier, "to": other, "source": source, "sha256": entry["sha256"]})
                if BULLETIN.fullmatch(other):
                    errors.append(f"bulletin in alias record cannot establish identity: {other}")
                else:
                    enqueue(other)
        if work:
            errors.append(f"alias traversal limit with {len(work)} pending lookups")
        state = "incomplete" if errors else ("resolved" if cves else "no_published_cve")
        return {"cves": sorted(cves), "edges": edges, "status": state, "errors": errors}


def evaluate(finding, kev, resolver):
    errors, reasons = [], []
    fields = finding if isinstance(finding, dict) else {}
    primary = fields.get("VulnerabilityID")
    aliases = resolver.resolve(primary, fields.get("VendorIDs", []))
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
