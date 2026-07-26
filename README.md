# houseband

An agentic music composition loop: three composer teams write Python that emits MIDI, a panel of LLM judges critiques the result against anchored rubrics, and a coach turns that critique into durable improvements so the next round is better.

**The interesting claim being tested is not "an LLM can write music."**
It is that **agents can measurably improve at a creative task when given structured critique and a mechanism to absorb it.**

That distinction shapes everything below.
A system built to show that an LLM can produce music needs one good output.
A system built to show that agents *improve* needs the improvement to be visible and falsifiable, which means it needs a critique signal specific enough to act on, a way for a lesson to persist past the round that produced it, and a check that the signal has not quietly become something the agents are gaming.
Most of the machinery here exists for that third thing.

You run it locally with your own Anthropic credential and watch the agents work in a browser.

## Architecture

```
user prompt ("epic long-form rock, building arrangement")
      |
      v
 [analyst]  once per genre, offline
      |     reads reference MIDI -> criteria.md (structural facts, no notes)
      v
 +----------------------------------------------------+
 |  ROUND N                                           |
 |                                                    |
 |  3 composer teams, in parallel                     |
 |    prompt = brief + criteria.md + own PLAYBOOK.md  |
 |    tools  = house library + render_midi()          |
 |         |                                          |
 |         v                                          |
 |    program.py -> out.mid                           |
 |         |                                          |
 |    +----+-----------------+                        |
 |    v                      v                        |
 |  parsed score        piano-roll PNG                |
 |  (text)              + fluidsynth audio            |
 |         |                                          |
 |         v                                          |
 |  VALIDATOR (deterministic, non-scoring)            |
 |    rejects unparseable / out-of-range / verbatim   |
 |    copying. A compiler check, not a judge.         |
 |         |                                          |
 |         v                                          |
 |  JUDGE PANEL (all LLM)                             |
 |    rubric judges  -> per-dimension findings        |
 |    pairwise judge -> Elo over candidates+reference |
 |         |                                          |
 |         v                                          |
 |  COACH                                             |
 |    findings -> playbook rules (routed by role)     |
 |    findings -> house library functions             |
 +----------------------------------------------------+
      |
      v  repeat
```

Every stage appends a typed JSON event to `runs/<id>/events.jsonl`.
The pipeline never talks to the UI; the UI is a pure reader of that file.

## Design decisions, and why

### Composers emit Python that writes MIDI

Not raw MIDI bytes, and not a hand-designed JSON schema.

Raw MIDI requires hand-computed tick arithmetic, and models get it slightly wrong in ways that produce timing drift.
A judge reads that drift as bad musicianship, and the coach then learns the wrong lesson from it: it starts writing playbook rules about rhythm to fix what was actually an arithmetic bug.
Removing an entire class of misattributed feedback is worth a lot.

A JSON schema avoids the arithmetic but throws away the two things that make code the right choice.
Loops make long-form structure cheap: sixteen bars of a developing arrangement is a `for` loop, not sixteen bars of transcription.
And code is the only artifact where a judge's lesson can become a *reusable function*.
When the panel keeps flagging mechanical timing, the coach can commit `humanize(track, feel="swing_58", vel_sigma=8)` to the shared library and every composer inherits the capability.
With JSON, the same lesson can only ever be another sentence of advice that has to be re-read and re-applied every round.

That is the difference between the learning loop operating in prompt space and operating in capability space, and it is why `houseband/house/learned.py` starts almost empty on purpose.

### All the judges are LLMs

No DSP metric suite, no computed music-theory scoring.

The upside is that the judge panel *is* a set of prompts.
It can be edited, extended, or evolved at runtime, and the rubrics in `houseband/judges/rubrics/*.md` are plain markdown a user can rewrite without touching code.
A hardcoded metric suite is a fixed opinion about what good music is, baked into Python.

The accepted costs are real: higher token spend, and noisier scores.
The variance mitigations below are the price of this decision, not an afterthought.

The two things that are *not* LLM-judged are the two that need arithmetic.
`houseband/validator.py` checks that a bass line is in its instrument's playable range and computes melodic n-gram overlap against the reference.
LLMs cannot reliably do either.
The validator scores nothing: it is a compiler check that decides whether a submission is well-formed at all.

### Rubric judges give feedback; a pairwise tournament gives the ranking

These are deliberately separate jobs, because LLMs are much more consistent comparing two things than scoring one in isolation.

