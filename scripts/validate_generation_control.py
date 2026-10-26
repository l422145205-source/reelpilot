"""No downloads: test the production cancellation hook on a tiny random CPU Transformer."""

import argparse
import json
from pathlib import Path
import platform
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import time


def run():
    import torch
    import transformers
    from transformers import BatchEncoding, GPT2Config, GPT2LMHeadModel
    from reelpilot.control import TaskCancelled
    from reelpilot.planner import TransformersPlanner, generation_guard
    from reelpilot.schema import Brief

    torch.set_num_threads(1)
    torch.manual_seed(7)
    model = GPT2LMHeadModel(
        GPT2Config(
            vocab_size=64,
            n_positions=128,
            n_embd=16,
            n_layer=1,
            n_head=2,
            bos_token_id=1,
            eos_token_id=None,
            pad_token_id=0,
        )
    ).eval()
    ids = torch.tensor([[1, 3]])
    completed = []
    with torch.inference_mode():
        output = model.generate(
            ids,
            max_new_tokens=8,
            do_sample=False,
            stopping_criteria=generation_guard(lambda: completed.append(True)),
        )
    assert output.shape[1] == 10 and len(completed) == 8
    forwards = []
    hook = model.register_forward_hook(lambda *args: forwards.append(True))

    class Tokenizer:
        eos_token_id = 0

        def apply_chat_template(self, *a, **kw):
            return "controlled interface probe"

        def __call__(self, *a, **kw):
            return BatchEncoding({"input_ids": ids})

        def decode(self, *a, **kw):
            raise AssertionError("cancelled generation must not decode partial output")

    planner = TransformersPlanner.__new__(TransformersPlanner)
    planner.model = model
    planner.tokenizer = Tokenizer()
    planner.prefix = None
    planner._new_prefix = lambda: None  # Control probe, not JSON constraint validation.
    planner.torch = torch
    planner.device = "cpu"

    def cancel():
        if len(forwards) >= 4:
            raise TaskCancelled("controlled token-boundary cancellation")

    try:
        try:
            planner.spec(Brief(text="forest"), check=cancel)
        except TaskCancelled:
            pass
        else:
            raise AssertionError("production planner accepted cancelled generation")
        assert len(forwards) == 4
    finally:
        hook.remove()
        planner.close()
    assert planner.model is None
    from reelpilot.resources import ModelResources

    calls = []
    entered, release = Event(), Event()

    class TinyEncoder:
        fingerprint = "random-cpu-gpt2-v1"

        def encode(self, *, text, check):
            check()
            entered.set()
            if text == "synchronized":
                assert release.wait(10)
            calls.append(True)
            with torch.inference_mode():
                vector = model.transformer(ids).last_hidden_state.mean(dim=1)[0]
                vector = torch.nn.functional.normalize(vector.float(), dim=0)
            return vector.numpy()

        def close(self):
            pass

    raw = TinyEncoder()
    for _ in range(8):
        raw.encode(text="baseline", check=lambda: None)
    assert len(calls) == 8
    calls.clear()
    entered.clear()
    barrier = Barrier(8)
    resources = ModelResources(encoder_factory=TinyEncoder, fingerprint=TinyEncoder.fingerprint)

    def query():
        barrier.wait(timeout=10)
        return resources.query("synchronized")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(query) for _ in range(8)]
        try:
            assert entered.wait(10)
            until = time.monotonic() + 10
            while resources.cache.stats()["coalesced"] != 7:
                if time.monotonic() > until:
                    raise TimeoutError("tiny model followers did not join")
                time.sleep(0.005)
        finally:
            release.set()
        vectors = [f.result(timeout=10) for f in futures]
    assert len(calls) == 1
    vectors[0][0] = 100
    assert all(v[0] != 100 for v in vectors[1:])
    resources.close()
    return {
        "schema_version": 1,
        "date": "2026-10-26",
        "os": platform.system(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "model": "locally initialized random GPT2, 1 layer, 16 hidden, CPU",
        "weights_downloaded": False,
        "parameters": sum(p.numel() for p in model.parameters()),
        "normal_new_tokens": 8,
        "normal_control_checks": len(completed),
        "cancel_forward_steps": len(forwards),
        "partial_output_accepted": False,
        "planner_closed": planner.model is None,
        "embedding_singleflight": {
            "baseline_forwards": 8,
            "shared_forwards": len(calls),
            "requests": 8,
            "coalesced_waiters": 7,
            "separate_return_buffers": True,
            "provider": "actual random GPT2 hidden-state forward on CPU",
            "instrumentation": "barrier before forward forces concurrent misses; no inference speed claim",
        },
        "limitations": [
            "generation-interface contract only",
            "not a pretrained Qwen checkpoint validation",
            "tokenizer is a controlled adapter; no semantic or GPU latency claim",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("reports/generation_control.json"))
    args = parser.parse_args()
    report = run()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
