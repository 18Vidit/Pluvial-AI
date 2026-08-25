import pytest

from pluvial.mireye.profile_job import extract_batch_result, select_stratified_segments


def test_extract_batch_result_normalizes_source():
    result = {"ok": True, "fields": {"elevation": {"value": 12.3, "source": "USGS"}}}
    assert extract_batch_result(result) == {"elevation": {"value": 12.3, "source": "USGS"}}


def test_extract_batch_result_handles_bare_values():
    result = {"ok": True, "fields": {"elevation": 12.3}}
    assert extract_batch_result(result) == {"elevation": {"value": 12.3, "source": None}}


def test_extract_batch_result_not_ok_is_empty():
    assert extract_batch_result({"ok": False}) == {}


def test_stratified_selection_is_deterministic_across_calls(tmp_path):
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    con.execute(
        "CREATE TABLE street_segments (segment_id BIGINT, name VARCHAR, highway_class VARCHAR, geom GEOMETRY)"
    )
    con.execute("CREATE TABLE complaints (case_number VARCHAR, segment_id BIGINT)")
    # Two segments tied on complaint count — the tiebreaker must make
    # selection order stable across repeated calls (idempotency-key safety).
    con.execute(
        "INSERT INTO street_segments VALUES "
        "(2, 'B', 'residential', ST_GeomFromText('POINT(-95.3 29.7)')), "
        "(1, 'A', 'residential', ST_GeomFromText('POINT(-95.4 29.8)'))"
    )
    con.execute("INSERT INTO complaints VALUES ('c1', 1), ('c2', 2)")

    first = select_stratified_segments(con, n_target=2)
    second = select_stratified_segments(con, n_target=2)
    assert first == second
    assert [s[0] for s in first] == [1, 2]


# --- failed batch locations ---------------------------------------------------

def test_a_refused_location_raises_in_strict_mode():
    """The silent-{} behaviour this replaces was found live: when the account's
    monthly allowance ran out mid-traversal, two dozen cells were written as
    ground with no soil data — which is a false statement about the world,
    not a missing one."""
    from pluvial.mireye.profile_job import BatchLocationFailed, extract_batch_result

    refused = {
        "index": 0,
        "ok": False,
        "error": {"error": "credits_exhausted", "message": "Monthly credit allowance exhausted"},
    }
    with pytest.raises(BatchLocationFailed) as caught:
        extract_batch_result(refused, strict=True)
    assert caught.value.code == "credits_exhausted"


def test_the_bulk_profiler_still_skips_rather_than_aborting():
    """One unresolvable location in a 1,000-segment ETL run must not take the
    job down; its absence shows up as an unprofiled segment."""
    from pluvial.mireye.profile_job import extract_batch_result

    assert extract_batch_result({"ok": False, "error": {"error": "no_data"}}) == {}


def test_a_successful_location_is_unaffected_by_strict():
    from pluvial.mireye.profile_job import extract_batch_result

    ok = {"ok": True, "fields": {"elevation": {"value": 12.5, "source": "USGS"}}}
    assert extract_batch_result(ok, strict=True) == {"elevation": {"value": 12.5, "source": "USGS"}}