**Rubric judges produce actionable critique.**
One call per dimension across eight dimensions, with structured output.
Four properties carry nearly all the value:

- **Schema-required evidence anchors.** Every finding must name a bar range or a track. This is the whole difference between actionable feedback and vibes.
- **Anchored scales, not "rate 1 to 10."** Each level has an explicit descriptor: *Form: 2 = one section repeated with no variation; 4 = two sections, contrast is dynamic only; 6 = clear ABAB with distinct material; 8 = multi-section arc with a bridge and a reduced section.* A bare 1-10 scale is a vibe with a number attached.
- **Role attribution on every finding.** Each finding names which of `songwriter`, `rhythm`, `arranger`, `mix` is responsible, so the lesson routes to that role's section of the playbook. Without it every agent gets every critique and nobody sharpens.
- **Blind and order-randomised.** Judges never learn which team produced which candidate, and the reference is indistinguishable from an agent's work.

**The pairwise judge produces the leaderboard.**
Head-to-head preference with justification, fed into Elo.
Three corrections for known LLM-judge failure modes:

- **Every pair runs in both presentation orders.** Position bias is real and large. A split verdict counts as a draw.
- **The reference's Elo is pinned** at a fixed anchor rating and never updates, so team ratings stay on an interpretable scale across rounds instead of drifting as a group.
- **The reference is rubric-scored once and cached.** It never changes, so re-scoring it every round is pure spend.

The leaderboard comes from the tournament; the coaching comes from the rubric.
Conflating them would mean either a ranking built on noisy absolute scores or feedback that says only "you lost."

### The reference is a blind calibration anchor, never a similarity target

A reference MIDI enters the candidate pool unlabelled and gets ranked alongside the agents' work.

If agent output beats a real song on melody or form, the judge is miscalibrated, and you know that *before* building a learning loop on a broken signal.
This is the cheapest judge-validation mechanism available: it costs one extra candidate per round and it is the difference between a run that means something and a run that produces a beautiful upward Elo chart while the music gets worse.

The reference is used one other way: an analyst pass extracts *structural facts* into `criteria.md` (instrumentation tier count, presence of a bare section, climax position as a fraction of length, target duration).
Composers see those facts and never the reference's notes.

**It is never used for similarity scoring, and that is not a preference.**
Rewarding similarity makes plagiarism the optimal policy: if the score is "how close is this to the reference", the highest-scoring possible submission *is* the reference, and a competent optimiser finds that out fast.
`validator.py::check_originality` makes the distinction enforceable rather than aspirational, rejecting any candidate that shares more than 12% of its melodic 8-gram windows with a reference.
Intervals rather than absolute pitches, because transposing a lifted melody is the first thing anyone would try.

See [`docs/references.md`](docs/references.md).

### Anti-Goodhart, built in from the start

Agents will learn to game LLM judges.
These are cheap, so they exist from round one rather than being retrofitted after a run turns out to have been measuring nothing.

- The blind reference anchor catches gross miscalibration. `judges/calibration.py` turns it into an explicit gate: the reference must beat every agent on the three structural dimensions (form and arrangement, melody, harmony and voice leading), and a run that fails emits a `JUDGE CALIBRATION SUSPECT` event rather than burying the fact in a report. Production and originality are excluded on purpose, because those are the two dimensions where an agent can legitimately win.
- **One judge dimension is held out from the coach** each run, so it cannot be optimised against directly. Which dimension rotates per run, so none is permanently invisible to learning.
- **Median-of-3 sampling** on the dimensions that drive learning (form and arrangement, melody, rhythm and groove), because a single noisy score can teach the coach something untrue.
- **The panel's own noise floor is measured and reported**, not assumed. `ScoredDimension.spread` is the observed disagreement between samples of the same candidate, and the calibration report carries the mean across dimensions. A dimension whose samples routinely disagree by three points cannot support a one-point conclusion, and that is exactly the number the coach needs before writing a rule.
- Pairwise order-swapping removes the position-bias exploit.
- The honest check no code can do for you: listen to the round 1 winner and the round 3 winner back to back and ask whether the improvement is *audible*, not just numeric. If judge scores climb while your own preference stays flat, you are Goodharting and the run is not showing what you think it is.

### Playbooks are per-team, not per-role

`playbooks/<team>.md`, with `attributed_role` organising rules into sections *within* the file.

A role-keyed playbook shared across teams would leak team A's lessons to team B, which muddies the Elo separation the whole thing exists to demonstrate, and mixes contradictory style advice into one file.
Findings route only to the team whose candidate produced them.

