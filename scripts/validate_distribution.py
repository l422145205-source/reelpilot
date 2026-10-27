"""Smoke-test a normal wheel installation, not an editable source checkout.

Run this file with the target venv's Python, from a directory outside the checkout.
It installs nothing, downloads no models, and never starts the video HTTP backend.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from importlib import metadata, resources
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import uuid


CHECKOUT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {"service_status", "search_clips", "inspect_edit"}
STATIC_FILES = ("index.html", "app.js", "style.css")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def child_environment():
    env = dict(os.environ)
    # Do not let either the console scripts or the MCP subprocess see source-path
    # injections inherited from development tooling.
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def installed_package():
    prefix = Path(sys.prefix).resolve()
    require(sys.prefix != sys.base_prefix, "Run with a dedicated ordinary virtual environment")
    cfg = prefix / "pyvenv.cfg"
    require(cfg.is_file(), "The target environment has no pyvenv.cfg")
    config = cfg.read_text(encoding="utf-8").lower()
    require(
        not re.search(r"include-system-site-packages\s*=\s*true", config),
        "The target venv must not inherit system site-packages",
    )
    require(
        not Path.cwd().resolve().is_relative_to(CHECKOUT),
        "Run from a working directory outside the source checkout",
    )

    import reelpilot

    module_path = Path(reelpilot.__file__).resolve()
    package_roots = {
        Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")
    }
    require(
        any(module_path.is_relative_to(p) and p.is_relative_to(prefix) for p in package_roots),
        "reelpilot was not imported from this venv's installed site-packages",
    )
    distribution = metadata.distribution("reelpilot")
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    require(
        direct_url.get("dir_info", {}).get("editable") is not True,
        "Editable installs do not validate wheel distribution",
    )
    require(module_path.parent == Path(distribution.locate_file("reelpilot")).resolve(),
            "Imported module and distribution metadata refer to different locations")

    static = {}
    for name in STATIC_FILES:
        data = resources.files("reelpilot").joinpath("static", name).read_bytes()
        require(bool(data), f"Installed static resource is empty: {name}")
        static[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    sql = resources.files("reelpilot").joinpath("analytics.sql").read_bytes()
    require(b'CREATE VIEW IF NOT EXISTS daily_metrics' in sql, 'Installed analytics SQL is missing')
    from reelpilot.analytics import ingest_journal, report_warehouse
    from reelpilot.admission import Admission
    from reelpilot.journal import Journal

    scratch = Path.cwd()/('installed-analytics-'+uuid.uuid4().hex)
    journal = Journal(scratch/'source')
    Admission(journal).reserve('installed-check', {'provider': 'explicit', 'mode': 'lexical'}).finish()
    warehouse = scratch/'warehouse.sqlite'
    ingest_journal(journal.db, warehouse, source_id='wheel-smoke')
    repeat = ingest_journal(journal.db, warehouse, source_id='wheel-smoke')
    require(report_warehouse(warehouse)['daily'][0]['logical_requests'] == 1,
            'Installed analytics ingestion/report failed')
    require(repeat['inserted_events'] == repeat['changed_requests'] == 0,
            'Installed analytics replay duplicated facts')
    return {
        "version": distribution.version,
        "module_in_current_venv": True,
        "module_relative_to_venv": module_path.relative_to(prefix).as_posix(),
        "editable": False,
        "system_site_packages": False,
        "cwd_outside_checkout": True,
        "static": static,
        "analytics_sql_sha256": hashlib.sha256(sql).hexdigest(),
        "analytics_ingest_and_replay": True,
    }


def console_scripts():
    results = []
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("reelpilot", "reelpilot-mcp"):
        executable = Path(sysconfig.get_path("scripts")) / (name + suffix)
        require(executable.is_file(), f"Installed console entry point is missing: {name}")
        result = subprocess.run(
            [str(executable), "--help"],
            env=child_environment(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            check=False,
        )
        require(result.returncode == 0 and bool(result.stdout.strip()),
                f"Installed console entry point failed: {name} (exit {result.returncode})")
        results.append({"name": name, "help_exit_code": result.returncode})
    return results


async def inspect_stdio(params, mode):
    from mcp import Client

    async with Client(params, mode=mode, read_timeout_seconds=20) as client:
        # Ping was removed in the modern wire protocol. Never send it there.
        if mode == "legacy":
            await client.send_ping()
        tools = (await client.list_tools()).tools
        require({t.name for t in tools} == EXPECTED_TOOLS, "Unexpected read-only MCP tool set")
        require(all(t.input_schema for t in tools), "A discovered tool has no input schema")
        require(all(t.annotations and t.annotations.read_only_hint for t in tools),
                "Default tools must advertise read-only behavior")
        listed_resources = (await client.list_resources()).resources
        listed_prompts = (await client.list_prompts()).prompts
        require(not listed_resources and not listed_prompts,
                "Unexpected resources/prompts in the default gateway")
        unavailable = await client.call_tool("service_status")
        error_text = " ".join(getattr(c, "text", "") for c in unavailable.content)
        require(unavailable.is_error and "backend_unavailable" in error_text,
                "An absent backend did not produce the documented explicit error")
        # Force an actual protocol request, rather than accepting cached discovery.
        again = (await client.list_tools(cache_mode="refresh")).tools
        require({t.name for t in again} == EXPECTED_TOOLS,
                "Discovery did not survive an unavailable-backend tool call")
        if mode == "legacy":
            await client.send_ping()
        schema = [t.model_dump(mode="json", by_alias=True, exclude_none=True)
                  for t in sorted(tools, key=lambda item: item.name)]
        schema_bytes = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
        return {
            "mode": mode,
            "protocol": client.protocol_version,
            "server": {"name": client.server_info.name, "version": client.server_info.version},
            "tools": sorted(t.name for t in tools),
            "tool_schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
            "resources": len(listed_resources),
            "prompts": len(listed_prompts),
            "absent_backend_error": "backend_unavailable",
            "discovery_after_tool_error": True,
        }


async def local_probe(mode):
    from mcp import StdioServerParameters

    # Reserve a port without listening: no user service can occupy the test target.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        params = StdioServerParameters(
            command=sys.executable,
            args=["-I", "-B", "-m", "reelpilot.mcp_server", "--api-url", f"http://127.0.0.1:{port}"],
            cwd=Path.cwd(),
            env=child_environment(),
        )
        return await inspect_stdio(params, mode)


async def docker_probe(image, mode):
    from mcp import StdioServerParameters

    docker = shutil.which("docker")
    require(docker is not None, "--docker-image requires a working Docker CLI/daemon")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", image) is not None,
            "Invalid Docker image reference")
    container_name = "reelpilot-distribution-" + uuid.uuid4().hex
    params = StdioServerParameters(
        command=docker,
        args=[
            "run", "--rm", "-i", "--pull", "never", "--name", container_name,
            "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--env", "PYTHONDONTWRITEBYTECODE=1", image,
        ],
        cwd=Path.cwd(),
        env=child_environment(),
    )
    try:
        result = await inspect_stdio(params, mode)
        result.update({"image": image, "network": "none", "root_filesystem": "read-only",
                       "backend_available_for_probe": False})
        return result
    finally:
        # Timeout/client failure must not leave our own named container behind.
        # This UUID name is generated here; no pre-existing container is targeted.
        subprocess.run([docker, "rm", "--force", container_name],
                       capture_output=True, timeout=20, check=False)


def error_summary(exc):
    leaves = []

    def collect(item):
        if isinstance(item, BaseExceptionGroup):
            for child in item.exceptions:
                collect(child)
        else:
            leaves.append(f"{type(item).__name__}: {item}")

    collect(exc)
    message = " | ".join(leaves[:4])[:1600]
    for path, marker in ((CHECKOUT, "${CHECKOUT}"), (Path(sys.prefix), "${VENV}"),
                         (Path.cwd(), "${WORKING_DIRECTORY}"), (Path.home(), "${HOME}")):
        for spelling in (str(path), str(path).replace("\\", "/")):
            message = message.replace(spelling, marker)
    return message


async def validate(args, report):
    report["package"] = installed_package()
    report["console_scripts"] = console_scripts()
    report["mcp_sdk"] = metadata.version("mcp")
    modes = ("auto", "legacy") if args.legacy else ("auto",)
    report["local_stdio"] = [await local_probe(mode) for mode in modes]
    if args.docker_image:
        report["docker_stdio"] = [await docker_probe(args.docker_image, mode) for mode in modes]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy", action="store_true", help="also check the SDK's legacy wire mode")
    parser.add_argument("--docker-image", help="also inspect a prebuilt stdio MCP image; does not build/pull it")
    args = parser.parse_args()
    report = {
        "schema_version": 1,
        "scope": "Installed wheel/entrypoint and stdio discovery; not backend or rendering readiness",
        "python": platform.python_version(),
        "platform": sys.platform,
        "api_started_by_validator": False,
        "pretrained_model_requests_by_validator": False,
        "status": "failed",
    }
    try:
        asyncio.run(validate(args, report))
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = error_summary(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
