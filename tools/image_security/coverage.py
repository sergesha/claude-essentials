"""Bind a stopped-image inventory and supplemental sources to one verified Docker candidate."""

import subprocess
import tempfile
import tarfile
import zipfile
from pathlib import Path

from image_inventory import export_rootfs, inventory
from supplier_coverage import check_suppliers
from ubuntu_coverage import ubuntu_record, ubuntu_snapshot

PLATFORMS = {"knowledge-atlas": "linux/arm64", "redis-memory-mcp": "linux/amd64",
             "redis-memory-embeddings": "linux/amd64"}


def collect_coverage(producer, candidate, archive, report, reports):
    result = {"complete": False, "errors": [], "components": [], "findings": [],
              "platform": candidate.get("platform"), "diff_ids": candidate.get("diff_ids")}
    if producer not in PLATFORMS or candidate["platform"] != PLATFORMS[producer]:
        result["errors"].append(f"unknown producer or unsupported candidate platform: {producer}")
        return result
    try:
        with tempfile.TemporaryDirectory(dir=reports.parent) as tmp:
            rootfs = export_rootfs(candidate, Path(tmp) / "rootfs.tar")
            inventory(producer, rootfs, report, result)
    except (KeyError, ValueError, TypeError, OSError, EOFError, subprocess.SubprocessError,
            tarfile.TarError, zipfile.BadZipFile) as exc:
        result["errors"].append(f"candidate filesystem inventory: {exc}")
    if result.get("build_revision") != candidate.get("revision") and producer == "knowledge-atlas":
        result["errors"].append("runtime build manifest revision differs from the image revision")
    if producer == "knowledge-atlas" and result.get("ubuntu_sources"):
        try:
            ubuntu_snapshot("noble", result["ubuntu_sources"], reports, result)
        except (KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
            result["errors"].append(f"Ubuntu vendor status for {', '.join(result['ubuntu_sources'])}: {exc}")
    if "queries" in result:
        check_suppliers(result, reports)
    result["complete"] = bool(result["components"]) and not result["errors"]
    return result