Rules have to earn their slot: which rules were active for which score deltas is recorded, rules that do not correlate with gains get deprecated, and total playbook size is capped so the composer prompt does not bloat into uselessness.

### Event-sourced, so the visualisation is nearly free

Every stage appends a typed Pydantic event to `runs/<id>/events.jsonl`.
`POST /api/runs` launches the pipeline as a detached child process; `GET /api/runs/<id>/events` replays and then tails the log over SSE.

The server never imports the pipeline.
That separation buys three things: the CLI works fully headless and the log is the complete record of a run, any past run replays identically, and a composer that crashes the pipeline cannot take the UI down.
A page reload costs "replay from `from_seq`, then tail", because the log is append-only.

**Every LLM event carries the response `usage` block**, so the UI shows a running token total.
That is a trust feature when users are spending their own key, and the per-round budget guard needs the same numbers to halt a runaway round.

## Quickstart

Requires Python 3.11+ and about 40MB of download for the soundfont.

```bash
git clone https://github.com/mattmorgan/houseband.git
cd houseband

# Creates .venv, installs dependencies, checks for FluidSynth, fetches a
# soundfont, then proves it works by rendering examples/good_program.py.
bash scripts/setup.sh
```

`scripts/setup.sh` is idempotent and safe to re-run.
It will stop and tell you what to install if FluidSynth is missing, rather than installing system packages on your machine uninvited:

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

Open <http://127.0.0.1:8000>, enter a prompt, pick a reference and a number of rounds, and watch the board fill in.

`PYTHONPATH=.` is how this repo runs, on purpose.
`render.py` hands the same path to the subprocess that executes a composer's program, so it has to work whether or not anyone ran `pip install -e .`.

### Headless

The browser is optional.
The pipeline runs standalone and the event log is the complete record:

```bash
PYTHONPATH=. .venv/bin/python -m houseband.loop \
    --prompt "epic long-form rock, building arrangement" \
    --teams 3 --rounds 3 \
    --reference my_reference.mid
```

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
If the panel cannot rank the good one decisively above the bad one, the rubrics are broken and no amount of agent work will fix it.

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
`runs/<id>/events.jsonl` is exactly the file someone attaches to a bug report, so a leak there would not be a recoverable mistake.
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
Mount `references/` too if you want your own reference MIDI available.

Before deploying anywhere other than your own machine, read [`docs/security.md`](docs/security.md).
The short version: run it single-user, and do not expose an instance publicly with your own key configured.

## What a run produces

```
runs/<id>/
  events.jsonl          the complete record; everything else is derived
  meta.json             prompt, teams, rounds, held-out dimension
  criteria.md           structural facts the analyst derived from the reference
  reference/            the reference's own rendered artifacts
  staged/               library functions the coach proposed, awaiting approval
  round1/
    artifacts/
      c1.oga            audio, by blind candidate id (format depends on your
      c1.png            FluidSynth build; .oga where Vorbis is available)
      ...
    <team>/             the composer's scratch dir: program.py, out.mid, and
                        every revision it accepted along the way
    verdicts.json       per-dimension findings with bar anchors, plus ratings
    composers.json      turns, render attempts, and token usage per team
  round2/ ...
playbooks/<team>.md     what the coach taught, and why
houseband/house/learned.py   capabilities the coach added
```

Candidates are named `c1`, `c2` in the artifacts directory rather than by team, because judges must be blind to which team produced which piece.
`verdicts.json` holds the `id_to_team` mapping for after the fact.

## Limitations

Stated plainly, because the point of the project is a falsifiable claim and a README that oversells it defeats the purpose.

**The audio will not sound like a produced record.**
Rendering is FluidSynth plus a General MIDI soundfont.
Even with a good modern bank, GM is single-dynamic samples with no articulation, no amp modelling and no mixing, so a "distorted guitar" is a static sampled waveform rather than an instrument being played.
Judge the composition, the arrangement and the form; do not judge the production, which is a property of the renderer and not of the agents.
This is also why audio is a deliverable rather than a judged artifact: with a bare GM bank every candidate sounds equally like a GM bank, so a listening judge has almost no signal to discriminate on.
See [`docs/soundfonts.md`](docs/soundfonts.md).

