"""Is a news item really about one of our instruments?

Some news feeds tag items with a bare ticker and no exchange: Alpaca's feed (fiscal.ai)
tags the Canadian «Questcorp Mining» (CSE: QQQ) as "QQQ", TELUS (TSX: T) as "T", Auxly
Cannabis (TSX: XLY) as "XLY". The tag alone cannot tell those apart from the Invesco QQQ
ETF, AT&T or the Consumer Discretionary SPDR in our catalog, so for such feeds an item
is kept for a symbol only when its text names the instrument: the distinctive start of
the catalog name ("Questcorp" is not "Invesco QQQ") or the ticker written in capitals.
"""

import re
import unicodedata

# words that say nothing about which company it is (legal forms, filler)
_FILLER = frozenset({
    "the", "a", "an", "and", "of", "&", "inc", "incorporated", "corp", "corporation",
    "co", "company", "companies", "plc", "ltd", "limited", "llc", "lp", "nv", "n", "v", "sa",
    "se", "ag", "spa", "holdings", "holding", "group", "class", "shares", "common", "stock",
    "trust", "etf", "fund", "ucits",
})
# a first word shared by many unrelated names (issuers, generic adjectives): the next word
# is needed too ("American Express", "iShares Russell", "State Street Consumer")
_GENERIC = frozenset({
    "american", "general", "international", "national", "united", "first", "global",
    "bank", "state", "new", "north", "south", "us", "u", "s", "royal", "standard",
    "invesco", "ishares", "spdr", "vanguard", "schwab", "proshares", "direxion",
    "wisdomtree", "xtrackers", "amundi", "lyxor", "select", "sector", "ark",
})

# Brands the press uses instead of the listed name (recall only: an item that says "Google"
# is about Alphabet). Words as _words() splits them.
_ALIASES: dict[str, tuple[tuple[str, ...], ...]] = {
    "GOOG": (("google",), ("youtube",), ("waymo",)),
    "GOOGL": (("google",), ("youtube",), ("waymo",)),
    "META": (("facebook",), ("instagram",), ("whatsapp",)),
    "AMZN": (("aws",),),
    "AAPL": (("iphone",),),
    "DIS": (("disney",),),
    "C": (("citi",), ("citibank",)),
    "ON": (("onsemi",),),
    "PDD": (("temu",),),
    "JPM": (("j", "p", "morgan"), ("chase",)),
    "PG": (("p&g",),),
    "JNJ": (("j&j",),),
    "LLY": (("lilly",),),
    "SCHW": (("schwab",),),
    "TTWO": (("rockstar",),),
    "CMCSA": (("nbcuniversal",), ("nbc",)),
    "GM": (("cadillac",), ("chevrolet",)),
    "RTX": (("raytheon",),),
}


def _words(text: str) -> list[str]:
    """Lowercase ASCII words: accents dropped ("Mondelēz" -> "mondelez"), punctuation,
    hyphens and apostrophes split ("Bristol-Myers" -> "bristol myers", "Meta's" -> "meta s")."""
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    )
    return re.findall(r"[a-z0-9&]+", ascii_text)


def name_key(name: str) -> tuple[str, ...]:
    """The distinctive start of an instrument name, as consecutive words: the first
    meaningful word ("Questcorp", "Amazon"), running on to the next meaningful one when the
    first is generic or very short ("Bank of America", "State Street", "GE Aerospace").
    () when nothing distinctive is left."""
    words = _words(name)
    while words and words[0] in _FILLER:
        words = words[1:]
    if not words:
        return ()
    key = [words[0]]
    if words[0] in _GENERIC or len(words[0]) <= 3:
        for w in words[1:]:
            if w in _FILLER and w not in ("of", "&"):
                break
            key.append(w)
            if w not in _FILLER:
                break
        while key[-1] in _FILLER:  # "ON Semiconductor" keeps both, "AT&T Inc." just one
            key.pop()
    return tuple(key)


def _has_phrase(words: list[str], key: tuple[str, ...]) -> bool:
    n = len(key)
    for i in range(len(words) - n + 1):
        if tuple(words[i:i + n]) == key:
            return True
    if n == 1:
        k = key[0]
        for i, w in enumerate(words):
            if len(k) >= 6 and w.startswith(k):  # "UnitedHealth" in "UnitedHealthcare"
                return True
            if i + 1 < len(words) and w + words[i + 1] == k:  # "Exxon Mobil" = "ExxonMobil"
                return True
    return False


def _ticker_forms(symbol: str) -> list[str]:
    base = symbol.split(".")[0]  # "EQQQ.DE" -> "EQQQ"
    forms = {base, base.replace("-", "."), base.replace("-", "/")}  # BRK-B, BRK.B, BRK/B
    return sorted(forms)


def _mentions_ticker(text: str, symbol: str) -> bool:
    for t in _ticker_forms(symbol):
        if len(t) == 1:
            # a one-letter word is everywhere ("Series T"): only an explicit ticker counts
            pattern = rf"(?:\$|\(|:\s?){re.escape(t)}\b"
        else:
            pattern = rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])"
        if re.search(pattern, text):  # case-sensitive: "QQQ", not "qqq"
            return True
    return False


def mentions(text: str, symbol: str, name: str | None) -> bool:
    """True when `text` names the instrument `symbol` (catalog `name`)."""
    if _mentions_ticker(text, symbol):
        return True
    keys = [k for k in (name_key(name or ""), *_ALIASES.get(symbol, ())) if k]
    if not keys:
        return False
    words = _words(text)
    return any(_has_phrase(words, k) for k in keys)
