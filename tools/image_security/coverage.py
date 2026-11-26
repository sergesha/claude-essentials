"""Summarize the pinned scanner's inventory and investigate specific findings."""

import subprocess
import tarfile
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

from advisories import string
from image_inventory import export_rootfs, finding_targets, targeted_inventory
from native_evidence import SWI_OLD, swi_browser_finding

PLATFORMS = {"knowledge-atlas": {"linux/arm64"},
             "redis-memory-mcp": {"linux/amd64", "linux/arm64"},
             "redis-memory-embeddings": {"linux/amd64", "linux/arm64"}}
RETAINED_SWI = ("sha256:51fadf91d930e65cbd21da768700c7328c2c104a15b069a1c3fdbbf77c83c971",
                "cbd1bc08beb2ef49cf0ba682d639bfbfe09bc0b9")


def target_summary(result):
    cls, target, kind = (result.get(key) for key in ("Class", "Target", "Type"))
    if cls not in ("os-pkgs", "lang-pkgs") or not string(target) or not string(kind):
        raise ValueError("invalid Trivy result target")
    packages = result.get("Packages")
    if not isinstance(packages, list) or not packages or not all(isinstance(p, dict) for p in packages):
        raise ValueError("missing or invalid Trivy package inventory")
    named, anonymous = [], []
    for package in packages:
        if string(package.get("Name")) and string(package.get("Version")):
            named.append(package)
        else:
            anonymous.append(package)
    ids = {p["ID"] for p in named if string(p.get("ID"))}
    if not named or any(not (cls == "lang-pkgs" and kind == "cargo" and p.get("AnalyzedBy") == "cargo"
            and p.get("Name") in (None, "") and p.get("Version") in (None, "") and string(p.get("ID"))
            and p["ID"] not in ids and p.get("Relationship") == "root"
            and isinstance(p.get("DependsOn"), list) and p["DependsOn"]
            and all(string(dep) and dep in ids for dep in p["DependsOn"])) for p in anonymous):
        raise ValueError("incomplete Trivy package inventory")
    scope = "OS packages" if cls == "os-pkgs" else "source metadata" if kind == "cargo" else "library metadata"
    return {"class": cls, "target": target, "type": kind, "scope": scope,
            "package_count": len(named), "metadata_row_count": len(anonymous),
            "analyzers": dict(Counter(p["AnalyzedBy"] if string(p.get("AnalyzedBy")) else "unspecified"
                                      for p in named))}


def collect_coverage(producer, candidate, report, reports, *, archive_bound=True):
    result = {"errors": [], "findings": [], "applicability": {},
              "limitations": ["Trivy reports recognized OS and library package metadata. Its inventory does not "
                              "establish native linkage or the composition of vendored or statically linked code."]}
    if producer in ("knowledge-atlas", "redis-memory-embeddings"):
        result["limitations"].append("A reported ONNX Runtime package version does not establish its upstream source "
                                     "tag or bundled native dependency composition.")
    if candidate.get("platform") not in PLATFORMS.get(producer, set()):
        result["errors"].append(f"unknown producer or unsupported candidate platform: {producer}")
        return result
    retained_swi = (candidate.get("id"), candidate.get("revision")) == RETAINED_SWI
    if producer == "knowledge-atlas" and retained_swi:
        row = swi_browser_finding(SWI_OLD)
        row["Evidence"].update(candidate_id=candidate["id"], candidate_revision=candidate["revision"],
                               provenance="retained candidate file inspection")
        result["findings"].append(row)
    if not archive_bound:
        return result
    inspect_swi = producer == "knowledge-atlas" and not retained_swi
    python, cargo = finding_targets(report)
    if inspect_swi or python or cargo:
        try:
            with tempfile.TemporaryDirectory(dir=reports.parent) as tmp:
                rootfs = export_rootfs(candidate, Path(tmp) / "rootfs.tar")
                targeted_inventory(rootfs, inspect_swi, python, cargo, result)
        except (AttributeError, KeyError, OSError, TypeError, ValueError, EOFError, subprocess.SubprocessError,
                tarfile.TarError, zipfile.BadZipFile) as exc:
            result["limitations"].append(f"Candidate-specific applicability evidence unavailable: {exc}")
    return result