**The repo ships no useful references, and cannot.**
The flow that motivates the whole reference mechanism -- drop in a MIDI of a song you love, see whether the agents reach its structural bar -- works locally and is not distributable.
Community transcriptions of copyrighted songs are derivative works, so the repo ships only an empty `references/` directory.
Public-domain rock is thin, so what is legitimately available is overwhelmingly classical and folk, which calibrates form and melody usefully and groove and production badly.
That is a real limitation of the distributed version, not an oversight.
See [`docs/references.md`](docs/references.md).

**LLM judges are noisy, and the system mitigates that rather than eliminating it.**
Median-of-3 sampling, both-order pairwise comparison, the pinned reference anchor and the calibration gate all reduce the noise floor or make it visible.
None of them removes it.
`ScoredDimension.spread` reports the observed spread precisely so you can see how much of a round-over-round gain is real, and a one-point improvement inside a three-point spread is not a result.

**This system executes model-written Python.**
The mitigations are a static AST import allowlist, a subprocess with a hard timeout and a scratch working directory, and a minimal environment with no credential.
They are mitigations, not a sandbox: a determined adversarial program can defeat a static allowlist.
The container is the real isolation boundary for anything deployed.
Read [`docs/security.md`](docs/security.md) before you run this anywhere that matters.

**A round costs real money on your key.**
Eight rubric dimensions with median-of-3 on three of them, twelve pairwise calls, three composer agents at high effort, per round.
The UI shows a running token total and `config.round_token_budget` (default 400,000 output tokens) halts a runaway round, but there is no cost estimate before you press start.

**Three rounds is not evidence of a trend.**
The demo is built to make improvement *visible*, not to establish it statistically.
A single run with three rounds and three teams can show the loop working; it cannot distinguish a real learning effect from a lucky sample.

## Anthropic-only today

houseband is Anthropic-only.
"Plug in your LLM" is satisfied by bringing your own Anthropic credential; multi-provider support is deferred rather than designed out.

The seam is deliberately small.
Model id and effort live in `houseband/config.py` (`DEFAULT_MODEL`, `COMPOSER_EFFORT`, `JUDGE_EFFORT`, `COMPOSER_MAX_TOKENS`, `JUDGE_MAX_TOKENS`) and are never hardcoded at a call site, so swapping models is a one-line change.
The provider boundary itself is four places, each importing `anthropic` locally rather than at module scope:

- `houseband/composer.py` -- a hand-written tool loop over `messages.stream`, with one tool, `render_midi`
- `houseband/judges/` -- `messages.parse()` with Pydantic schemas, no tools
- `houseband/coach.py` -- the same
- `houseband/brief.py` and `houseband/analyst.py` -- one-shot structured calls

What a second provider would actually have to supply: streaming with a large `max_tokens`, tool use, and structured output against a JSON Schema.
The last is the constraint that matters, and it is why `houseband/types.py` avoids tuples in its schemas.
Prompt caching is used but not depended on.

## Repo layout

```
houseband/
  config.py            soundfont discovery, model settings, credential source
  events.py            Pydantic event schemas + JSONL writer, with key scrubbing
  types.py             the contracts between composers, judges, and the coach
  house/               the library composers write against
    core.py            Score, tracks, sections, chords, bar/beat musical time
    learned.py         functions the coach added. Starts almost empty on purpose.
  brief.py             prompt -> structured brief
  analyst.py           reference MIDI -> criteria.md
  composer.py          the agent loop; one tool, render_midi(code)
  render.py            program.py -> MIDI -> audio + piano roll
  score_text.py        MIDI -> compact judge-readable score text
  validator.py         the deterministic gate: imports, ranges, originality
  judges/              rubric panel + pairwise tournament, rubrics as markdown
  coach.py             findings -> playbook rules + staged library functions
  loop.py              round orchestration, events, per-round budget guard
  server.py            FastAPI: launch a run, tail its events over SSE
web/index.html         single-page live board. No build step.
examples/              the hand-written judge calibration pair
scripts/
  setup.sh             fresh clone -> verified working render
  fetch_soundfont.py   install a GM bank, print its license
docs/
  soundfonts.md        options and their verified licenses
  references.md        what can and cannot be distributed, and why
  security.md          executing generated code: mitigations and their limits
```

## Tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## License

MIT. See [`LICENSE`](LICENSE).

Soundfonts are fetched, not committed, and carry their own licenses.
Both banks `scripts/fetch_soundfont.py` can install are MIT, verified from primary sources; the script prints the license text it fetched before it exits, so you are never surprised about what you installed.
See [`docs/soundfonts.md`](docs/soundfonts.md).
