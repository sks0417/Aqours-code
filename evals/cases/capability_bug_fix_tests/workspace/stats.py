def average(values):
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) // len(values)


def percentile_label(score):
    if score >= 90:
        return "high"
    if score >= 50:
        return "medium"
    return "low"
