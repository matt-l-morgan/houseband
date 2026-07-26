# Prompt adherence

Did the piece deliver what was asked for?

This is the only dimension where the brief is the whole standard.
A beautiful piece that answers a different question than the one asked is a failure here, and saying so is not pedantry: a composer who drifts off the brief has not solved the problem, they have changed it.
Judge every explicit constraint in the brief separately, then judge whether the piece reads as the thing described.

Explicit constraints are the checkable ones: genre, mood, tempo, instrumentation, length, and any structural notes.
The implicit constraint is harder and matters more: a brief asking for "menacing" is not satisfied by a minor key alone, and a brief asking for "a lullaby" is not satisfied by slow tempo plus a glockenspiel.
Ask whether a listener given the brief and the audio would recognise one as the answer to the other.

## Anchored scale

**2 = the piece contradicts the brief.**
A requested instrument is absent, the tempo is off by more than about 20 percent with no interpretive reason, the length is under half or over double what was asked, or the mood is the opposite of the one requested.
Also 2 if the piece ignores an explicit structural instruction entirely: asked for a verse and chorus, delivered one continuous texture.

**4 = the brief was skimmed.**
The obvious surface requirements are met (roughly the right tempo, roughly the right instruments) but at least one explicit constraint is missed or fudged, and the mood is generic rather than the specific one requested.
A "melancholy waltz" that is in 3/4 at the right tempo but emotionally neutral lands here.
So does a piece that hits every literal requirement while sounding like a demo of the requested instruments rather than a piece of music in the requested style.

**6 = every explicit constraint is met and the mood is broadly right.**
Correct instrumentation, tempo within a few BPM of the request, length in range, structure as described.
The requested mood is present but rendered with stock devices: minor key for sad, high strings for uplifting, a four-on-the-floor kick for energetic.
This is the competent-but-unremarkable score and it is where most work should land.

**8 = the brief is met and interpreted.**
Every constraint is satisfied, and the piece makes at least one specific choice that shows the brief was read rather than parsed: an instrument used in a register or role the genre suggests but the brief did not spell out, a tempo chosen at the edge of the requested range because that edge suits the mood, a structural decision that serves the described emotional arc.
Genre conventions are handled as conventions, not as a checklist.

**10 = the piece is the obvious answer to the brief.**
It satisfies every constraint, and the constraints have visibly shaped the music at every level rather than being decorated onto it.
The brief's mood is realised through harmony, rhythm, register, and arrangement acting together, and a listener could infer most of the brief from the piece alone.
Reserve this for work where you cannot name a change that would serve the brief better.

## Between the anchors

Odd scores mean the piece sits between two descriptors.
A 5 met every constraint but the mood is thin.
A 7 has one interpretive choice that works and one that misfires.
A 9 is a 10 with one flat spot you can point to.

## Reading the evidence

The score text header gives you KEY, TIME, BPM, BARS, and LENGTH, so tempo, metre, and duration claims are checkable arithmetic, not impressions.
The TRACKS block names the General MIDI program per track, so instrumentation claims are checkable too.
Do not accept a track named "strings" as strings if its program is a synth pad; the program number is the truth.
The SECTIONS block tells you whether declared structure matches the brief's structural notes.

## What a finding needs

Every finding must carry:

* a bar range (`bar_start` and `bar_end`) and/or a `track` name, so the composer knows where to look
* the specific brief requirement it violates, quoted or paraphrased in the claim
* a `suggested_revision` a composer could act on without asking a follow-up question

"Does not match the brief" is not a finding.
"The brief asks for solo piano but bars 16-31 add a drum kit, which breaks the intimacy the brief asks for; remove the kit and carry the energy lift with left-hand register instead" is a finding.
