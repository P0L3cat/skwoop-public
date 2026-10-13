"""notes_pick: the notes search the bridge does before a deep notes read, over real files in a temp folder."""
from __future__ import annotations

from pathlib import Path

import pytest

import notes_pick


def _scope(tmp_path: Path) -> tuple[notes_pick.NoteFile, ...]:
    wiki = tmp_path / "wiki"
    (wiki / "home").mkdir(parents=True)
    (wiki / "index.md").write_text("# Index\n- [Appliances](home/appliances.md)\n")
    (wiki / "home" / "appliances.md").write_text("# Appliances\n\nThe dishwasher is a Bosch 500, bought 2024.\n"
                                                 "Warranty: two years.\n")
    asap = tmp_path / "ASAP.txt"
    asap.write_text("- Dish disposal pods\n- call the plumber\n")
    grocery = tmp_path / "grocery.txt"
    grocery.write_text("milk\n")

    return notes_pick.read_scope(wiki, (asap, grocery))


@pytest.mark.parametrize(("said", "terms"), [
    ("What's in my notes about the dishwasher?", ("dishwasher",)),
    ("Find the dishwasher warranty", ("dishwasher", "warranty")),
    ("Do I have notes on Mel's birthday?", ("mel", "birthday")),
    ("What's in my notes?", ()),
])
def test_search_terms_drop_the_ask_words(said, terms):
    assert notes_pick.search_terms(said) == terms


@pytest.mark.parametrize("said", ["Write a note about the dishwasher.", "Add a note that the pods ran out",
                                  "Make a note about the plumber"])
def test_writes_get_no_prepick(said, tmp_path):
    assert notes_pick.facts(said, _scope(tmp_path)) is None


def test_a_hit_names_the_page_and_its_lines(tmp_path):
    facts = notes_pick.facts("What's in my notes about the dishwasher?", _scope(tmp_path))
    assert facts is not None
    assert "whole notes scope (2 family wiki pages" in facts[0] and "Matches in: family wiki: Appliances" in facts[0]
    want = "family wiki: Appliances (home/appliances.md) (lines): The dishwasher is a Bosch 500, bought 2024."
    assert facts[1] == want
    assert facts[2].endswith("ASAP.txt (close match only, lines): - Dish disposal pods")


def test_no_hit_says_so_for_the_whole_scope(tmp_path):
    facts = notes_pick.facts("What's in my notes about the furnace?", _scope(tmp_path))
    assert facts == [facts[0]] and "No note mentions it." in facts[0]


def test_plural_and_singular_both_match(tmp_path):
    facts = notes_pick.facts("Any notes on warranties?", _scope(tmp_path))
    assert facts is not None and "Matches in: family wiki: Appliances" in facts[0]


def test_facts_fit_the_deep_facts_blob(tmp_path):
    facts = notes_pick.facts("notes about the dishwasher", _scope(tmp_path))
    assert facts is not None and len(facts) <= notes_pick.MAX_FACTS
    assert all(len(f) <= notes_pick.FACT_CHARS for f in facts)
