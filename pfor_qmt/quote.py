def quote_plain(value):
    if type(value).__module__.startswith("pandas.") and type(value).__name__ in ("NAType", "NaTType"):
        return None
    if isinstance(value, dict):
        return {key: quote_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [quote_plain(item) for item in value]
    if hasattr(value, "tolist"):
        return quote_plain(value.tolist())
    if hasattr(value, "item"):
        return quote_plain(value.item())
    return value


def quote_records(value):
    if hasattr(value, "columns") and hasattr(value, "to_dict"):
        # DataFrame.values can round large integer IDs in a mixed float/int table.
        return quote_plain(value.to_dict("records"))
    names = getattr(getattr(value, "dtype", None), "names", None)
    if names:
        return [{name: quote_plain(row[name]) for name in names} for row in value]
    if isinstance(value, dict):
        return [quote_plain(value)] if value else []
    if isinstance(value, (list, tuple)):
        if not all(isinstance(row, dict) for row in value):
            raise ValueError("QMT quote rows must be dictionaries")
        return quote_plain(value)
    if value is None:
        return []
    raise ValueError("unsupported QMT quote data type: %s" % type(value).__name__)


def quote_callback_data(data):
    if not isinstance(data, dict):
        raise ValueError("QMT quote callback must be a stock-code dictionary")
    return {code: quote_records(value) for code, value in data.items()}

