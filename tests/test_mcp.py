"""Real SDK protocol calls plus a subprocess/HTTP/FFmpeg integration, no models."""

import asyncio
import json
import hashlib
from importlib.metadata import version
import os
from pathlib import Path
import socket
import sys
from threading import Thread
import time

import httpx
import pytest

pytest.importorskip("mcp.server.mcpserver")
from mcp import Client, StdioServerParameters
import uvicorn

from reelpilot.api import create_app
from reelpilot.demo import make_demo
from reelpilot.mcp_server import GatewayConfig, create_server
from reelpilot.media import probe

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_real_sdk_discovery_read_only_tools_and_structured_output(mode):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "version": "fixture",
                "assets": 1,
                "segments": 3,
                "secret_path": "do not disclose",
            },
        )

    async def exercise():
        async with Client(
            create_server(transport=httpx.MockTransport(handle)), mode=mode
        ) as client:
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools} == {
                "service_status",
                "search_clips",
                "inspect_edit",
            }
            assert all(t.annotations.read_only_hint for t in tools)
            result = await client.call_tool("service_status")
            assert not result.is_error
            assert result.structured_content == {
                "status": "ok",
                "version": "fixture",
                "assets": 1,
                "segments": 3,
            }
            assert client.protocol_version == (
                "2026-07-28" if mode == "auto" else "2025-11-25"
            )
            forbidden = await client.call_tool("submit_edit", {"job_id": "not-allowed"})
            assert forbidden.is_error

    asyncio.run(exercise())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765",
        "http://example.com:8765",
        "http://localhost:8765",
        "http://127.0.0.1:8765/path",
        "http://user@127.0.0.1:8765",
        "http://127.0.0.1:8765?x=1",
        "http://127.0.0.1:8765#x",
        "http://127.0.0.1:99999",
        "file:///tmp/data",
        "http://127.0.0.1",
    ],
)
def test_gateway_rejects_nonlocal_or_ambiguous_operator_endpoints(url):
    with pytest.raises(ValueError):
        GatewayConfig(api_url=url)


@pytest.mark.parametrize(
    "name,args",
    [
        ("search_clips", {"query": "x" * 601}),
        ("search_clips", {"query": "robot", "limit": 11}),
        ("inspect_edit", {"job_id": "../escape"}),
        (
            "submit_edit",
            {"job_id": "id", "brief": {"text": "robot", "budget_seconds": 91}},
        ),
        (
            "submit_edit",
            {
                "job_id": "id",
                "brief": {"text": "robot"},
                "execution_timeout_seconds": 301,
            },
        ),
    ],
)
def test_mcp_schema_blocks_out_of_budget_requests_before_http(name, args):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={})

    async def exercise():
        server = create_server(
            GatewayConfig(allow_render=True), transport=httpx.MockTransport(handle)
        )
        async with Client(server) as client:
            assert (await client.call_tool(name, args)).is_error

    asyncio.run(exercise())
    assert calls == []


@pytest.mark.parametrize(
    "case,code",
    [
        ("redirect", "backend_error"),
        ("capacity", "capacity_exhausted"),
        ("huge", "response_too_large"),
        ("invalid", "invalid_response"),
        ("timeout", "backend_timeout"),
    ],
)
def test_failures_are_bounded_and_never_retried_or_redirected(case, code):
    calls = []

    def handle(request):
        calls.append(request)
        if case == "redirect":
            return httpx.Response(
                302, headers={"Location": "https://example.com/private"}
            )
        if case == "capacity":
            return httpx.Response(
                429, json={"detail": "private backend path must not be echoed"}
            )
        if case == "huge":
            return httpx.Response(200, content=b"x" * 1024)
        if case == "invalid":
            return httpx.Response(200, json=[])
        raise httpx.ReadTimeout("fixture timeout", request=request)

    async def exercise():
        server = create_server(
            GatewayConfig(response_bytes=512), transport=httpx.MockTransport(handle)
        )
        async with Client(server) as client:
            result = await client.call_tool("service_status")
            assert result.is_error
            text = "\n".join(c.text for c in result.content if c.type == "text")
            assert code in text
            assert "private backend path" not in text

    asyncio.run(exercise())
    assert len(calls) == 1


def test_search_projects_source_evidence_without_filesystem_paths():
    def handle(request):
        assert json.loads(request.content)["limit"] == 1
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "segment": {
                            "id": "s1",
                            "asset_id": "demo",
                            "start": 0,
                            "end": 4,
                            "frame": "private/frame.jpg",
                            "transcript": "x" * 2000,
                        },
                        "score": 1.0,
                        "evidence": ["match"],
                    }
                ]
            },
        )

    async def exercise():
        async with Client(
            create_server(transport=httpx.MockTransport(handle))
        ) as client:
            result = await client.call_tool(
                "search_clips", {"query": "robot", "limit": 1}
            )
            assert not result.is_error
            data = result.structured_content
            assert len(data["hits"][0]["transcript"]) == 1000
            assert data["source_text_is_untrusted"]
            assert "private/frame" not in json.dumps(data)

    asyncio.run(exercise())


