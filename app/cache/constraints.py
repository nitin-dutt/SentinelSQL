"""
Question "constraints" for semantic-cache safety (alignment_plan CAC-01).

Embedding similarity measures topic, not meaning-in-detail. "Top 5 customers by revenue" and
"Top 10 customers by revenue", or "orders in 2024" and "orders in 2025", embed almost identically
yet need different SQL. A semantic (L2) cache hit is therefore only allowed when both questions
agree on every *constraint*: the tokens that change which rows or which answer is wanted.

Constraints extracted:
  * numbers, including number words ("five" -> 5), years and day numbers;
  * ranking / comparison / negation / aggregation / time-window words (top, more, not, average,
    last, before, ...);
  * month names and quarters;
  * quoted strings and capitalised names mid-sentence (named entities such as "Alice", "North").

Two questions that differ in any of these are treated as different questions. This trades some
hit rate for correctness: a miss costs one model call, a wrong hit returns another question's data.
"""

import re
from typing import FrozenSet

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "dozen": "12", "fifteen": "15",
    "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "hundred": "100", "thousand": "1000",
    "million": "1000000",
}

_MARKER_WORDS = {
    # ranking / extremes
    "top", "bottom", "highest", "lowest", "most", "least", "best", "worst", "biggest", "smallest",
    "largest", "greatest", "maximum", "minimum", "max", "min", "oldest", "newest", "latest", "earliest",
    # comparison / ranges
    "above", "below", "over", "under", "more", "less", "greater", "fewer", "between", "exactly", "only",
    # negation / exclusion
    "not", "no", "without", "except", "excluding", "never",
    # ordering in time
    "before", "after", "since", "until", "first", "last", "previous", "prior", "next", "current", "this",
    "today", "yesterday", "tomorrow", "ytd", "mtd", "qtd", "weekly", "monthly", "yearly", "daily", "quarterly",
    # aggregation
    "average", "avg", "mean", "median", "total", "sum", "count", "distinct", "unique",
}

_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-z]+")
_QUARTER_RE = re.compile(r"\bq[1-4]\b")
_QUOTED_RE = re.compile(r"[\"']([^\"']{1,80})[\"']")
_CAPITALISED_RE = re.compile(r"\b[A-Z][A-Za-z0-9_'-]*")


def extract_constraints(question: str) -> FrozenSet[str]:
    """The set of constraint tokens in a question (lower-case, comparable by equality)."""
    if not question:
        return frozenset()
    found = set()
    lowered = question.lower()

    found.update(_NUMBER_RE.findall(lowered))
    found.update(_QUARTER_RE.findall(lowered))
    for word in _WORD_RE.findall(lowered):
        if word in _NUMBER_WORDS:
            found.add(_NUMBER_WORDS[word])
        elif word in _MARKER_WORDS or word in _MONTHS:
            found.add(word)

    for quoted in _QUOTED_RE.findall(question):
        found.update(f"q:{w}" for w in _WORD_RE.findall(quoted.lower()))

    # Capitalised words after the first word: names, places, codes ("North", "USA", "Alice").
    for match in _CAPITALISED_RE.finditer(question):
        if match.start() == 0 or question[:match.start()].strip() == "":
            continue
        word = match.group(0).lower().strip("'-")
        if word not in _MARKER_WORDS and word not in _NUMBER_WORDS and word not in _MONTHS:
            found.add(f"e:{word}")
    return frozenset(found)
