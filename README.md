# houseband

An agentic loop that writes **loopable MIDI starter clips for producers**.
Three composers each write a 16-bar idea as Python that emits MIDI, a panel of LLM judges critiques every take against anchored rubrics, you keep or bin the stems you actually want, and a coach turns both signals into durable instructions so the next round is better.

Every take downloads as a DAW bundle: one combined MIDI file plus a separate file per stem, tempo map intact, ready to drag into Ableton, Logic or Pro Tools.

**The interesting claim being tested is not "an LLM can write music."**
It is that **agents can measurably improve at a creative task when given structured critique and a mechanism to absorb it.**

That distinction shapes everything below.
A system built to show that an LLM can produce music needs one good output.
A system built to show that agents *improve* needs the improvement to be visible and falsifiable, which means a critique signal specific enough to act on, a way for a lesson to persist past the round that produced it, and a check that the signal has not quietly become something the agents are gaming.
Most of the machinery here exists for that third thing.

You run it locally with your own Anthropic credential and watch the agents work in a browser.

## Why clips

A 16-bar loop is about 30 seconds at 128bpm, 22 at drum-and-bass tempo, 43 at 90.
That is the unit a producer actually starts from, and the tool is built around it for three reasons.

It is cheap enough to iterate on.
Somebody auditioning ideas will not wait six minutes per take, and the whole point is running many rounds.

It is judgeable.
A four-minute arrangement gives a judge too many places to hide a vague opinion; sixteen bars forces every finding to name a bar.

It is the honest deliverable.
The output is a starting point, not a finished record, and a tool that pretends otherwise wastes the producer's time.

## Architecture

```
user prompt ("dub techno loop at 124, room for a vocal")
      |
      v
 [brief]      prompt -> structured brief (genre, tempo hint, instrumentation)
 [criteria]   brief  -> criteria.md, deterministic. No model call.
      |
      v
 +--------------------------------------------------------+
 |  ROUND N                                               |
 |                                                        |
 |  3 composers, in parallel                              |
 |    prompt = brief + criteria.md + own PLAYBOOK.md      |
 |    tools  = house library + render_midi()              |
 |         |                                              |
 |         v                                              |
 |    program.py -> out.mid   (16 bars, must loop)        |
 |         |                                              |
 |    +----+-------------+----------------+               |
 |    v                  v                v               |
 |  parsed score   piano roll + audio  DAW bundle         |
 |  (text)         (audition it now)   (combined + stems) |
 |         |                                              |
 |         v                                              |
 |  VALIDATOR (deterministic, non-scoring)                |
 |    rejects unparseable MIDI and out-of-range parts.    |
 |    A compiler check, not a judge.                      |
 |         |                                              |
 |         v                                              |
 |  JUDGE PANEL (all LLM)                                 |
 |    rubric judges  -> per-dimension findings, each      |
 |                      anchored to a bar range           |
 |    pairwise judge -> Elo, both presentation orders      |
 |         |                                              |
 |         |        <---- YOU: keep / maybe / discard,    |
 |         |              per stem, plus a note           |
 |         v                                              |
 |  COACH                                                 |
 |    producer feedback  -> playbook rules  (outranks)    |
 |    judge findings     -> playbook rules                |
 |    recurring findings -> new house library functions   |
 +--------------------------------------------------------+
      |
      v  repeat
```

Every stage appends a typed JSON event to `runs/<id>/events.jsonl`.
The pipeline never talks to the UI; the UI is a pure reader of that file.

## Design decisions, and why

### Composers emit Python that writes MIDI

Not raw MIDI bytes, and not JSON note lists.

Tick arithmetic is where a model makes silent errors that a human reading the output cannot see.
The house library takes bar and beat, so a composer says `bar 4, beat 1` and the conversion to ticks and seconds happens once, in tested code.

Loops make structure cheap.
A four-bar figure restated with variation is three lines of Python and forty note events in JSON, and the version that is three lines is the version a model gets right.

And code is the only artifact where a judge's lesson can become a reusable capability.
"Your hi-hats are rigidly quantised" can become a `swung_hats()` function that every later round calls.
A lesson about a JSON blob has nowhere to live.

### The criteria are deterministic, derived from the brief

`houseband/criteria.py` turns the brief into the structural targets every composer is briefed against and every judge scores against.
It is plain code with no model call, so the same prompt always produces the same criteria.