@pytest.fixture(scope="module")
def mcp_video_api(tmp_path_factory):
    root = tmp_path_factory.mktemp("mcp-real-media")
    library = make_demo(root / "collection")
    app = create_app(library.root, root / "jobs", execution_backend="process")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "local fixture API did not start"
        yield f"http://127.0.0.1:{port}", root
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()
        assert not thread.is_alive(), "local fixture API did not drain"


def test_stdio_disconnect_reconnect_real_render_replay_and_conflict(mcp_video_api):
    url, root = mcp_video_api
    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "reelpilot.mcp_server", "--api-url", url, "--allow-render"],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    request = {
        "job_id": "mcp-original",
        "brief": {"text": "forest;robot", "budget_seconds": 8},
    }

    async def wait(client):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = await client.call_tool("inspect_edit", {"job_id": "mcp-original"})
            assert not result.is_error, result
            if result.structured_content["status"] not in {"queued", "running"}:
                return result.structured_content
            await asyncio.sleep(0.03)
        raise AssertionError("real render did not reach terminal receipt")

    async def exercise():
        evidence = {
            "scenario": "actual stdio MCP processes -> local HTTP API -> isolated worker -> FFmpeg",
            "mcp_sdk": version("mcp"),
            "protocol_versions": [],
            "synthetic_original_media": True,
            "pretrained_model_or_network_api_used": False,
        }
        async with Client(params, read_timeout_seconds=30) as first:
            evidence["protocol_versions"].append(first.protocol_version)
            assert {t.name for t in (await first.list_tools()).tools} == {
                "service_status",
                "search_clips",
                "inspect_edit",
                "submit_edit",
                "cancel_edit",
            }
            hit = await first.call_tool("search_clips", {"query": "robot"})
            assert hit.structured_content["hits"][0]["segment_id"] == "demo_0001"
            submitted = await first.call_tool("submit_edit", request)
            assert not submitted.is_error, submitted
        # Close the actual stdio server process. The durable job belongs to the API.
        async with Client(params, mode="legacy", read_timeout_seconds=30) as second:
            evidence["protocol_versions"].append(second.protocol_version)
            state = await wait(second)
            assert state["status"] == "complete", state
            assert state["media"]["width"] == 640
            assert abs(state["media"]["duration"] - 8) < 0.25
            assert (
                next(s for s in state["stages"] if s["stage"] == "render")["attempts"]
                == 1
            )
            assert (await second.call_tool("submit_edit", request)).is_error is False
            replay = await wait(second)
            assert replay["status"] == "complete"
            assert (
                next(s for s in replay["stages"] if s["stage"] == "render")["attempts"]
                == 1
            )
            evidence["first_render_attempts"] = next(
                s for s in state["stages"] if s["stage"] == "render"
            )["attempts"]
            evidence["replay_render_attempts"] = next(
                s for s in replay["stages"] if s["stage"] == "render"
            )["attempts"]
            evidence["receipt_recovered_in_new_stdio_process"] = True
            conflict = {**request, "brief": {"text": "dragon", "budget_seconds": 8}}
            error = await second.call_tool("submit_edit", conflict)
            assert error.is_error
            assert "job_conflict" in " ".join(
                c.text for c in error.content if c.type == "text"
            )
            evidence["conflicting_payload_rejected"] = True
            cancelled = await second.call_tool(
                "cancel_edit", {"job_id": "mcp-original"}
            )
            assert cancelled.structured_content["status"] == "complete"
            assert cancelled.structured_content["requested"] is False
            evidence["completed_job_not_retroactively_cancelled"] = True
            missing = await second.call_tool("inspect_edit", {"job_id": "absent"})
            assert missing.is_error
        with httpx.Client(trust_env=False) as client:
            response = client.get(url + "/api/jobs/mcp-original/output")
            assert response.status_code == 200
            output = root / "verified-from-api.mp4"
            output.write_bytes(response.content)
            assert probe(output)["width"] == 640
            evidence["output_media"] = {
                k: probe(output)[k] for k in ("width", "height", "duration")
            }
            evidence["output_sha256"] = hashlib.sha256(response.content).hexdigest()
        if target := os.environ.get("REELPILOT_MCP_REPORT"):
            target = Path(target).resolve()
            if not target.is_relative_to(ROOT):
                raise ValueError("MCP evidence output must stay inside the checkout")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    asyncio.run(exercise())
