#!/usr/bin/env python3
"""Exercise an installed Redis Memory plugin through the real Codex host.

The harness uses an isolated HOME/CODEX_HOME, a temporary marketplace, a
unique namespace, and only synthetic values. MCP result text is never written
to the evidence transcript; the summary records assertions and tool names.
"""

import argparse
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import tempfile
import time
import uuid


EXPECTED_TOOLS = {
    "kv_set",
    "kv_get",
    "kv_delete",
    "kv_list",
    "mem_save",
    "mem_search",
    "mem_list",
    "mem_delete",
    "search",
}


parser = argparse.ArgumentParser()
parser.add_argument("package", type=Path)
parser.add_argument("--artifact-root", type=Path)
args = parser.parse_args()

artifact_root = args.artifact_root or Path(
    tempfile.mkdtemp(prefix="redis-memory-real-host.")
)
artifact_root.mkdir(parents=True, exist_ok=True)
home = artifact_root / "home"
codex_home = home / ".codex"
codex_home.mkdir(parents=True)
consumer = artifact_root / "consumer"
consumer.mkdir()
market = artifact_root / "marketplace"
package = market / "redis-memory"
shutil.copytree(args.package.resolve(), package)
market_manifest = market / ".agents/plugins/marketplace.json"
market_manifest.parent.mkdir(parents=True)
market_manifest.write_text(
    json.dumps(
        {
            "name": "redis-memory-host-check",
            "plugins": [
                {
                    "name": "redis-memory",
                    "source": {"source": "local", "path": "./redis-memory"},
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": "Productivity",
                }
            ],
        }
    )
)

codex = shutil.which("codex")
assert codex, "codex not found"
run_id = uuid.uuid4().hex
test_ref = f"issue61-{run_id}"
namespace = f"issue61_{run_id}"
kv_key = f"hostcheck_{run_id}"
kv_value = f"synthetic-value-{run_id}"
mem_text = f"Synthetic Redis Memory host check {run_id}."
tag = f"hostcheck-{run_id}"

env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
for key in ("DOCKER_HOST", "DOCKER_CONTEXT"):
    if key in os.environ:
        env[key] = os.environ[key]
env.update(
    HOME=str(home),
    CODEX_HOME=str(codex_home),
    XDG_CONFIG_HOME=str(home / ".config"),
    XDG_CACHE_HOME=str(home / ".cache"),
    XDG_DATA_HOME=str(home / ".local/share"),
    PYTHONDONTWRITEBYTECODE="1",
    REDIS_MEMORY_MCP_MODE="shared",
    REDIS_URL="redis://host.docker.internal:6379/0",
    EMBED_URL="http://host.docker.internal:8081",
    NAMESPACE=namespace,
    INDEX_NAME="idx:memories",
    REDIS_MEMORY_MCP_REF=test_ref,
)

# Seed the bootstrap cache from the checkout. This makes the test exercise the
# current worktree without publishing a Git ref, while start.sh still owns the
# image build and final docker run exactly as it does in production.
bootstrap_cache = home / ".cache/redis-memory-mcp"
bootstrap_cache.mkdir(parents=True)
(bootstrap_cache / ".installed-ref").write_text(test_ref)
shutil.copy2(package / "docker-compose.yaml", bootstrap_cache / "docker-compose.yaml")
shutil.copytree(package / "server", bootstrap_cache / "server")

