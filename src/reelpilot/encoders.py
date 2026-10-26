"""Local Qwen3-VL image/text embeddings with explicit, fingerprinted pooling."""

from __future__ import annotations

import gc
import json
from pathlib import Path
from threading import RLock

import numpy as np

from .library import Library, stable_hash
from .resources import VectorCache


def embedding_identity(model_path, instruction="Represent the user's input."):
    path = Path(model_path)
    manifest_path = path / "download_manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {"repo": str(model_path)}
    )
    fingerprint = stable_hash(
        {
            "model": manifest.get("repo"),
            "revision": manifest.get("revision"),
            "instruction": instruction,
            "pooling": "last_nonpadding_l2",
            "max_pixels": 256 * 32 * 32,
            "input_format": "qwen_reference_image_without_empty_text_v2",
        }
    )
    return manifest, fingerprint


class QwenEncoder:
    instruction = "Represent the user's input."

    def __init__(self, model_path: str | Path, device="cuda", *, check=lambda: None):
        check()
        self.check = check
        self._closed = False
        import torch
        from transformers import AutoProcessor, Qwen3VLModel
        from transformers.models.qwen3_vl.modeling_qwen3_vl import Qwen3VLPreTrainedModel

        class EmbeddingBackbone(Qwen3VLPreTrainedModel):
            # The embedding checkpoint stores model.* keys. HF's generative
            # checkpoint conversion rules must not remap this embedding model.
            _checkpoint_conversion_mapping = {}

            def __init__(self, config):
                super().__init__(config)
                self.model = Qwen3VLModel(config)
                self.post_init()

            def forward(self, **kwargs):
                return self.model(**kwargs)

        self.torch = torch
        self.lock = RLock()
        self.query_cache = VectorCache()
        self.path = Path(model_path)
        self.device = device
        self.manifest, self.fingerprint = embedding_identity(model_path, self.instruction)
        self.model = self.processor = None
        try:
            self.processor = AutoProcessor.from_pretrained(
                str(model_path), min_pixels=4 * 32 * 32, max_pixels=256 * 32 * 32, padding_side="left"
            )
            self.model, loading = EmbeddingBackbone.from_pretrained(
                str(model_path),
                torch_dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32,
                attn_implementation="sdpa",
                output_loading_info=True,
            )
            if loading["missing_keys"] or loading["unexpected_keys"] or loading.get("mismatched_keys"):
                raise ValueError("embedding checkpoint failed strict key validation; refusing random weights")
            self.loading_info = loading
            self.model = self.model.to(device).eval()
            check()
        except BaseException:
            self.close()
            raise

    def encode(self, *, text="", image=None, check=None):
        guard = check if check is not None else self.check
        guard()
        if self._closed:
            raise RuntimeError("encoder is closed")
        torch = self.torch
        content = []
        if image is not None:
            content.append({"type": "image", "image": image})
        if text:
            content.append({"type": "text", "text": text})
        elif image is None:
            content.append({"type": "text", "text": "NULL"})
        messages = [
            {"role": "system", "content": [{"type": "text", "text": self.instruction}]},
            {"role": "user", "content": content},
        ]
        prompt = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.processor(
            text=[prompt], images=[image] if image is not None else None, padding=True, return_tensors="pt"
        ).to(self.device)
        with torch.inference_mode():
            output = self.model(**inputs)
            mask = inputs["attention_mask"]
            indices = (torch.arange(mask.shape[1], device=self.device)[None, :] * mask).max(dim=1).values
            vector = output.last_hidden_state[torch.arange(len(mask), device=self.device), indices]
            vector = torch.nn.functional.normalize(vector.float(), dim=-1)
        result = vector[0].cpu().numpy()
        guard()
        return result

    def __call__(self, query: str):
        def guard():
            self.check()
            if self._closed:
                raise RuntimeError("encoder is closed")

        def compute():
            with self.lock:
                return self.encode(text=query, check=guard)

        return self.query_cache.get(self.fingerprint, query, compute, check=guard)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.query_cache.wait_idle()
        with self.lock:
            self.model = None
            self.query_cache.clear()
            gc.collect()
            if self.device.startswith("cuda"):
                self.torch.cuda.empty_cache()


def build_vectors(library: Library, encoder: QwenEncoder, *, view="joint"):
    if view not in {"joint", "image", "text"}:
        raise ValueError("embedding view must be joint, image or text")
    from PIL import Image

    vectors = []
    ids = []
    for i, segment in enumerate(library.segments.values()):
        image = Image.open(library.frame_path(segment.id)).convert("RGB") if segment.frame else None
        # Transcripts are source data. No generated captions or gold labels are inserted.
        vector = encoder.encode(
            text=segment.transcript if view != "image" else "", image=image if view != "text" else None
        )
        vectors.append(vector)
        ids.append(segment.id)
        if (i + 1) % 10 == 0:
            print(f"embedded {i+1}/{len(library.segments)}", flush=True)
    stem = "vectors" if view == "joint" else "vectors_" + view
    np.savez_compressed(library.root / (stem + ".npz"), ids=np.array(ids), vectors=np.stack(vectors))
    (library.root / (stem + ".json")).write_text(
        json.dumps(
            {
                "collection_hash": library.hash,
                "fingerprint": encoder.fingerprint,
                "model": encoder.manifest,
                "pooling": "last_nonpadding_l2",
                "instruction": encoder.instruction,
                "view": view,
                "rows": len(ids),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