This matters more than it looks.
If the criteria can drift between runs, a rising score across rounds might be the target moving rather than the music improving, and the central claim becomes unfalsifiable.

**This replaced a reference-recording mechanism, and the failure mode is worth recording.**
Criteria used to be derived by an LLM analyst reading a MIDI transcription of a real song, which doubled as a pinned calibration anchor at the top of the Elo table.
They were cached per reference file, and a run that asked for *no* reference silently adopted whichever file sorted first in `references/`.
So a request for a house loop was briefed against a transcription of a six-minute rock song: every composer was told to build toward a climax in the final third, and the judges then marked the clips down for not having one.
The anchor itself scored 1/10 on prompt adherence and 2/10 on loop usability, because it was a full arrangement being asked to be a 16-bar loop.
Deriving criteria from the brief cannot drift that way, and it drops a feature that required every user to supply a copyrighted file the repo could never ship.

### All the judges are LLMs

There is no DSP metric suite, deliberately.

A metric that measures note-density variance is precise and measures the wrong thing.
The reason to want language-based judges is that their criteria are *pliable*: adding "leave room for a vocal" is a paragraph in a rubric, not a new statistic and a new threshold to tune.

The rubrics live in `houseband/judges/rubrics/*.md` as prose, loaded at runtime.
Editing a judge means editing a markdown file.

### Rubric judges give feedback; a pairwise tournament gives the ranking

Two different jobs, and one judge doing both does neither well.

Absolute scores are actionable but drift: a model's idea of "7" moves between calls, and across rounds that drift is indistinguishable from progress.
Pairwise comparison is stable but tells a composer nothing it can act on.

So the panel does both.
Nine rubric dimensions produce findings that must cite a bar range and name a track, which is what the coach can turn into a rule.
A pairwise tournament produces the ranking, and every pair is judged **in both presentation orders** with disagreement recorded as a draw, because position bias is real and a single-order verdict is exactly the biased signal the module exists to discard.

Elo here is zero-sum, so it ranks within a round and says little across them.
The absolute measure is the weighted rubric total, whose anchors are written descriptors, so a 7 in round one means the same thing as a 7 in round five.

### The groove outranks the melody

The weights in `houseband/types.py` encode a product decision that looks wrong until you have watched someone audition clips.

`rhythm_groove` and `loop_usability` carry the most.
A producer drops a clip on a timeline and nods or does not, within about two bars.
A great progression over a stiff groove gets deleted; an ordinary progression over a groove that moves gets kept.

`melody` is weighted *below* harmony, on purpose.
The producer supplies the topline.
A fully-formed melody competes for the register and the attention a vocal needs, and is the first thing deleted.

`form_arrangement` is absent entirely.
A 16-bar loop has one section by definition, so a clip would score 2 on a form rubric however good it was, and a composer reading that finding would be coached into adding an intro and an ending that ruin the thing which made it useful.

### Producer feedback outranks the judges

The judges are a proxy for usefulness.
You keeping or binning a stem *is* usefulness.

So the coach prompt puts your feedback at the top, with deletion tallies across rounds, and says explicitly that it outranks the judges' findings.
A pad you deleted twice running is a stronger signal than any rubric score.

The per-stem part is the useful part.
"This is a 6/10" teaches a composer nothing; "the kit is usable, the pad is mud" teaches it something specific.

### Playbooks are per-composer, not per-role

Each composer keeps its own `playbooks/<name>.md` with a provenance ledger, so a rule has to earn its slot: the ledger records which finding produced it and whether the score on that dimension actually moved afterwards.

Sharing playbooks by role would leak lessons between competitors and destroy the comparison.
The three personas are meant to diverge.

### Anti-Goodhart, built in from the start

Agents optimise against whatever they are told about, so the system holds things back and cross-checks itself.

One **rotating held-out dimension** per run is never shown to the coach, giving an unpolluted read on whether the music improved or only the rubric compliance did.
**Judge-variance re-scoring** reports `ScoredDimension.spread`, so a one-point gain inside a three-point spread is visibly not a result.
**Diversity selection** (`houseband/judges/diversity.py`) picks a varied shortlist by farthest-point over score-derived descriptors rather than taking the Elo winner, because ideation wants several different ideas rather than one champion.
And the **calibration gate** (`scripts/calibration_check.py`) scores a hand-written competent clip against a deliberately terrible one, blind, and fails loudly if the panel cannot separate them.

