# Contributing

ReelPilot is a local video-retrieval and agent-runtime prototype. Small, reproducible improvements are welcome: execution races, media portability, retrieval failure cases, clearer documentation and examples.

## Start without models

```bash
python -m pip install -e '.[dev]'
reelpilot selfcheck
pytest
```

The self-check writes a fresh UUID directory under `data/selfcheck/`; it does not remove existing files. Its interruption is a Python exception. Real process-death and service-restart tests are separate:

```bash
python scripts/validate_resources.py
```

## Report a useful bug

Include the exact command or minimal request, Python/OS versions, expected behavior, observed error and whether the problem occurs in the model-free demo. Use original, synthetic or publicly licensed media for a reproducer; keep credentials and private media out of logs.

State whether a model call, test double, controlled delay or actual subprocess was involved. A timeout during setup is different from a failure at a specified crash checkpoint.

## Changes to experiments

Keep published snapshots intact. Add a separately named report with configuration, sample provenance, selection procedure, raw outcomes and limitations. Report negative results. A ranking metric is not answer accuracy; a passing execution test is not a model-quality result; a tiny random-model probe is not pretrained-model validation.

## Changes to runtime controls

Preserve these contracts:

- An old owner cannot commit artifacts or overwrite its successor.
- Accepted cancellation cannot be reset by retrying the same job ID.
- Completion and cancellation are ordered by the persistent transaction.
- Cache waiters retain their own cancellation rights; returned vectors cannot mutate cached state.
- New work is bounded; shutdown must not silently discard durable receipts.

Run the relevant regression and the full CPU suite when changing these boundaries. Explain the failure trigger and before/after behavior in a pull request. GPU checkpoints and full films are optional and must not become prerequisites for CPU CI.

See the [engineering case study](docs/engineering/cancellation-and-completion.md) for a concrete regression-first workflow and [THIRD_PARTY.md](THIRD_PARTY.md) for asset provenance.
