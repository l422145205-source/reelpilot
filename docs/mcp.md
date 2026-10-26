# Durable video tools over MCP

ReelPilot exposes its existing API as a **local stdio MCP server**, using the official
Python SDK 2.2.0. A host can search time-grounded clips and submit a bounded edit
without opening the workbench. The API remains the owner of durable jobs and
isolated media workers; the MCP process does not become a second scheduler.

## Run locally

From the cloned checkout, in a virtual environment (Python 3.11+):

```bash
python -m pip install -e '.[mcp]'
reelpilot demo --collection data/demo
reelpilot serve --collection data/demo --execution-backend process
```

Keep that API running. Configure your MCP host to launch:

```json
{
  "mcpServers": {
    "reelpilot": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["-m", "reelpilot.mcp_server", "--api-url", "http://127.0.0.1:8765"]
    }
  }
}
```

On Windows, use the virtual environment's `Scripts/python.exe`. This is a host
configuration example, not a claim that every desktop MCP host has been tested.
The equivalent executable is `reelpilot-mcp`. Stdout carries MCP messages only.

Default tools are `service_status`, `search_clips` and `inspect_edit`. To enable
`submit_edit` and `cancel_edit`, the operator must add `--allow-render` to the
server's launch arguments. With that flag absent, write tools are not advertised
and calls to their names fail. Annotations describe behavior; they do not provide
authentication or replace the host's own approval policy.

## A recoverable call sequence

1. Call `service_status` to inspect configured model capabilities. The default
   demo uses lexical retrieval and explicit goals; it calls no pretrained model.
2. Call `search_clips` with `{"query":"robot","limit":5}`. Evidence contains
   stable segment/asset IDs and source times, not local file paths. Source
   transcripts are untrusted data, not instructions.
3. With rendering enabled, call `submit_edit`:

   ```json
   {"job_id":"demo-edit-1","brief":{"text":"forest;robot","budget_seconds":8}}
   ```

4. Poll `inspect_edit` with that same ID. Once committed, it returns media
   dimensions, duration, stage attempt counts and a local output URL.
5. If the MCP connection closes or submission times out, **inspect the original
   ID first**. An HTTP timeout has an unknown outcome; it does not prove that no
   job was accepted. There are no automatic POST retries or invented replacement
   IDs. A live or mismatched ID produces `job_conflict`.
6. Explicit `cancel_edit` requests cancellation. Its receipt is not evidence that
   a worker has exited: poll to a terminal state. Cancelling an already completed
   job returns `requested: false` and leaves committed completion unchanged.

Replaying an identical completed request can reuse hash-verified stages. It does
not promise exactly-once tool execution after a process failure. All fields,
including provider and execution timeout, are part of the original request; do
not change them while reusing the ID.

## Boundaries and failure behavior

- The operator chooses one HTTP loopback IP and explicit port. No per-tool URL,
  arbitrary path, shell, ingest or network-download tool is exposed. DNS names,
  credentials, URL paths and redirects are rejected; environment proxy settings
  are ignored.
- HTTP connect/read waits are bounded; decoded backend response bytes are capped
  at 2 MiB. Search accepts at most 10 results and 20 asset IDs; transcript snippets
  are capped at 1,000 characters. These are response/accounting budgets, not a
  process RSS or total-memory guarantee.
- Tool inputs use the same validated `Brief` and ID contracts as the API. MCP
  execution timeout is capped at 300 seconds. Capacity, conflict, backend timeout,
  invalid response and missing-job errors are distinguishable. Backend exception
  strings and local paths are not echoed.
- Closing the stdio client does **not** cancel a job owned by the API. The
  process execution backend retains its existing worker containment and exit
  acknowledgment. This gateway adds no public HTTP MCP endpoint or multi-user
  authentication; loopback restriction is not a security sandbox.

## Reproduce protocol and media evidence

```bash
python -m pip install -e '.[dev,mcp]'
pytest tests/test_mcp.py -q
```

The integration starts an actual local API, launches and closes real stdio MCP
server processes, searches original synthetic video, submits a real FFmpeg edit,
reconnects to recover the same receipt, replays it and rejects conflicting reuse.
It also checks cancellation of an already completed job. Separate protocol tests
cover read-only discovery, bounded inputs/responses, redirects, timeouts and
capacity errors. Both `2026-07-28` negotiation and the SDK client's legacy
`2025-11-25` mode are exercised. This tests wire compatibility through the current
official SDK, not every historical SDK binary or desktop host.

[Actual integration record](../reports/mcp_validation.json) records the original
8-second media output and render stage attempt counts. HTTP fault cases use
explicit test doubles; the stdio/HTTP/worker/FFmpeg integration does not. These
checks measure protocol and execution contracts, not model quality or adoption.

SDK references: [current official guide](https://github.com/modelcontextprotocol/python-sdk),
[v2 changes](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md).
