"""Wallet tokens versus underlying shares; persisted quantities stay in token units."""
from decimal import Decimal, localcontext


def ratio(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError('token_per_share must be positive and finite')
    return result


def pool_ratio(pool):
    exact = pool.get('token_per_share_exact')
    return ratio(exact if exact is not None else pool.get('token_per_share', 1))


def tokens_to_shares(tokens, conversion):
    with localcontext() as context:
        context.prec = 60
        return Decimal(str(tokens)) / ratio(conversion)


def shares_to_tokens(shares, conversion):
    with localcontext() as context:
        context.prec = 60
        return Decimal(str(shares)) * ratio(conversion)
