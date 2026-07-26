# references/

Drop `.mid` or `.midi` files in this directory.
They become selectable by filename when you start a run, in the web UI's dropdown or with `--reference <filename>` on the CLI.

## What happens to a file you put here

A reference is used two ways, and never as a similarity target.

1. **As a blind calibration anchor.**
   It joins the candidate pool unlabelled, and the judges rank it against the agents' work.
   If the agents beat a real song on melody or form, the judge panel is miscalibrated and you have learned that before trusting a single score.
2. **As the source of derived structural criteria.**
   An analyst pass extracts structural facts (instrumentation tiers, whether there is a bare section, where the climax falls, target duration) into `criteria.md`.
   Composers see those facts and never the reference's notes.

Rewarding similarity would make plagiarism the optimal policy, so `houseband/validator.py::check_originality` rejects any candidate sharing more than 12% of its melodic 8-gram windows with a reference, measured in intervals so transposition does not help.

## Nothing here is committed

`.gitignore` excludes everything in this directory except this file and `.gitkeep`.

That is deliberate.
Community MIDI transcriptions of copyrighted songs are derivative works of the underlying compositions, and a public repo cannot redistribute them.
The gitignore rule means you cannot commit one by accident, even with `git add -A`.

Use whatever you like here on your own machine.
For sources that are genuinely free to redistribute, and for the reason public-domain rock is thin enough to be a real limitation, see [`docs/references.md`](../docs/references.md).

## Before you trust one

Listen to it first.
A sloppy transcription is a confidently wrong anchor: quantised timing and flat velocities will score *below* the agents' output, and you will misread that as the judges being broken rather than the reference being bad.

```bash
PYTHONPATH=. .venv/bin/python -c "
from pathlib import Path
from houseband import render
a = render.render_all(Path('references/YOUR_FILE.mid'), Path('runs/ref-check'))
print(a.audio, a.piano_roll)
"
```

Then play the audio and look at the piano roll.
Velocity maps to opacity, so a flat wall of identical shading means there are no dynamics to anchor against.
