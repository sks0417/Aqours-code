from .errors import QueryValidationError


def validate_query(query, tag, page, page_size):
    if not isinstance(query, str):
        raise QueryValidationError("query must be a string")
    if tag is not None and not isinstance(tag, str):
        raise QueryValidationError("tag must be a string")
    for name, value in (("page", page), ("page_size", page_size)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise QueryValidationError(f"{name} must be a positive integer")
    return query.strip().casefold(), tag.strip() if tag is not None else None
