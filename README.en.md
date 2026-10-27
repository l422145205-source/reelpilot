# ReelPilot

**v0.6 operational analytics:** a read-only journal extractor and transactional SQLite marts separate logical requests, admission attempts, stage commits and cache-reuse events. Sequence watermarks, hash-based snapshot UPSERTs and atomic checkpoints support replay and rollback. A real original-media lab reconciles 6 logical requests, 8 accepted attempts and 2 render commits. No model-quality or production success-rate claim is made. [Schema, SQL, reproduction and interview walkthrough](docs/analytics.md)

**A time-grounded video retrieval and rough-cut agent with recoverable execution.**

[中文](README.md) · [Engineering case study](docs/engineering/cancellation-and-completion.md) · [Contributing](CONTRIBUTING.md)

[![CPU contracts and media pipeline](https://github.com/l422145205-source/reelpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/l422145205-source/reelpilot/actions/workflows/ci.yml)

Turn a brief such as “find a forest and a robot” into a checked edit plan and a playable video. Models propose search goals; deterministic code owns source intervals, duration limits, file publication and tool execution.

Normal-wheel installation and a separate stdio MCP container have a dedicated
[distribution validation path](docs/distribution.md): the source archive is hash
checked before building, and discovery runs outside the editable checkout. The
linked CI run and JSON artifacts distinguish completed checks from configuration.

![Workbench](examples/workbench.png)

## Try it without a GPU or model download

Python 3.11+, from a cloned checkout:

```bash
git clone https://github.com/l422145205-source/reelpilot.git
cd reelpilot
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .
reelpilot selfcheck
```

The self-check generates original synthetic media, commits a render, injects a controlled Python interruption, resumes from the verified artifact, replays the request, then corrupts **only its newly created output** and verifies repair. It prints the output directory and a JSON report. Existing files are preserved; no network model service is called. This checks installation and execution contracts, not model semantics or OS-crash recovery. See an [actual self-check report](examples/selfcheck-report.json).

For the interactive workbench:

```bash
reelpilot demo --collection data/demo
reelpilot serve --collection data/demo
```

Open <http://127.0.0.1:8765>, use explicit planning and lexical retrieval, and enter `forest; robot`. This mode does not call an LLM. FFmpeg is provided by `imageio-ffmpeg`.

## What is implemented

| Area | Implementation |
|---|---|
| Multimodal retrieval | Whisper word timestamps; six-second windows; Qwen3-VL-Embedding image, dialogue and joint indexes; optional BM25/RRF |
| Planning and compilation | Qwen3 + constrained JSON; typed EditPlan; bounded beam search; independent source/time/budget checks |
| Execution | Fixed-argument FFmpeg tools; content-verified stage replay; request and pipeline ownership leases |
| Task control | Persistent cancellation, attempt deadlines, atomic completion decisions, optional per-job worker processes |
| Model resources | Bounded FIFO admission, fingerprinted LRU query vectors, same-key single-flight, opt-in resident planner |
| MCP integration | Local stdio, current/legacy protocol negotiation, bounded read tools, opt-in render/cancel, durable receipt recovery across client processes |

The workbench supports source seeking, candidate inspection, progress, cancellation, playback and edit-plan export. A [saved 12-second result](examples/recovery-preview.mp4) and [retrieval frames](examples/retrieval-preview.jpg) are available without running models; source attribution is in [THIRD_PARTY.md](THIRD_PARTY.md).

## Reproduce the engineering evidence

For tool integration with an MCP host, install `pip install -e '.[mcp]'` and see
the [MCP guide](docs/mcp.md). Default tools are read-only; enabling rendering uses
the API's original durable IDs, worker processes and stage journal. Actual stdio
disconnect/reconnect and FFmpeg integration evidence is included, without a GPU.

```bash
python -m pip install -e '.[dev]'
python scripts/validate_resources.py
python scripts/measure_model_resources.py
# Optional models dependencies are needed for this tiny CPU probe; no weights are downloaded.
python scripts/validate_generation_control.py
```

The v0.4 snapshot records 103 passing CPU/media tests across Windows and Ubuntu CI, seven real process experiments, and a separate tiny random Transformer probe. New commits may add tests; consult Actions for the current count. The probe exercised actual generation cancellation and shared eight same-key requests across one forward. It does not validate pretrained Qwen behavior or GPU speedups. Controlled-delay experiments label their injected delays explicitly.

One real bug was a two-read supervision race: completion could occur between “is complete?” and an ownership check, causing a false lost-owner error. A deterministic regression reproduces that old sequence; the supervisor now validates one row snapshot. [Read the case study](docs/engineering/cancellation-and-completion.md).

## Model experiments and limits

The original retrieval pilot uses two public films, about 13.1 minutes, 132 windows and 30 bilingual query forms over 15 scene intents. Labels were agent-assisted and developmental. Image-only MRR@10 was 0.9319 in that small pilot; adding BM25/RRF did not consistently help visual queries. This is not an independent quality benchmark or a production claim. See the [raw rankings](reports/retrieval_pilot.json) and [full experiment discussion](docs/experiments.md).

Real-model setup, pinned revisions and downloads are documented in the [Chinese guide](README.md#运行真实模型与公开素材). Full weights and films are excluded from Git. The project is a single-machine prototype: no multi-tenant authentication, distributed GPU scheduler or production SLA. Thread cancellation is cooperative; process containment is lifecycle management for trusted tools, not a security sandbox. Power-loss atomicity has not been established.

## Read the implementation

- [Model resources](src/reelpilot/resources.py), [design and evidence](docs/model-resources.md).
- [Journal and finalization](src/reelpilot/journal.py), [runtime controls](docs/runtime-controls.md).
- [Supervisor](src/reelpilot/supervisor.py), [real process tests](tests/test_process_runtime.py).
- [Compiler](src/reelpilot/compiler.py), [architecture](docs/architecture.md).

Code was developed with AI coding assistance. Model sources, test doubles, actual runs and experimental limits are documented separately. Code: [MIT](LICENSE).
