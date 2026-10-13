"""Skwoop pico mood model, runtime only: one English sentence -> PAD (valence, arousal, dominance in [-1, 1]) -> mood
+ tag, and urgency(text) for the alert floor. Vendored from mood-model 753e21c (tag emotion-engine-v0.3); training,
context head and mixes stay in that repo. The bridge loads it through emotion.py:

MoodModel(load_weights(path)).mood("The deploy failed.")   # MoodResult(name='sad', tag='[apologetically]', ...)
urgency("The CO alarm is going off, get out now.")         # True: the bridge forces alert / [crisply]
"""

from __future__ import annotations

from skwoop_mood.model import MoodModel, MoodResult, Pad, load_weights
from skwoop_mood.urgency import urgency

__all__ = ["MoodModel", "MoodResult", "Pad", "load_weights", "urgency"]
