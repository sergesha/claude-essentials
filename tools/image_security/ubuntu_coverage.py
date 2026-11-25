"""Reconcile current Ubuntu tracker records against installed source versions."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from advisories import CVE, demand

UCT = "https://git.launchpad.net/ubuntu-cve-tracker"
DEB = re.compile(r"(?:(\d+):)?([0-9][0-9A-Za-z.+:~\-]*)")
LIMIT = 32 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def debian_parts(version):
    match = DEB.fullmatch(version)
    demand(match, f"invalid Debian version {version!r}")
    upstream, dash, revision = match[2].rpartition("-")
    return int(match[1] or 0), upstream if dash else revision, revision if dash else "0"


def compare_debian(left, right):
    """Debian Policy 5.6.12. Compare epoch and alternating text and decimal runs."""
    def order(char):
        if char == "~":
            return -1
        if not char or char.isdigit():
            return 0
        return ord(char) if char.isalpha() else 256 + ord(char)

    def compare(a, b):
        i = j = 0
        while i < len(a) or j < len(b):
            while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
                diff = order(a[i:i+1]) - order(b[j:j+1])
                if diff:
                    return diff
                i += 1
                j += 1
            ai, bj = i, j
            while i < len(a) and a[i].isdigit():
                i += 1
            while j < len(b) and b[j].isdigit():
                j += 1
            an, bn = a[ai:i].lstrip("0"), b[bj:j].lstrip("0")
            if an != bn:
                return len(an) - len(bn) or (1 if an > bn else -1)
        return 0

    e, u, r = debian_parts(left)
    other, v, s = debian_parts(right)
    return e - other or compare(u, v) or compare(r, s)


def ubuntu_record(raw, cve, release, sources, findings, proofs, errors):
    fields = {}
    for line in raw.decode().splitlines():
        if line[:1].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key in fields and key.startswith(release + "_"):
            raise ValueError(f"{cve}: duplicate {key}")
        fields[key] = value.strip()
    demand(fields.get("Candidate") == cve, f"Ubuntu record identity mismatch: {cve}")
    for key, value in fields.items():
        package = key.removeprefix(release + "_")
        if package == key or package not in sources:
            continue
        priority = fields.get("Priority_" + package, fields.get("Priority"))
        severity = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM",
                    "low": "LOW", "negligible": "LOW", "untriaged": "UNKNOWN"}.get(priority)
        status, _, note = value.partition(" ")
        note = note.strip()
        fixed = note[1:-1] if status == "released" and note.startswith("(") and note.endswith(")") else ""
        fix_status = {"needed": "affected", "in-progress": "affected", "deferred": "fix_deferred",
                      "pending": "affected", "ignored": "will_not_fix", "released": "fixed"}.get(status)
        for binary in sources[package]:
            version = binary["source_version"]
            proof = dict(cve=cve, release=release, source_package=package, **binary, vendor_status=value,
                         priority=priority, sha256=digest(raw), applicability="unresolved")
            proofs.append(proof)
            if status == "not-affected" and (not note or not re.search(r"\d", note)):
                proof["applicability"] = "vendor not affected"
                continue
            if status == "released" and DEB.fullmatch(fixed) and compare_debian(version, fixed) >= 0:
                proof["applicability"] = "fixed in installed version"
                continue
            if (not severity or not fix_status or (status == "released" and not DEB.fullmatch(fixed))
                    or (status == "ignored" and not re.search(
                        r"will not fix|won't fix|was (?:needed|deferred)|low priority|too intrusive",
                        note, re.IGNORECASE))):
                errors.append(f"{cve}: {release} {package} {version}, unresolved {value} ({priority})")
                continue
            proof["applicability"] = "affected"
            findings.append({"VulnerabilityID": cve, "PkgName": package, "InstalledVersion": version,
                             "FixedVersion": fixed, "Severity": severity, "Status": fix_status,
                             "DataSource": {"ID": "ubuntu-cve-tracker"}, "VendorEvidence": proof})


def ubuntu_snapshot(release, sources, output, result):
    """Read a fresh complete tracker checkout from the official current branch."""
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        checkout = Path(tmp) / "tracker"
        subprocess.run(["git", "-c", "http.followRedirects=false", "-c", "core.hooksPath=/dev/null",
                        "clone", "--quiet", "--depth=1", "--single-branch", "--no-tags", "--branch", "master",
                        "--", UCT, str(checkout)], check=True, timeout=600, env=env)
        rev = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD", "HEAD^{tree}"],
                                      text=True, env=env, timeout=30).splitlines()
        proofs = []
        receipt = {"url": UCT, "branch": "master", "commit": rev[0], "tree": rev[1],
                   "fetched_at": datetime.now(timezone.utc).isoformat(), "release": release,
                   "installed_sources": sorted(sources), "records_read": {}, "matches": proofs}
        try:
            prefix = (release + "_").encode()
            for area in ("active", "retired", "ignored"):
                folder = checkout / area
                demand(folder.is_dir(), f"Ubuntu tracker missing {area}")
                count = 0
                for path in sorted(folder.glob("CVE-*")):
                    if not CVE.fullmatch(path.name):
                        continue
                    demand(path.is_file() and not path.is_symlink() and path.stat().st_size <= LIMIT,
                           f"invalid Ubuntu tracker record {path.name}")
                    raw = path.read_bytes()
                    count += 1
                    if prefix in raw:
                        try:
                            ubuntu_record(raw, path.name, release, sources, result["findings"], proofs, result["errors"])
                        except (UnicodeError, ValueError) as exc:
                            result["errors"].append(f"{area}/{path.name}: {exc}")
                receipt["records_read"][area] = count
                if area != "ignored":
                    demand(count, f"empty Ubuntu tracker {area}")
            demand(proofs, "no matching Ubuntu status data for installed source packages")
        finally:
            (output / "ubuntu-tracker.json").write_text(json.dumps(receipt, separators=(",", ":")))
