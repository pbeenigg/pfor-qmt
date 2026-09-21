"""Domestic QMT identifiers, shared with the Python 3.6 terminal bridge."""
import re

EQUITY_MARKETS = ('SH', 'SZ', 'BJ')
OPTION_MARKETS = ('SHO', 'SZO')
FUTURE_MARKETS = ('IF', 'SF', 'DF', 'ZF', 'INE', 'GF')
MARKETS = EQUITY_MARKETS + OPTION_MARKETS + FUTURE_MARKETS
KINDS = ('future', 'option', 'stock', 'index', 'fund', 'bond')


def normalize_code(value):
    if not isinstance(value, str) or '.' not in value:
        raise ValueError('Invalid QMT instrument code')
    body, market = value.strip().rsplit('.', 1)
    market = market.upper()
    pattern = r'\d{6}' if market in EQUITY_MARKETS else r'\d{8}' if market in OPTION_MARKETS else r'[A-Za-z0-9_][A-Za-z0-9_ &()\-]{0,79}' if market in FUTURE_MARKETS else None
    if pattern is None or re.fullmatch(pattern, body) is None or body != body.strip():
        raise ValueError('Invalid domestic QMT instrument code')
    return body + '.' + market


def market_of(code):
    return normalize_code(code).rsplit('.', 1)[1]


def derivative_kind(code):
    body, market = normalize_code(code).rsplit('.', 1)
    if market in OPTION_MARKETS:
        return 'option'
    if market in FUTURE_MARKETS:
        # Exchange contract grammar, including option legs inside a combination.
        return 'option' if re.search(r'\d-?[CP]-?\d', body) else 'future'
    return None


def contract_type(code):
    body, market = normalize_code(code).rsplit('.', 1)
    if market not in FUTURE_MARKETS:
        return 'contract'
    if '&' in body or ' ' in body:
        return 'combination'
    if derivative_kind(code) == 'option':
        return 'contract'
    if '(EFP)' in body:
        return 'efp'
    if re.search(r'L\d{1,2}$', body) or re.search(r'(?<!\d)\d{2,3}$', body) and market != 'ZF':
        return 'continuous'
    if re.search(r'(?:00|001|L[019])$', body) or re.fullmatch(r'[A-Za-z_]+\d{2}', body):
        return 'continuous'
    return 'contract'