That last one is the gate everything else rests on.
A learning loop built over a panel that cannot discriminate trains on noise, and it is far better to find that out from one script than after reading three rounds of noise as progress.

### Event-sourced, so the visualisation is nearly free

Every stage appends one typed event to `runs/<id>/events.jsonl`.
The server replays that file and then tails it over SSE; the browser is a pure reader.

This is why a composer that crashes the pipeline cannot take the UI down, why the server can restart mid-run without losing an event, and why a page can attach to a run that some other terminal started.
Credential scrubbing is enforced in the writer rather than at the call sites, because `events.jsonl` is exactly the file someone attaches to a bug report.

## Quickstart

Requires Python 3.11+ and about 40MB of download for the soundfont.

```bash
git clone https://github.com/matt-l-morgan/houseband.git
cd houseband

# Creates .venv, installs dependencies, checks for FluidSynth, fetches a
# soundfont, then proves it works by rendering examples/good_program.py.
bash scripts/setup.sh
```

`scripts/setup.sh` is idempotent and safe to re-run.
It stops and tells you what to install if FluidSynth is missing, rather than installing system packages on your machine uninvited:

```bash
brew install fluid-synth          # macOS
sudo apt-get install fluidsynth   # Debian/Ubuntu  (note: no hyphen)
```

If you would rather do it by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/fetch_soundfont.py     # prints the license it installs
```

Then give it a credential and start the server:

```bash
export ANTHROPIC_API_KEY=sk-ant-...

PYTHONPATH=. .venv/bin/python -m houseband.server
```

Open <http://127.0.0.1:8000>, type a prompt, pick a clip length and a number of rounds, and watch the board fill in.
As each composer finishes you can play its clip immediately, rate it stem by stem, read what the judges said, and download the DAW bundle.

`PYTHONPATH=.` is how this repo runs, on purpose.
`render.py` hands the same path to the subprocess that executes a composer's program, so it has to work whether or not anyone ran `pip install -e .`.

### Headless

The browser is optional.
The pipeline runs standalone and the event log is the complete record:

```bash
PYTHONPATH=. .venv/bin/python -m houseband.loop \
    --prompt "dub techno loop at 124, sparse, room for a vocal" \
    --teams 3 --rounds 3 --bars 16
```

`--bars` accepts 8, 16 or 32.
Those are the lengths the rubrics and the DAW-readiness check are written against; an arbitrary bar count produces a clip that does not loop on a four-bar phrase boundary, and the loop-usability judge would mark the composer down for our arithmetic.

### Verify the render path without spending anything

The whole audio path works with no credential at all:

```bash
PYTHONPATH=. .venv/bin/python -c "
from pathlib import Path
from houseband import render
code = Path('examples/good_program.py').read_text()
r = render.execute_program(code, Path('runs/check'))
a = render.render_all(r.midi_path, Path('runs/check'), r.sidecar_path)
print(a.audio, a.piano_roll)
"
```

`examples/good_program.py` and `examples/bad_program.py` are the hand-written judge calibration pair.
If the panel cannot rank the good one decisively above the bad one, the rubrics are broken and no amount of agent work will fix it:

```bash
PYTHONPATH=. .venv/bin/python scripts/calibration_check.py
```

### Work on the UI without spending anything

`scripts/synthetic_run.py` writes a complete, plausible event log with no API calls, so the whole board can be developed and demoed offline:

```bash
PYTHONPATH=. .venv/bin/python scripts/synthetic_run.py --run-id synthetic
```

## Credentials

**Your key stays on your machine.**
Nothing in this repo writes a credential to disk, and nothing writes one to a log.

Three ways to supply one, in the order the SDK resolves them:

1. **Environment variable.**
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   # or
   export ANTHROPIC_AUTH_TOKEN=...
   ```
2. **An `ant auth login` profile.** If you use OAuth, there is nothing to export.
   ```bash
   ant auth login
   ```
3. **Paste it into the web UI.** Useful if you would rather not put a key in your shell history.

