from pluvial.api.app import _haversine_m


def test_haversine_zero_for_same_point():
    assert _haversine_m(29.76, -95.37, 29.76, -95.37) == 0


def test_haversine_known_distance():
    # Houston City Hall to the Toyota Center, roughly 1.3km apart.
    d = _haversine_m(29.760179, -95.3693747, 29.7508, -95.3621)
    assert 900 < d < 1700
