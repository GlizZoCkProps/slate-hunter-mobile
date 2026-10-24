import re
import unicodedata

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def player_key(name):
    """Normalize a player name so 'Jaren Jackson Jr.' == 'Jaren Jackson'.

    Lowercase, strip accents, punctuation and generational suffixes.
    """
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    parts = re.sub(r"[^a-z0-9 ]", " ", text.lower().replace(".", "").replace("'", "")).split()
    while parts and parts[-1] in _SUFFIXES:
        parts.pop()
    return "".join(parts)


def team_key(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def implied_prob(american):
    """Raw implied probability (with vig) of an American price."""
    if american is None:
        return None
    p = float(american)
    if p == 0:
        return None
    return (-p) / (-p + 100.0) if p < 0 else 100.0 / (p + 100.0)


def no_vig_pair(price_a, price_b):
    """Normalize a two-way market to remove vig. Returns (pa, pb) or (None, None)."""
    a, b = implied_prob(price_a), implied_prob(price_b)
    if a is None or b is None or a + b <= 0:
        return None, None
    return a / (a + b), b / (a + b)


def profit_units(american, stake=1.0):
    """Profit on a winning bet at an American price."""
    p = float(american)
    return stake * (p / 100.0) if p > 0 else stake * (100.0 / -p)
