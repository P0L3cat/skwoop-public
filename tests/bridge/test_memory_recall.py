"""memory_recall: the daemon's JSON is parsed once, at the edge; the model only ever sees facts_text."""
from __future__ import annotations

import pytest

from memory.memory_recall import MAX_FACTS, MemoryFact, RecallError, facts_text, parse_recall


def test_parse_keeps_text_results_in_order_and_skips_junk():
    raw = {"results": [{"type": "observation", "text": "Married to  Mel."}, {"type": "world"}, "junk",
                       {"text": "Mel is Alex's wife.", "id": 3}], "entities": {}}
    assert parse_recall(raw) == (MemoryFact("observation", "Married to Mel."), MemoryFact("", "Mel is Alex's wife."))


@pytest.mark.parametrize("raw", [None, [], {"results": None}, {"answer": "Mel"}])
def test_parse_rejects_what_is_not_a_recall_result(raw):
    with pytest.raises(RecallError):
        parse_recall(raw)


def test_facts_text_dedupes_caps_and_says_when_empty():
    facts = tuple(MemoryFact("world", f"fact {i % 8}") for i in range(20))
    text = facts_text(facts)
    assert text.count("\n- ") == MAX_FACTS and text.count("fact 0") == 1
    assert facts_text(()) == "Skwoop's memory has nothing on this."
