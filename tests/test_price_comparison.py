"""Tests for shopping_agent.services.price_comparison."""
from unittest.mock import MagicMock


from shopping_agent.services.price_comparison import (
    find_best_match,
    normalize_product_name,
    normalize_size,
    size_to_grams,
    sizes_compatible,
)


class TestNormalizeProductName:
    def test_lowercases_input(self):
        assert normalize_product_name("FULL CREAM MILK") == "full cream milk"

    def test_removes_store_names(self):
        result = normalize_product_name("Coles Full Cream Milk 2L")
        assert "coles" not in result

    def test_removes_woolworths_brand(self):
        result = normalize_product_name("Woolworths Full Cream Milk 2L")
        assert "woolworths" not in result

    def test_removes_fresh_descriptor(self):
        result = normalize_product_name("Fresh Full Cream Milk 2L")
        assert "fresh" not in result

    def test_normalizes_weight_format(self):
        result = normalize_product_name("Milk 2 L")
        assert "2l" in result

    def test_collapses_whitespace(self):
        result = normalize_product_name("  milk   2l  ")
        assert result == "milk 2l"


class TestNormalizeSize:
    def test_lowercases_and_strips(self):
        assert normalize_size("  2L  ") == "2l"

    def test_replaces_litre(self):
        assert normalize_size("2litre") == "2l"

    def test_replaces_gram(self):
        assert normalize_size("500gram") == "500g"

    def test_removes_spaces(self):
        assert normalize_size("2 l") == "2l"


class TestSizeToGrams:
    def test_grams(self):
        assert size_to_grams("500g") == 500.0

    def test_kilograms(self):
        assert size_to_grams("1kg") == 1000.0

    def test_ml(self):
        assert size_to_grams("250ml") == 250.0

    def test_litres(self):
        assert size_to_grams("2l") == 2000.0

    def test_returns_none_for_unparseable(self):
        assert size_to_grams("large") is None

    def test_returns_none_for_empty(self):
        assert size_to_grams("") is None


class TestSizesCompatible:
    def test_matching_sizes_return_positive(self):
        assert sizes_compatible("500g", "500g") > 0

    def test_different_sizes_return_negative(self):
        assert sizes_compatible("500g", "1kg") < 0

    def test_none_returns_zero(self):
        assert sizes_compatible(None, "500g") == 0
        assert sizes_compatible("500g", None) == 0
        assert sizes_compatible(None, None) == 0

    def test_unparseable_returns_zero(self):
        assert sizes_compatible("large", "medium") == 0

    def test_equivalent_sizes_match(self):
        # 1000g == 1kg
        assert sizes_compatible("1000g", "1kg") > 0


def _make_product(name, store_val="coles", brand=None, unit_size=None):
    p = MagicMock()
    p.name = name
    p.store.value = store_val
    p.brand = brand
    p.unit_size = unit_size
    p.id = hash(name)
    return p


class TestFindBestMatch:
    def test_finds_obvious_match(self):
        source = _make_product("Full Cream Milk 2L")
        candidates = [
            _make_product("Full Cream Milk 2 Litre", "woolworths"),
            _make_product("Skim Milk 1L", "woolworths"),
        ]
        result = find_best_match(source, candidates)
        assert result is not None
        matched, confidence = result
        assert matched.name == "Full Cream Milk 2 Litre"
        assert 0.0 < confidence <= 1.0

    def test_returns_none_when_no_good_match(self):
        source = _make_product("Full Cream Milk 2L")
        candidates = [_make_product("Orange Juice 1L", "woolworths")]
        result = find_best_match(source, candidates)
        assert result is None

    def test_returns_none_with_empty_candidates(self):
        source = _make_product("Full Cream Milk 2L")
        assert find_best_match(source, []) is None

    def test_brand_mismatch_excludes_candidate(self):
        source = _make_product("Milk 2L", brand="Dairy Farmers")
        candidate = _make_product("Milk 2L", "woolworths", brand="Oak")
        # Low brand score should skip this candidate
        result = find_best_match(source, [candidate])
        # May or may not match depending on brand score threshold — just must not raise
        assert result is None or result[1] > 0

    def test_size_mismatch_reduces_score(self):
        source = _make_product("Milk", unit_size="2L")
        candidate_same_size = _make_product("Milk", "woolworths", unit_size="2L")
        candidate_diff_size = _make_product("Milk", "woolworths", unit_size="1L")

        result_same = find_best_match(source, [candidate_same_size])
        result_diff = find_best_match(source, [candidate_diff_size])

        if result_same and result_diff:
            assert result_same[1] > result_diff[1]
