from pluvial.mireye.fields import is_soil_usable


def test_urban_land_dominant_is_unusable():
    assert is_soil_usable({"soil_map_unit_name": {"value": "Urban land, till substratum, 0 to 3 percent slopes"}}) is False
    assert is_soil_usable({"soil_map_unit_name": {"value": "Urban land-Greenbelt complex"}}) is False


def test_natural_soil_is_usable():
    assert is_soil_usable({"soil_map_unit_name": {"value": "Lake Charles clay, 0 to 1 percent slopes"}}) is True
    assert is_soil_usable({"soil_map_unit_name": {"value": "Bernard clay loam, 0 to 1 percent slopes"}}) is True


def test_missing_field_is_unusable_not_assumed_true():
    assert is_soil_usable({}) is False
    assert is_soil_usable({"soil_map_unit_name": {"value": None}}) is False


def test_bare_string_values_also_handled():
    # is_soil_usable should tolerate a raw string value, not only {value: ...}
    assert is_soil_usable({"soil_map_unit_name": "Urban land"}) is False
    assert is_soil_usable({"soil_map_unit_name": "Trinity clay, 0 to 1 percent slopes"}) is True
