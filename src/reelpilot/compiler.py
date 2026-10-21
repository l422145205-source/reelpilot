"""Compile retrieved evidence into a bounded edit IR, then independently verify it."""
from __future__ import annotations

from dataclasses import dataclass
import math

from .library import Library
from .retrieval import Retriever
from .schema import EditClip, EditPlan, QuerySpec


def overlaps(a: EditClip, b: EditClip) -> bool:
    return a.asset_id == b.asset_id and min(a.end, b.end) > max(a.start, b.start) + 0.001


@dataclass
class State:
    clips: tuple[EditClip, ...]
    covered: frozenset[str]
    duration: float
    utility: float


def compile_plan(library: Library, retriever: Retriever, spec: QuerySpec, *, mode="hybrid",
                 beam_width=128) -> EditPlan:
    if any(a not in library.assets for a in spec.asset_ids):
        raise ValueError("unknown requested asset")
    candidates = {}
    warnings = []
    per_clip = min(6.0, spec.budget_seconds/len(spec.goals))
    if per_clip < 1:
        raise ValueError("budget cannot support all requested goals")
    for goal in spec.goals:
        view = {"visual": "image", "dialogue": "text", "mixed": "joint"}[goal.channel]
        hits = retriever.search(goal.query, mode=mode, limit=12, asset_ids=spec.asset_ids, view=view)
        if not hits:
            raise ValueError(f"no retrieved evidence for goal {goal.id}")
        for rank, hit in enumerate(hits, 1):
            s = hit.segment
            duration = min(per_clip, s.end-s.start)
            # Times originate from the media index, never from generated model text.
            clip = EditClip(segment_id=s.id, asset_id=s.asset_id, start=s.start,
                            end=s.start+duration, goal_ids=[goal.id], relevance=1/rank)
            candidates[(goal.id, s.id)] = clip
        if mode != "lexical":
            warnings.append(f"{goal.id}: dense similarity is a ranking signal, not proof of visual correctness")
    states = [State((), frozenset(), 0, 0)]
    # Each goal is an obligation. The beam jointly handles overlap and the duration budget.
    # This is a bounded search heuristic; no global optimality guarantee is made.
    for goal in spec.goals:
        next_states = []
        options = [c for (g, _), c in candidates.items() if g == goal.id]
        for state in states:
            for clip in options:
                same = next((c for c in state.clips if c.segment_id == clip.segment_id), None)
                if same is not None:
                    merged = same.model_copy(update={"goal_ids": sorted(set(same.goal_ids+[goal.id]))})
                    clips = tuple(merged if c.segment_id == same.segment_id else c for c in state.clips)
                    next_states.append(State(clips, state.covered | {goal.id}, state.duration,
                                             state.utility+clip.relevance))
                elif (len(state.clips) < spec.max_clips and
                      state.duration+clip.end-clip.start <= spec.budget_seconds+1e-6 and
                      not any(overlaps(clip, c) for c in state.clips)):
                    next_states.append(State(state.clips+(clip,), state.covered | {goal.id},
                                             state.duration+clip.end-clip.start,
                                             state.utility+clip.relevance))
        if not next_states:
            raise ValueError(f"cannot cover goal {goal.id} under budget, clip-count and overlap constraints")
        next_states.sort(key=lambda x: (-len(x.covered), -x.utility, x.duration,
                                      tuple(c.segment_id for c in x.clips)))
        states = next_states[:beam_width]
    clips = list(states[0].clips)
    if spec.chronological:
        clips.sort(key=lambda c: (c.asset_id, c.start))
    plan = EditPlan(collection_hash=library.hash, spec=spec, clips=clips,
                    warnings=warnings, retrieval_mode=mode)
    verify_plan(library, plan)
    return plan


def verify_plan(library: Library, plan: EditPlan):
    if plan.collection_hash != library.hash:
        raise ValueError("stale plan: collection hash differs")
    if len(plan.clips) > plan.spec.max_clips or plan.duration > plan.spec.budget_seconds + 1e-6:
        raise ValueError("edit budget exceeded")
    goals = {g.id for g in plan.spec.goals}
    covered = set()
    for i, clip in enumerate(plan.clips):
        if clip.segment_id not in library.segments:
            raise ValueError("ungrounded segment ID")
        segment = library.segments[clip.segment_id]
        if clip.asset_id != segment.asset_id:
            raise ValueError("segment and asset disagree")
        if plan.spec.asset_ids and clip.asset_id not in plan.spec.asset_ids:
            raise ValueError("asset filter violated")
        if not (segment.start-1e-6 <= clip.start < clip.end <= segment.end+1e-6):
            raise ValueError("clip is outside its retrieved evidence interval")
        if not set(clip.goal_ids) <= goals:
            raise ValueError("unknown goal assigned to clip")
        visual_goals = {g.id for g in plan.spec.goals if g.channel != "dialogue"}
        if set(clip.goal_ids) & visual_goals and not segment.visual_usable:
            raise ValueError("visual evidence points to a blank frame window")
        covered.update(clip.goal_ids)
        if any(overlaps(clip, earlier) for earlier in plan.clips[:i]):
            raise ValueError("overlapping source intervals")
    if covered != goals:
        raise ValueError("uncovered goals")
    if plan.spec.chronological and plan.clips != sorted(plan.clips, key=lambda c: (c.asset_id, c.start)):
        raise ValueError("chronological order violated")
    return {"valid": True, "duration": plan.duration, "covered_goals": sorted(covered),
            "checks": ["collection", "budget", "provenance", "bounds", "overlap", "coverage", "order"]}

