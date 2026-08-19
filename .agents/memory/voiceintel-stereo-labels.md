---
name: VoiceIntel stereo Agent/Caller labels
description: How Five9 stereo recordings must be labeled, and why whole-channel transcription mislabels turns
---

# Five9 stereo Agent/Caller labeling

Five9 stereo recordings have cross-channel bleed, and Whisper segments are not
guaranteed single-speaker turns. Two approaches were tried:

1. **Rejected:** transcribe each channel independently and label everything
   from a channel with one role. Bleed lets Whisper pick up both speakers on
   one channel, so consecutive Agent/Caller turns all get labeled Agent
   (user-reported bug).
2. **Current:** keep the mixed transcript's turn boundaries and assign each
   turn by per-window (~0.12 s) RMS dominance between the extracted left/right
   channels.

**Why:** labels must be deterministic and never confidently wrong. Safety rules
baked into `label_segments_by_channel`:
- A turn with clear speech on BOTH channels (an exchange merged into one
  Whisper segment) rejects the entire labeled view — mixed transcript retained
  with a visible notice.
- Ambiguous/near-equal windows and silence neither support nor veto a label;
  no distinct window at all → reject.
- Any channel error → no labels at all (never partial).

**How to apply:**
- Agent channel side is `FIVE9_AGENT_CHANNEL` (default `left`); verify against
  a known call before trusting labels in a new deployment.
- Reprocessing must recompute or clear `speaker_segments` /
  `speaker_label_error` — stale labels describing an old transcript are a
  correctness bug.
- Channel-split temp files need per-call unique names (uuid suffix); Five9
  basenames collide across concurrent imports.
- Only `source='sftp'` records get labels; email voicemails and legacy rows
  stay on the mixed transcript. Production requires a Docker image rebuild to
  pick this up.
