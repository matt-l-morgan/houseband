# References

A reference MIDI is a real piece of music that a round is measured against.
It is used two ways, and it is worth being precise about both, because the difference between them is the difference between a working system and a plagiarism machine.

## The two uses

**1. A blind calibration anchor.**
The reference enters the candidate pool unlabelled, with an opaque id like every other candidate, and the judges rank it alongside the agents' work.
Its Elo is pinned at a fixed rating and never updates, so team ratings stay on an interpretable scale across rounds.

This is the cheapest judge-validation mechanism available.
If agent output beats a real song on melody or form, the judge is miscalibrated, and you know that *before* you build a learning loop on top of a broken signal.
Without it, a run can show scores climbing beautifully for three rounds while the music gets worse, and nothing in the numbers would tell you.

**2. A source of derived structural criteria.**
A one-time analyst pass extracts *structural facts* into `criteria.md`: how many instrumentation tiers the arrangement uses, whether there is a bare section and where, the climax position as a fraction of total length, target duration.
Composers see only these criteria.
They never see the reference's notes.

## Never a similarity target

Reference MIDI is never used for similarity scoring, and this is not a preference.

Rewarding similarity makes plagiarism the optimal policy.
If a judge scores "how close is this to the reference", then the highest-scoring possible submission is the reference itself, and a competent optimiser will find that out fast.
Every round after that is a race toward copying, and the system stops measuring what it claims to measure.

So the reference sets the *bar*, not the *target*.
Match its structural ambition with your own material.

`houseband/validator.py::check_originality` makes that enforceable rather than aspirational.
It reduces each non-drum track to a monophonic top line, converts it to a sequence of melodic intervals, and compares 8-gram windows against the reference's.
More than 12% overlap and the candidate is rejected by the deterministic gate before it ever reaches a judge.

Intervals rather than absolute pitches, deliberately: transposing a lifted melody is the first thing anyone would try, and it should not work.
This lives in the validator rather than in a judge because it is arithmetic, and LLMs genuinely cannot compute n-gram overlap.

## What this repo can distribute, and what it cannot

**This is the real casualty of open-sourcing the project, and it deserves stating plainly rather than papering over.**

The flow that motivates the whole reference mechanism is "drop in a MIDI of a song you love, see whether the agents can reach its structural bar."
That works perfectly on your own machine.
It is not distributable.

Community MIDI transcriptions of copyrighted songs are **derivative works** of the underlying musical composition.
The transcriber's own labour does not change that.
Most of the enormous corpus of freely downloadable song MIDIs on the internet has no license from the rights holders of the compositions, and a public repo that bundled them would be redistributing infringing copies.
Fine for a hackathon demo on a laptop; a real problem in a repo other people clone.

So:

- **You, locally:** put whatever you like in `references/`.
  Everything in there except `README.md` and `.gitkeep` is gitignored, so there is no way to commit one by accident.
- **This repo:** ships no copyrighted transcriptions, and never will.

## Where to get references that are actually free

**[Mutopia Project](https://www.mutopiaproject.org/)** is the best-behaved source.
Public-domain classical scores typeset in LilyPond, each with an explicit license (public domain, CC0, or CC-BY-SA), MIDI exported alongside the PDF.
Both the composition and the typesetting are clear, which is rare.

**[MAESTRO](https://magenta.tensorflow.org/datasets/maestro)** is around 200 hours of virtuosic piano performance from the International Piano-e-Competition, aligned MIDI and audio, released under CC BY-NC-SA 4.0.
The performances are captured from Disklavier pianos, so the timing and dynamics are genuinely human rather than quantised, which makes it an unusually good anchor for the rhythm and production dimensions.
Note the **NC**: non-commercial use only.
The repertoire is public-domain classical.

**Folk and session collections.**
[The Session](https://thesession.org/) holds tens of thousands of traditional Irish tunes, with ABC notation that converts to MIDI; the tunes themselves are traditional and out of copyright, though individual settings are contributed under the site's terms.
The Nottingham Music Database and the ABC folk collections are similar.
These are melodically strong and structurally simple, which makes them a good anchor for melody and a weak one for arrangement.

**Anything you wrote yourself.**
The most underrated option, and the only one with no licensing question at all.

## The genre-coverage problem

**Public-domain rock is thin, and this is a real limitation of the distributed version of houseband rather than an oversight.**

The prompt this system was designed around is "epic long-form rock, building arrangement."
The reference corpus that would calibrate that well consists almost entirely of recordings from 1965 onwards, all of which are firmly in copyright.
What is legitimately free is overwhelmingly classical, plus traditional folk.

The consequences are honest and unavoidable:

- A classical reference calibrates form, melody and voice leading usefully, and does so across genres: an eight-minute piece with a real arc is an eight-minute piece with a real arc.
- It calibrates **groove**, **production** and **rock-idiomatic orchestration** badly or not at all.
  A Chopin nocturne tells a judge nothing about whether a drum part sits right.
- Derived structural criteria transfer better than you might expect, because they are abstractions: "four instrumentation tiers, one bare section at 60%, climax at 78% of length" is genre-neutral.
  It is the pairwise ranking that suffers, because a judge comparing a rock candidate against a nocturne is partly comparing genres.

The workaround, if you want the rock flow, is to use your own reference locally.
There is no workaround for the distributed repo, and pretending otherwise would be worse than saying so.

## Vet quality by ear. This matters more than it sounds.

**A sloppy transcription is a confidently wrong anchor.**

The failure is quiet, which is what makes it expensive.
A transcription with quantised-to-death timing, everything at velocity 100, and a piano patch standing in for the whole arrangement will be ranked *below* the agents' output on rhythm and production.
The judges are not wrong to do that.
But you will read it as "the agents have surpassed a real song", conclude the panel is miscalibrated, and start tuning rubrics against a broken measurement.
Every round after that inherits the error.

So before a MIDI becomes a reference, listen to it:

- **Render it and play it.** `render.render_all()` works on any MIDI, sidecar or not.
  If it sounds like a MIDI file from 1998, it will score like one.
- **Look at the piano roll.** Velocity maps to opacity, so a flat wall of identical alpha means no dynamics.
  Register collisions and eight-bars-looped-sixteen-times are obvious in the picture and laborious to spot in a note list.
- **Check the arrangement is there.** A four-track reduction of a twelve-piece arrangement is a different piece of music, and it will anchor orchestration and arrangement at the wrong level entirely.
- **Check the length.** Target duration is one of the derived criteria, so a truncated transcription teaches the composers to write short.

## Rotate them

Anchoring every round to one reference makes every output that reference's shape.
Use three to five per genre and rotate between rounds.

The reference is rubric-scored once and the verdict cached, since it never changes and re-scoring it every round is pure spend.
Rotating references means more than one cached verdict, which is cheap, and it is the difference between calibrating against music and calibrating against one song.

## Adding one

Drop `.mid` or `.midi` files into `references/` and select one by filename when you start a run:

```bash
python -m houseband.loop --prompt "epic long-form rock" --reference my_reference.mid
```

or pick it from the dropdown in the web UI.
See `references/README.md`.
