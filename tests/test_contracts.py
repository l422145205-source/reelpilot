import json
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from reelpilot.compiler import compile_plan, verify_plan
from reelpilot.library import Library, within, save_library
from reelpilot.planner import checked_spec
from reelpilot.retrieval import Retriever
from reelpilot.schema import Brief, QuerySpec, Goal, EditClip, Segment, explicit_spec


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -3, 0, 10000])
def test_budget_rejects_nonfinite_and_unbounded_values(value):
    with pytest.raises(ValidationError):
        Brief(text="robot", budget_seconds=value)


@pytest.mark.parametrize("path", ["../private.txt", "../../etc/passwd", "/etc/passwd"])
def test_index_paths_cannot_escape_collection(tmp_path, path):
    with pytest.raises(ValueError):
        within(tmp_path, path)


def test_no_lexical_match_is_not_a_random_top_k(library):
    assert Retriever(library).search("unseenword", mode="lexical") == []


def test_missing_dense_model_never_silently_falls_back(library):
    with pytest.raises(RuntimeError, match="real index"):
        Retriever(library).search("robot", mode="hybrid")


def test_compiled_intervals_are_grounded_and_under_budget(library):
    spec = explicit_spec(Brief(text="forest;robot", budget_seconds=8))
    plan = compile_plan(library, Retriever(library), spec, mode="lexical")
    assert plan.duration <= 8
    assert {c.segment_id for c in plan.clips} == {"forest", "robot"}
    assert verify_plan(library, plan)["covered_goals"] == ["g1", "g2"]


@pytest.mark.parametrize("mutation", ["stale", "time", "asset", "coverage", "overlap", "budget"])
def test_independent_verifier_rejects_tampered_ir(library, mutation):
    plan = compile_plan(library, Retriever(library), explicit_spec(Brief(text="forest;robot", budget_seconds=8)), mode="lexical")
    if mutation == "stale":
        plan.collection_hash = "b"*64
    elif mutation == "time":
        plan.clips[0].end = 50
    elif mutation == "asset":
        plan.clips[0].asset_id = "other"
    elif mutation == "coverage":
        plan.clips = plan.clips[:1]
    elif mutation == "overlap":
        plan.clips[1] = plan.clips[0].model_copy(update={"goal_ids": ["g2"]})
    elif mutation == "budget":
        plan.spec.budget_seconds = 2
    with pytest.raises(ValueError):
        verify_plan(library, plan)


def test_goal_filter_is_enforced(library):
    spec = explicit_spec(Brief(text="robot", asset_ids=["unknown"]))
    with pytest.raises(ValueError, match="unknown requested asset"):
        compile_plan(library, Retriever(library), spec, mode="lexical")


def test_zero_evidence_cannot_be_declared_covered(library):
    with pytest.raises(ValueError, match="no retrieved evidence"):
        compile_plan(library, Retriever(library), explicit_spec(Brief(text="whale")), mode="lexical")


@pytest.mark.parametrize("raw", ["```json\n{}\n```", '{"queries":[]}', '{"queries":[1]}',
                                 '{"queries":["robot"],"shell":"rm -rf"}', '{"queries":"robot"}'])
def test_planner_malformed_or_extra_tool_fields_fail_closed(raw):
    with pytest.raises((ValueError, TypeError)):
        checked_spec(raw, Brief(text="robot"), "transformers")


def test_model_cannot_change_user_budget_or_create_source_times():
    spec = checked_spec('{"queries":["a city","a robot"]}', Brief(text="make a trailer", budget_seconds=8), "transformers")
    assert spec.budget_seconds == 8 and spec.planner == "transformers"
    assert "start" not in spec.model_dump()


def test_dense_embedding_fingerprint_is_required(library):
    np.savez_compressed(library.root/"vectors.npz", ids=np.array(list(library.segments)), vectors=np.eye(3))
    (library.root/"vectors.json").write_text(json.dumps({"collection_hash": library.hash, "fingerprint": "expected"}))
    class WrongEncoder:
        fingerprint = "different"
        def __call__(self, query):
            return np.ones(3)
    with pytest.raises(ValueError, match="fingerprint"):
        Retriever(library, WrongEncoder(), view="joint").search("robot", mode="dense")


def test_stale_vector_rows_are_rejected(library):
    np.savez_compressed(library.root/"vectors.npz", ids=np.array(["wrong"]), vectors=np.ones((1, 3)))
    (library.root/"vectors.json").write_text(json.dumps({"collection_hash": library.hash, "fingerprint": "test"}))
    with pytest.raises(ValueError, match="row order"):
        Retriever(library)


def test_explicit_goals_can_select_picture_and_dialogue_separately():
    spec = explicit_spec(Brief(text="visual: a city; dialogue: I have been alone"))
    assert [g.channel for g in spec.goals] == ["visual", "dialogue"]


def test_constrained_model_schema_preserves_channel_and_forbids_tool_fields():
    spec = checked_spec('{"queries":[{"query":"a city","channel":"visual"}]}',
                        Brief(text="city"), "transformers")
    assert spec.goals[0].channel == "visual"
    with pytest.raises(ValueError):
        checked_spec('{"queries":[{"query":"x","channel":"visual","shell":"x"}]}',
                     Brief(text="city"), "transformers")


def test_adaptive_route_uses_different_observed_vector_views(library):
    ids = np.array(list(library.segments))
    for view, vectors in [("image", np.eye(3)), ("text", np.roll(np.eye(3), 1, axis=0))]:
        np.savez_compressed(library.root/f"vectors_{view}.npz", ids=ids, vectors=vectors)
        (library.root/f"vectors_{view}.json").write_text(json.dumps(
            {"collection_hash": library.hash, "fingerprint": "same-embedding-space"}))
    class Encoder:
        fingerprint = "same-embedding-space"
        def __call__(self, query):
            return np.array([1., 0., 0.])
    retriever = Retriever(library, Encoder())
    assert retriever.search("unseen", mode="adaptive", view="image")[0].segment.id == "forest"
    assert retriever.search("unseen", mode="adaptive", view="text")[0].segment.id == "robot"


def test_independent_checker_rejects_blank_visual_evidence(library):
    plan = compile_plan(library, Retriever(library), explicit_spec(Brief(text="forest")), mode="lexical")
    library.segments["forest"].visual_usable = False
    with pytest.raises(ValueError, match="blank frame"):
        verify_plan(library, plan)