commands = []
for command in (
    [codex, "plugin", "marketplace", "add", str(market), "--json"],
    [codex, "plugin", "add", "redis-memory@redis-memory-host-check", "--json"],
):
    result = subprocess.run(
        command,
        env=env,
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=60,
    )
    commands.append(
        {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )
    assert result.returncode == 0, result.stderr
(artifact_root / "commands.json").write_text(json.dumps(commands, indent=2))

transcript = []
summary = {
    "codex": subprocess.run(
        [codex, "--version"], capture_output=True, text=True, timeout=10
    ).stdout.strip(),
    "plugin_installed": True,
    "namespace": namespace,
    "test_ref": test_ref,
    "tool_inventory": [],
    "calls": [],
    "cleanup": {"kv": False, "memory": False, "image": False, "index": False},
}
stderr_file = (artifact_root / "app-server.stderr").open("w")
process = subprocess.Popen(
    [codex, "app-server", "--stdio"],
    env=env,
    cwd=consumer,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=stderr_file,
)
buffer = b""
next_id = 10
thread_id = None
server_name = None
memory_id = None
kv_created = False
mem_created = False


def send(message, *, record=True):
    if record:
        transcript.append({"send": message})
    process.stdin.write((json.dumps(message) + "\n").encode())
    process.stdin.flush()


def receive(identifier, timeout=120, *, record=True):
    global buffer
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue
            value = json.loads(line)
            if record:
                transcript.append({"receive": value})
            if value.get("id") == identifier:
                return value
        else:
            ready, _, _ = select.select(
                [process.stdout], [], [], max(0, deadline - time.monotonic())
            )
            if ready:
                data = os.read(process.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError("app-server EOF")
                buffer += data
    raise TimeoutError(identifier)


def content_text(response):
    assert "error" not in response, "Host tool call returned an error"
    result = response.get("result", {})
    assert not result.get("isError"), "MCP tool returned isError"
    return "\n".join(
        block.get("text", "")
        for block in result.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def call_tool(tool, arguments, *, cleanup=False):
    global next_id
    identifier = next_id
    next_id += 1
    send(
        {
            "id": identifier,
            "method": "mcpServer/tool/call",
            "params": {
                "server": server_name,
                "threadId": thread_id,
                "tool": tool,
                "arguments": arguments,
            },
        },
        record=False,
    )
    response = receive(identifier, record=False)
    text = content_text(response)
    transcript.append(
        {
            "tool_call": tool,
            "cleanup": cleanup,
            "is_error": bool(response.get("result", {}).get("isError")),
            "content": "<redacted>",
        }
    )
    summary["calls"].append({"tool": tool, "cleanup": cleanup, "ok": True})
    return text


try:
    send(
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "redis_memory_host_check", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        }
    )
    receive(1)
    send({"method": "initialized", "params": {}})
    send(
        {
            "id": 2,
            "method": "thread/start",
            "params": {
                "cwd": str(consumer),
                "ephemeral": True,
                "sandbox": "read-only",
            },
        }
    )
    thread = receive(2)
    thread_id = thread["result"]["thread"]["id"]
    startup_deadline = time.monotonic() + 330
    status_id = 3
    while True:
        send(
            {
                "id": status_id,
                "method": "mcpServerStatus/list",
                "params": {"detail": "full", "threadId": thread_id},
            }
        )
        status = receive(status_id, timeout=60)
        servers = [
            server
            for server in status["result"]["data"]
            if server.get("pluginId") == "redis-memory@redis-memory-host-check"
        ]
        assert len(servers) == 1, [
            (server["name"], server.get("pluginId")) for server in servers
        ]
        server = servers[0]
        if server["runtimeStatus"] not in ("notStarted", "starting"):
            break
        if time.monotonic() >= startup_deadline:
            raise TimeoutError("redis-memory-mcp remained in startup state")
        time.sleep(2)
        status_id += 1
    server_name = server["name"]
    assert server["runtimeStatus"] == "connected", server["runtimeStatus"]
    actual_tools = set(server["tools"])
    assert actual_tools == EXPECTED_TOOLS, sorted(actual_tools)
    summary["server"] = {
        "name": server_name,
        "plugin_id": server["pluginId"],
        "runtime_status": server["runtimeStatus"],
    }
    summary["tool_inventory"] = sorted(actual_tools)
    safe_status = {
        "server": summary["server"],
        "tools": summary["tool_inventory"],
    }
    (artifact_root / "mcp-status.json").write_text(json.dumps(safe_status, indent=2))

    value = call_tool(
        "kv_set",
        {"key": kv_key, "value": kv_value, "tags": tag, "ttl_days": 1},
    )
    kv_created = True
    assert kv_key in value and kv_value in value
    value = call_tool("kv_get", {"key": kv_key})
    assert kv_key in value and kv_value in value
    value = call_tool("kv_list", {"pattern": kv_key})
    assert kv_key in value and kv_value in value

    value = call_tool(
        "mem_save",
        {"text": mem_text, "label": "Synthetic host check", "tags": tag, "ttl_days": 1},
    )
    match = re.search(r"Saved mem\[([0-9a-f-]{36})\]", value)
    assert match, "mem_save did not return an ID"
    memory_id = match.group(1)
    mem_created = True
    value = call_tool("mem_search", {"query": mem_text, "tags": tag, "top_k": 1})
    assert memory_id in value and mem_text in value
    value = call_tool("mem_list", {"limit": 2, "tag": tag})
    assert memory_id in value and mem_text in value
    value = call_tool("search", {"query": run_id, "tags": tag, "top_k": 1})
    assert kv_key in value and memory_id in value

    # Exercise read access to the pre-existing shared memory area without
    # persisting or printing any returned record content.
    value = call_tool("mem_list", {"limit": 1, "shared": True})
    assert value
    summary["shared_memory_read"] = {
        "called": True,
        "record_present": value != "No semantic memories found.",
        "content_recorded": False,
    }

    value = call_tool("kv_delete", {"key": kv_key}, cleanup=True)
    assert value == f"Deleted kv[{kv_key}]"
    kv_created = False
    summary["cleanup"]["kv"] = True
    value = call_tool("mem_delete", {"memory_id": memory_id}, cleanup=True)
    assert value == f"Deleted mem[{memory_id}]"
    mem_created = False
    summary["cleanup"]["memory"] = True
    value = call_tool("kv_get", {"key": kv_key})
    assert value == f"Not found: '{kv_key}'"
    value = call_tool("mem_list", {"limit": 2, "tag": tag})
    assert value == "No semantic memories found."
    summary["verified"] = True
finally:
    if server_name and thread_id:
        if kv_created:
            try:
                call_tool("kv_delete", {"key": kv_key}, cleanup=True)
                summary["cleanup"]["kv"] = True
            except Exception:
                summary["cleanup"]["kv"] = False
        if mem_created and memory_id:
            try:
                call_tool("mem_delete", {"memory_id": memory_id}, cleanup=True)
                summary["cleanup"]["memory"] = True
            except Exception:
                summary["cleanup"]["memory"] = False
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    stderr_file.close()
    image_name = f"redis-memory-mcp:{test_ref}"
    image_cleanup = subprocess.run(
        ["docker", "image", "rm", "-f", image_name],
        env=env,
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=60,
    )
    summary["cleanup"]["image"] = image_cleanup.returncode == 0
    index_cleanup = subprocess.run(
        [
            "docker",
            "exec",
            "redis-stack",
            "redis-cli",
            "FT.DROPINDEX",
            f"idx:memories:{namespace}",
            "DD",
        ],
        env=env,
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=30,
    )
    summary["cleanup"]["index"] = index_cleanup.returncode == 0 and (
        "OK" in index_cleanup.stdout or "Unknown Index name" in index_cleanup.stdout
    )
    (artifact_root / "transcript.json").write_text(json.dumps(transcript, indent=2))
    (artifact_root / "summary.json").write_text(json.dumps(summary, indent=2))

print(artifact_root)
print(json.dumps(summary, indent=2))
assert all(summary["cleanup"].values()), "Test cleanup incomplete; inspect summary.json"
