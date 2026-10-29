"""One request implementation shared by thread and isolated-process backends."""

from contextlib import contextmanager

from .pipeline import run_job
from .retrieval import Retriever
from .schema import QuerySpec, explicit_spec


@contextmanager
def checked_lock(lock, check):
    if hasattr(lock, "lease"):
        with lock.lease(check):
            yield
        return
    if lock is None:
        check()
        yield
        return
    while not lock.acquire(timeout=0.05):
        check()
    try:
        check()
        yield
    finally:
        lock.release()


def run_request(
    ticket,
    body,
    library,
    *,
    encoder_path=None,
    planner_path=None,
    ollama_model="qwen3:4b",
    device="cuda",
    retriever_factory=None,
    model_lock=None,
    resources=None,
    pipeline=run_job
):
    journal = ticket.admission.journal
    ticket.phase("planning")
    with ticket.span('planning'):
        spec = resolve_spec(ticket, body, journal, planner_path=planner_path,
                            device=device, resources=resources, model_lock=model_lock,
                            ollama_model=ollama_model)
    ticket.guard()
    ticket.phase("executing")
    encoder = None
    try:
        with ticket.span('retriever_setup'):
            if retriever_factory is not None:
                retriever = retriever_factory(body.mode, check=ticket.guard)
            else:
                if body.mode != "lexical":
                    if not encoder_path:
                        raise ValueError("no query encoding model configured")
                    from .encoders import QwenEncoder

                    encoder = QwenEncoder(encoder_path, device=device, check=ticket.guard)
                retriever = Retriever(library, encoder)
        return pipeline(
            library, retriever, journal, ticket.job, spec, mode=body.mode, request_owner=ticket.owner
        )
    finally:
        if encoder is not None:
            encoder.close()


def resolve_spec(ticket, body, journal, *, planner_path, device, resources, model_lock, ollama_model):
    try:
        resolved = journal.get(ticket.job)["spec"]["spec"]
    except KeyError:
        resolved = None
    if resolved is not None:
        spec = QuerySpec.model_validate(resolved)
    elif body.provider == "transformers":
        if not planner_path:
            raise ValueError("no local planning model configured")
        from .planner import transformers_spec

        if resources is not None:
            spec = resources.plan(body.brief, check=ticket.guard)
        else:
            with checked_lock(model_lock, ticket.guard):
                spec = transformers_spec(body.brief, planner_path, device=device, check=ticket.guard)
    elif body.provider == "ollama":
        from .planner import ollama_spec

        with checked_lock(model_lock, ticket.guard):
            spec = ollama_spec(body.brief, ollama_model)
    else:
        spec = explicit_spec(body.brief)
    return spec
