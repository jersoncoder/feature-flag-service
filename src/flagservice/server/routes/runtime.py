"""Runtime API: server-side evaluation and the config SDKs poll."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response, status

from flagservice.core.evaluate import evaluate
from flagservice.server import schemas
from flagservice.server.deps import SdkKey, ServerKey, StoreDep
from flagservice.server.etags import MalformedHeader, parse_tags, weak_match

router = APIRouter(tags=["runtime"])


@router.post("/v1/evaluate")
def evaluate_flag(
    body: schemas.EvaluateRequest, key: ServerKey, store: StoreDep
) -> schemas.EvaluateResponse:
    """Evaluate one flag in the server key's environment.

    Always 200: a missing or broken flag is reported through ``reason``
    (FLAG_NOT_FOUND or ERROR), the same way the SDK reports it.
    """
    assert key.environment_id is not None
    result = evaluate(store.flag_config(key.environment_id, body.flag), body.context)
    return schemas.EvaluateResponse(
        flag=body.flag,
        variant=result.variant,
        value=result.value,
        reason=result.reason,
        rule_index=result.rule_index,
        error=result.error,
    )


@router.get(
    "/v1/environments/{environment_id}/config",
    response_class=Response,
    responses={
        200: {
            "content": {"application/json": {}},
            "description": "The environment's flags and rules, with an ETag.",
        },
        304: {"description": "Unchanged since the ETag given in If-None-Match."},
    },
)
def environment_config(
    environment_id: str,
    key: SdkKey,
    store: StoreDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    """The full ruleset of the SDK key's environment, for local evaluation."""
    if key.environment_public_id != environment_id or key.environment_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="this key is for another environment")
    body, etag = store.environment_config(key.environment_id)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if if_none_match is not None:
        try:
            tags = parse_tags(if_none_match)
        except MalformedHeader:
            tags = []  # an unreadable validator just means a full response
        if weak_match(tags, etag):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