The code never touches the value.
`houseband/config.py` holds no credential; the Anthropic SDK's zero-argument `Anthropic()` constructor already resolves all three sources on its own.
`config.credential_source()` reports which source will be used, *by name*, without reading it, which is enough to render "configured" in the UI and to fail fast with a clear message instead of a stack trace.

A key pasted into the UI lives in one module-level dict in the server process and nowhere else.
No endpoint returns it, not even the one that submitted it, and not a prefix or suffix of it either.
It reaches the pipeline through the child process environment, and is forgotten when the server restarts or when you `DELETE /api/credential`.

**No credential ever reaches a composer's program.**
`render.execute_program` builds the subprocess environment from scratch with exactly `PATH`, `HOME`, `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE` and `MPLBACKEND`.
A program that goes looking for a key finds nothing.

As a backstop, `events.py::scrub` runs over every event payload before it is written, redacting sensitive key names wholesale and pattern-matching key-shaped strings anywhere in the payload.
The guarantee is tested against the bytes on disk rather than asserted: `tests/test_events.py::test_message_and_data_are_both_scrubbed` and `tests/test_integration.py::test_no_key_material_reaches_the_log`.

## Docker

```bash
docker build -t houseband .
docker run --rm -p 8000:8000 \
    -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    -v "$PWD/runs:/app/runs" \
    houseband
```

Then <http://127.0.0.1:8000>.

The image installs FluidSynth, fetches the soundfont at **build** time so a deployed container needs no manual step, and runs as a non-root user.
It ends the build by actually rendering `examples/good_program.py` inside the image, because a build that produces an image which cannot render is a build that should have failed.

**No credential is baked into the image**, deliberately: a key in a layer is a key in every copy of it.
Supply one with `-e ANTHROPIC_API_KEY=...` or leave it unset and paste one into the UI.

Mounting `runs/` is optional but recommended, otherwise every artifact from a run disappears with the container.

Before deploying anywhere other than your own machine, read [`docs/security.md`](docs/security.md).
The short version: run it single-user, and do not expose an instance publicly with your own key configured.

## What a run produces

```
runs/<id>/
  events.jsonl          the complete record; everything else is derived
  request.json          prompt, composers, rounds, clip length, model, budget
  meta.json             held-out dimension and run settings
  criteria.md           the structural targets every composer was briefed against
  feedback.jsonl        your keep/discard verdicts, append-only
  staged/               library functions the coach proposed, awaiting approval
  exports/              DAW bundles, built on request
  round1/
    artifacts/
      r1c1.oga          audio, by blind candidate id (format depends on your
      r1c1.png          FluidSynth build; .oga where Vorbis is available)
      ...
    <composer>/         its scratch dir: program.py, out.mid, every revision it
                        accepted along the way, and daw/ with the bundle
    verdicts.json       per-dimension findings with bar anchors, plus ratings
    composers.json      turns, render attempts, and token usage per composer
  round2/ ...
playbooks/<name>.md     what the coach taught each composer, and why
houseband/house/learned.py   capabilities the coach added
```

Candidates are named `r1c1`, `r1c2` in the artifacts directory rather than by composer, because judges must be blind to which one produced which clip.
`verdicts.json` holds the `id_to_team` mapping for after the fact.

The event log records a finding *count* rather than the finding text, because one round of full findings runs over 100KB and the log is replayed in full on every page load.
`verdicts.json` is the authoritative record of what the judges said, and the server merges it into the API when you open a take's findings.

## Limitations

Stated plainly, because the point of the project is a falsifiable claim and a README that oversells it defeats the purpose.

**The audio will not sound like a produced record.**
Rendering is FluidSynth plus a General MIDI soundfont.
Even with a good modern bank, GM is single-dynamic samples with no articulation, no amp modelling and no mixing, so a "distorted guitar" is a static sampled waveform rather than an instrument being played.
The audio exists so you can audition an idea in two seconds; the deliverable is the MIDI.
Judge the composition and the groove, not the production, which is a property of the renderer and not of the agents.
See [`docs/soundfonts.md`](docs/soundfonts.md).

**LLM judges are noisy, and the system mitigates that rather than eliminating it.**
Median-of-3 sampling on the highest-weighted dimensions, both-order pairwise comparison, and the calibration gate all reduce the noise floor or make it visible.
None of them removes it.
`ScoredDimension.spread` reports the observed spread precisely so you can see how much of a round-over-round gain is real, and a one-point improvement inside a three-point spread is not a result.

