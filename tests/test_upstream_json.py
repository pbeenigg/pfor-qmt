"""Selected regressions from pinned cfquant, MIT Copyright (c) 2026 tao."""
import numpy as np
import pandas as pd
import pytest
from pfor_qmt import protocol
BIG_ID = 2 ** 60 + 37

@pytest.fixture
def wire():
    return protocol

@pytest.mark.parametrize("value,expected", [
    (np.int8(12), 12), (np.int32(100), 100), (np.int64(BIG_ID), BIG_ID),
    (np.uint64(2 ** 64 - 1), 2 ** 64 - 1), (np.float16(1.5), 1.5),
    (np.float32(11.5), 11.5), (np.float64(11.6), 11.6),
    (np.longdouble("11.5"), 11.5), (np.bool_(True), True),
    (np.str_("000001.SZ"), "000001.SZ"), (np.array(100), 100),
])
def test_numpy_scalars_are_builtin_json_values_without_mutating_input(wire, value, expected):
    params = {"orders": [{"value": value}], "options": (value, None)}
    data = wire.loads_message(wire.pack_request("test.request", params))["params"]
    assert data["orders"][0]["value"] == expected
    assert type(data["orders"][0]["value"]) is type(expected)
    assert data["options"] == [expected, None]
    assert params["orders"][0]["value"] is value
    assert isinstance(params["options"], tuple)


def test_dataframe_cell_numbers_and_nested_keys(wire):
    frame = pd.DataFrame({"volume": pd.Series([100], dtype="int64"), "price": pd.Series([11.6], dtype="float32")})
    params = {"order_volume": frame.at[0, "volume"], "price": frame.at[0, "price"],
              "flags": {np.int64(1): np.bool_(False)}}
    data = wire.loads_message(wire.pack_request("xttrader.order_stock", params))["params"]
    assert type(data["order_volume"]) is int and data["order_volume"] == 100
    assert type(data["price"]) is float and data["price"] == float(frame.at[0, "price"])
    assert data["flags"] == {"1": False}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"),
                                   np.float32("nan"), np.float64("inf"), np.array(float("nan"))])
def test_non_finite_numbers_fail_with_field_path(wire, value):
    with pytest.raises(ValueError, match=r"message.params.orders\[0\].price.*NaN and infinity"):
        wire.pack_request("xttrader.order_stock", {"orders": [{"price": value}]})


@pytest.mark.parametrize("value", [pd.DataFrame({"price": [11.6]}), pd.Series([11.6]),
                                   np.array([11.6]), np.complex128(1 + 2j), pd.NA, pd.NaT,
                                   pd.Timestamp("2026-09-11"), object()])
def test_ambiguous_or_unsupported_types_are_not_stringified_or_unwrapped(wire, value):
    with pytest.raises(TypeError, match=r"message.params.price: unsupported JSON type"):
        wire.pack_request("xttrader.order_stock", {"price": value})


def test_circular_references_fail_but_shared_values_work(wire):
    row = {"volume": np.int64(100)}
    assert wire.loads_message(wire.pack_request("test", {"rows": [row, row]}))["params"]["rows"] == [{"volume": 100}] * 2
    row["self"] = row
    with pytest.raises(ValueError, match="circular reference"):
        wire.pack_request("test", {"rows": [row]})


def test_empty_dataframe_root_has_a_clear_type_error(wire):
    with pytest.raises(TypeError, match=r"message.params.*DataFrame"):
        wire.pack_request("test", pd.DataFrame())


def test_response_missing_values_keep_existing_null_behavior():
    raw = protocol.pack_response("offline", result={"price": np.nan, "missing": pd.NA, "id": np.int64(BIG_ID)})
    assert protocol.loads_message(raw)["result"] == {"price": None, "missing": None, "id": BIG_ID}

