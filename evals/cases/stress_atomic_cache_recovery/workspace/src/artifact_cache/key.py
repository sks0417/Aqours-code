from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .models import BuildRequest, InvalidRequest


def cache_key(request: BuildRequest) -> str:
    if not isinstance(request, BuildRequest):
        raise InvalidRequest("request must be a BuildRequest")
    if not isinstance(request.inputs, Mapping) or not request.inputs:
        raise InvalidRequest("inputs must be a non-empty mapping")
    for name, content in request.inputs.items():
        if not isinstance(name, str) or not name:
            raise InvalidRequest("input names must be non-empty strings")
        if not isinstance(content, (bytes, str)):
            raise InvalidRequest("input content must be bytes or str")
    if not isinstance(request.namespace_version, str) or not request.namespace_version:
        raise InvalidRequest("namespace_version must be a non-empty string")

    # The compact identity was originally sufficient while the cache supported only
    # one tool configuration. It is retained here for compatibility with old keys.
    identity = (
        request.namespace_version,
        repr(dict(request.inputs)),
    )
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()