**This system executes model-written Python.**
The mitigations are a static AST import allowlist, a subprocess with a hard timeout and a scratch working directory, and a minimal environment with no credential.
They are mitigations, not a sandbox: a determined adversarial program can defeat a static allowlist.
The container is the real isolation boundary for anything deployed.
Read [`docs/security.md`](docs/security.md) before you run this anywhere that matters.

**A round costs real money on your key.**
Nine rubric dimensions with median-of-3 on three of them, a both-orders pairwise tournament, and three composers at high effort, per round.
The UI prices the token allowance in dollars before you press start, and `config.round_token_budget` (default 400,000 output tokens) halts a runaway round.

**Three rounds is not evidence of a trend.**
The demo is built to make improvement *visible*, not to establish it statistically.
A single run with three rounds and three composers can show the loop working; it cannot distinguish a real learning effect from a lucky sample.

## Anthropic-only today

houseband is Anthropic-only.
"Plug in your LLM" is satisfied by bringing your own Anthropic credential; multi-provider support is deferred rather than designed out.

The seam is deliberately small.
Model id and effort live in `houseband/config.py` (`DEFAULT_MODEL`, `COMPOSER_EFFORT`, `JUDGE_EFFORT`, `COMPOSER_MAX_TOKENS`, `JUDGE_MAX_TOKENS`) and are never hardcoded at a call site, so swapping models is a one-line change.
The model is also selectable per run in the UI, with per-million-token rates shown, because the choice belongs to whoever is paying.

The provider boundary itself is four places, each importing `anthropic` locally rather than at module scope:

- `houseband/composer.py` -- a hand-written tool loop over `messages.stream`, with one tool, `render_midi`
- `houseband/judges/` -- `messages.parse()` with Pydantic schemas, no tools
- `houseband/coach.py` -- the same
- `houseband/brief.py` -- one structured call per run

What a second provider would have to supply: streaming with a large `max_tokens`, tool use, and structured output against a JSON Schema.
The last is the constraint that matters, and it is why `houseband/types.py` avoids tuples in its schemas.
Prompt caching is used but not depended on.

## Repo layout

```
houseband/
  config.py            soundfont discovery, model settings, clip profile
  events.py            Pydantic event schemas + JSONL writer, with key scrubbing
  types.py             the contracts between composers, judges, and the coach
  timing.py            bar/beat <-> seconds, with a tempo map
  house/               the library composers write against
    core.py            Score, tracks, sections, chords, bar/beat musical time
    learned.py         functions the coach added. Starts almost empty on purpose.
  brief.py             prompt -> structured brief
  criteria.py          brief -> criteria.md, deterministic
  composer.py          the agent loop; one tool, render_midi(code)
  render.py            program.py -> MIDI -> audio + piano roll
  export.py            MIDI -> DAW bundle: combined file plus one per stem
  score_text.py        MIDI -> compact judge-readable score text
  validator.py         the deterministic gate: imports and playable ranges
  judges/              rubric panel, pairwise tournament, Elo, diversity
  coach.py             findings + producer feedback -> playbook rules
  loop.py              round orchestration, events, per-round budget guard
  server.py            FastAPI: launch a run, tail its events, take feedback
web/index.html         single-page live board. No build step, no CDN.
examples/              the hand-written judge calibration pair
scripts/
  setup.sh             fresh clone -> verified working render
  fetch_soundfont.py   install a GM bank, print its license
  calibration_check.py does the judge panel actually discriminate?
  report_run.py        round-over-round scores from a finished run
  synthetic_run.py     a fake run, for working on the UI without spending
docs/
  soundfonts.md        options and their verified licenses
  security.md          executing generated code: mitigations and their limits
```

## Tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Runs in parallel through pytest-xdist; `-n auto` is already in `pyproject.toml`.
No test needs a credential: every agent entry point takes an injected client, so the suite drives the real prompt construction and error paths against stubs.

## License

MIT. See [`LICENSE`](LICENSE).

Soundfonts are fetched, not committed, and carry their own licenses.
Both banks `scripts/fetch_soundfont.py` can install are MIT, verified from primary sources, and the script prints the license text it fetched before it exits, so you are never surprised about what you installed.
See [`docs/soundfonts.md`](docs/soundfonts.md).
