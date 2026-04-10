"""
Unit tests for the data adapter bridge layer.

Tests that load_district_data() returns the correct format expected
by all ported routers (statistics, dashboard, factors, export, etc.).
"""
import pytest


@pytest.mark.anyio
async def test_data_adapter_returns_dict(client):
    """Data adapter should load data from DB and return a dict keyed by dcode."""
    from services.data_adapter import load_district_data
    data = load_district_data()
    assert isinstance(data, dict)


@pytest.mark.anyio
async def test_data_adapter_district_format(client):
    """Each district entry should have the required fields."""
    from services.data_adapter import load_district_data
    data = load_district_data()
    if not data:
        pytest.skip("No district data available")

    dcode, district = next(iter(data.items()))
    assert isinstance(dcode, str)
    assert "name_th" in district
    assert "total_screened" in district
    assert "diseases" in district
    assert isinstance(district["diseases"], dict)


@pytest.mark.anyio
async def test_data_adapter_disease_format(client):
    """Each disease entry should have name, pct_at_risk, and indicators."""
    from services.data_adapter import load_district_data
    data = load_district_data()
    if not data:
        pytest.skip("No district data available")

    district = next(iter(data.values()))
    for disease_key, disease in district["diseases"].items():
        assert "name" in disease, f"Missing 'name' in {disease_key}"
        assert "name_en" in disease, f"Missing 'name_en' in {disease_key}"
        assert "pct_at_risk" in disease, f"Missing 'pct_at_risk' in {disease_key}"
        assert "total_screened" in disease, f"Missing 'total_screened' in {disease_key}"
        assert "indicators" in disease, f"Missing 'indicators' in {disease_key}"
        assert isinstance(disease["indicators"], dict)


@pytest.mark.anyio
async def test_data_adapter_indicator_format(client):
    """Indicators should have label, unit, bar_max, zones."""
    from services.data_adapter import load_district_data
    data = load_district_data()
    if not data:
        pytest.skip("No district data available")

    district = next(iter(data.values()))
    for disease_key, disease in district["diseases"].items():
        for ind_key, indicator in disease["indicators"].items():
            assert "label" in indicator, f"Missing 'label' in {disease_key}.{ind_key}"
            assert "unit" in indicator, f"Missing 'unit' in {disease_key}.{ind_key}"
            assert "bar_max" in indicator, f"Missing 'bar_max' in {disease_key}.{ind_key}"
            assert "zones" in indicator, f"Missing 'zones' in {disease_key}.{ind_key}"
            assert isinstance(indicator["zones"], list)


@pytest.mark.anyio
async def test_data_adapter_k_anonymity(client):
    """Districts with fewer than 5 screened should be excluded."""
    from services.data_adapter import load_district_data
    data = load_district_data()
    for dcode, district in data.items():
        assert district.get("total_screened", 0) >= 5, \
            f"District {dcode} has {district.get('total_screened')} screened (below k=5)"


@pytest.mark.anyio
async def test_data_adapter_disease_keys(client):
    """All expected diseases should be present."""
    from services.data_adapter import load_district_data
    data = load_district_data()
    if not data:
        pytest.skip("No district data available")

    expected = {"diabetes", "hypertension", "cardiovascular", "obesity", "dyslipidemia", "stroke", "ckd", "anemia"}
    district = next(iter(data.values()))
    actual = set(district["diseases"].keys())
    for key in expected:
        assert key in actual, f"Missing disease key: {key}"


@pytest.mark.anyio
async def test_data_adapter_cache(client):
    """Calling load_district_data twice should return same object (cached)."""
    from services.data_adapter import load_district_data
    d1 = load_district_data()
    d2 = load_district_data()
    assert d1 is d2, "Expected cached result"


@pytest.mark.anyio
async def test_data_adapter_invalidate(client):
    """After invalidation, cache should be cleared."""
    from services.data_adapter import load_district_data, invalidate_cache
    d1 = load_district_data()
    invalidate_cache()
    d2 = load_district_data()
    assert d1 is not d2, "Expected fresh result after invalidation"
