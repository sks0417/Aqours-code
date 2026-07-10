from stats import average, percentile_label


def test_average_keeps_fractional_part():
    assert average([1, 2]) == 1.5


def test_average_empty_values_error():
    try:
        average([])
    except ValueError as exc:
        assert "empty" in str(exc) or "must not" in str(exc)
    else:
        raise AssertionError("average([]) should raise ValueError")


def test_percentile_label_boundaries():
    assert percentile_label(90) == "high"
    assert percentile_label(50) == "medium"
    assert percentile_label(49) == "low"
