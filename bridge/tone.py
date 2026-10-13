"""Reply tone for the pod face: classify_tone(sentence) -> "neutral" | "happy" | "sad".

Ported from the Skwoop decider keyword control (skwoop-decider/src/control.py, 47 entries, frozen by
its LOCK.md; the same table is skw_reply_tone() in c/skw_decider.c). It beat the learned model on
every eval set. Rule: any sad keyword -> sad; else any happy keyword -> happy; else neutral.
Keywords match at a word start ("fail" matches "failed", "red" does not match "delivered").

Two small additions, both deterministic:
  * Negation: a keyword preceded by a negator ("no", "not", "never", "without", "zero", ...)
    within the previous 3 words of the same clause does not count. "No errors, all tests passed."
    -> happy; "No errors this time." -> neutral (under-reacting is the safe failure);
    "No, the deploy failed." -> sad (the comma ends the negator's clause).
    With no negator in the sentence the result is identical to the decider's table.
  * Curly apostrophes (’) are read as straight ones, so "couldn’t" matches "couldn't".

Standard library only; no model, no network.
"""
from __future__ import annotations

import re

NEUTRAL, HAPPY, SAD = "neutral", "happy", "sad"

# --- the decider keyword table, verbatim (src/control.py) ---
SAD_WORDS = ["fail", "error", "sorry", "couldn't", "could not", "can't", "cannot", "unable", "down", "broke",
             "crash", "problem", "unfortunately", "lost", "timeout", "timed out", "offline", "missed", "apolog",
             "wrong", "not working", "didn't", "bad news", "stuck", "red"]
HAPPY_WORDS = ["done", "success", "great", "good", "passed", "finished", "ready", "all set", "nice", "awesome",
               "congrat", "green", "fixed", "complete", "perfect", "saved", "delivered", "yay", "glad", "happy",
               "working again", "back up"]

# --- negation (bridge addition) ---
NEGATORS = frozenset({"no", "not", "never", "without", "zero", "nothing", "none", "nobody",
                      "isn't", "wasn't", "aren't", "weren't", "don't", "doesn't",
                      "hasn't", "haven't", "hadn't", "won't"})
NEGATION_WINDOW = 3          # words before the keyword, same clause
_CLAUSE = "|"                # marks . , ; : ! ? (a negator never reaches across one)
_CLAUSE_PUNCT = re.compile(r"[.,;:!?]")
_NON_WORD = re.compile(r"[^a-z' ]+")


def _normalise(text: str) -> tuple[str, str]:
    """(t, t2): t exactly as the decider builds it; t2 the same with clause breaks kept as '|'."""
    low = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    t = " " + _NON_WORD.sub(" ", low) + " "
    t2 = " " + _NON_WORD.sub(lambda m: f" {_CLAUSE} " if _CLAUSE_PUNCT.search(m.group()) else " ", low) + " "
    return t, t2


def _negated(t2: str, idx: int) -> bool:
    before = t2[:idx].split()
    if _CLAUSE in before:
        before = before[len(before) - before[::-1].index(_CLAUSE):]
    return any(w in NEGATORS for w in before[-NEGATION_WINDOW:])


def _hit(t: str, t2: str, kw: str) -> bool:
    """True if kw occurs (word-start match) at least once without a negator in front of it."""
    pat = " " + kw
    if pat not in t:
        return False
    idx, seen = t2.find(pat), False
    while idx != -1:
        seen = True
        if not _negated(t2, idx):
            return True
        idx = t2.find(pat, idx + 1)
    return not seen  # only matched across punctuation (e.g. "timed-out"): same as the decider


def classify_tone(sentence: str) -> str:
    """'sad' if any un-negated sad keyword, else 'happy' if any un-negated happy keyword, else 'neutral'."""
    if not sentence:
        return NEUTRAL
    t, t2 = _normalise(sentence)
    if any(_hit(t, t2, k) for k in SAD_WORDS):
        return SAD
    if any(_hit(t, t2, k) for k in HAPPY_WORDS):
        return HAPPY
    return NEUTRAL


if __name__ == "__main__":  # python tone.py "The deploy failed."
    import sys
    for line in sys.argv[1:] or [sys.stdin.read()]:
        print(classify_tone(line), "|", line.strip())
