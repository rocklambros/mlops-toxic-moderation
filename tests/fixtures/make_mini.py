"""Deterministic builder for the mini Jigsaw fixture. Run to regenerate the CSV.

Usage: python tests/fixtures/make_mini.py

Sizing is the point. The previous fixture gave three labels exactly 6 positives, so the
15% test split took one each and left 5 for 5 folds -- and `insult` failed the
every-label-in-every-fold assertion at seed 7. Every label here carries >= 12 positives
after dedup, which leaves >= 2 per validation fold, so the split tests pass across seeds
instead of passing at one lucky seed.
"""

import csv
from pathlib import Path

from model.labels import LABELS

BASE: list[tuple[str, set[str]]] = [
    ("have a nice day friend", set()),
    ("thanks for the thoughtful edit", set()),
    ("i disagree but respect your point", set()),
    ("great work on the article", set()),
    ("the weather is lovely today", set()),
    ("please cite a source for that claim", set()),
    ("i reverted the vandalism on that page", set()),
    ("welcome to wikipedia enjoy editing", set()),
    ("could you explain the third paragraph", set()),
    ("the citation format needs fixing", set()),
    ("happy to help with the translation", set()),
    ("this article needs more references", set()),
    ("moved the section for readability", set()),
    ("nice catch on that typo", set()),
    ("let us discuss this on the talk page", set()),
    ("the infobox image is too large", set()),
    ("you are an idiot", {"toxic", "insult"}),
    ("what a moron you are", {"toxic", "insult"}),
    ("shut up you fool", {"toxic", "insult"}),
    ("you clueless clown", {"toxic", "insult"}),
    ("absolute imbecile", {"toxic", "insult"}),
    ("you stupid loser", {"toxic", "insult"}),
    ("such a pathetic troll", {"toxic", "insult"}),
    ("get lost you halfwit", {"toxic", "insult"}),
    ("nobody cares dimwit", {"toxic", "insult"}),
    ("you write like a buffoon", {"toxic", "insult"}),
    ("what an ignorant fool", {"toxic", "insult"}),
    ("you vile disgusting worthless scum", {"toxic", "severe_toxic", "insult"}),
    ("f*ck this garbage", {"toxic", "obscene"}),
    ("this is sh!t", {"toxic", "obscene"}),
    ("what the hell is this crap", {"toxic", "obscene"}),
    ("total bullsh1t", {"toxic", "obscene"}),
    ("piece of cr@p edit", {"toxic", "obscene"}),
    ("obscene filth everywhere", {"toxic", "obscene"}),
    ("damn this stupid page", {"toxic", "obscene", "insult"}),
    ("go to hell already", {"toxic", "obscene"}),
    ("a**hole move right there", {"toxic", "obscene"}),
    ("screw your dumb rules", {"toxic", "obscene"}),
    ("filthy rotten trash article", {"toxic", "obscene"}),
    ("utter garbage sh!te", {"toxic", "obscene", "severe_toxic"}),
    ("i will kill you", {"toxic", "threat", "severe_toxic"}),
    ("i am going to hurt you", {"toxic", "threat"}),
    ("watch your back i am coming", {"toxic", "threat"}),
    ("you will pay with your life", {"toxic", "threat", "severe_toxic"}),
    ("i will find you and end you", {"toxic", "threat", "severe_toxic"}),
    ("death to you and yours", {"toxic", "threat", "severe_toxic"}),
    ("stop or i will end this now", {"toxic", "threat"}),
    ("i know where you live", {"toxic", "threat"}),
    ("expect a visit from me soon", {"toxic", "threat"}),
    ("you are a dead man walking", {"toxic", "threat", "severe_toxic"}),
    ("i will burn your house down", {"toxic", "threat", "severe_toxic"}),
    ("your family will regret this", {"toxic", "threat"}),
    ("people of that group are subhuman", {"toxic", "identity_hate", "severe_toxic"}),
    ("i hate everyone of your race", {"toxic", "identity_hate"}),
    ("your religion makes you worthless", {"toxic", "identity_hate"}),
    ("go back to where you came from", {"toxic", "identity_hate"}),
    ("your kind does not belong here", {"toxic", "identity_hate"}),
    ("slur against your ethnicity", {"toxic", "identity_hate", "severe_toxic"}),
    ("we should ban all of your people", {"toxic", "identity_hate"}),
    ("that nationality ruins everything", {"toxic", "identity_hate"}),
    ("your accent proves you are inferior", {"toxic", "identity_hate", "severe_toxic"}),
    ("no one of your faith is welcome", {"toxic", "identity_hate"}),
    ("deport every last one of them", {"toxic", "identity_hate"}),
    ("your gender makes you useless here", {"toxic", "identity_hate", "severe_toxic"}),
]

# Four planted rows, each exercising a different branch:
#   1. exact duplicate of c016
#   2. case + whitespace variant -> exact after normalization
#   3. TRUE near-duplicate at Jaccard 12/13 = 0.923 -> only the LSH-plus-exact branch
#      collapses it, which is the branch v1's fixture never reached
#   4. exact-normalized duplicate of c040 carrying an EXTRA label, to prove dedup ORs
#      labels rather than dropping a positive with a keep-first copy
PLANTS: list[tuple[str, set[str]]] = [
    ("you are an idiot", {"toxic", "insult"}),
    ("You  are an   IDIOT", {"toxic", "insult"}),
    ("you are an idiot!", {"toxic", "insult"}),
    ("i will kill you", {"toxic", "threat", "severe_toxic", "insult"}),
]


def build_rows() -> list[dict]:
    rows = []
    for i, (text, positives) in enumerate(BASE + PLANTS):
        row = {"id": f"c{i:03d}", "comment_text": text}
        for label in LABELS:
            row[label] = 1 if label in positives else 0
        rows.append(row)
    return rows


def main() -> None:
    out = Path(__file__).parent / "mini_jigsaw.csv"
    rows = build_rows()
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "comment_text", *LABELS])
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
