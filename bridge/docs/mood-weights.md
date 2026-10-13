# Mood weights

The mood-model weights are not in this repository. They are CC-BY-SA 4.0 (trained in part on EmoBank) and ship as a separate release.

Place these files next to the bridge package:

- `bridge/weights/skwoop_mood_v14.npz`
- `bridge/weights/urgency_clf_v024.npz`

Until both files are present, the bridge keeps keyword tone for each sentence and a rules-only urgency floor. Tests that need the numeric model skip and say why.
