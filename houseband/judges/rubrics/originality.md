# Originality

Is there a single decision here that surprises you?

Originality is not strangeness.
A piece that is unusual because it is incoherent scores low on every other dimension and should not be rewarded here either.
What this dimension asks is whether the piece makes choices, and whether any of those choices could only have come from this piece rather than from the genre's default settings.

Three things to look for, in ascending order of weight.

**Absence of cliché.** Is the material the first thing anyone would write given the brief?
The four-chord loop with the tonic on the downbeat, the scalar melody starting on the tonic and ending on the tonic, the four-on-the-floor kick with offbeat hats, the whole-note pad under everything.
None of these is bad; all of them are default.
A piece assembled entirely from defaults is not original no matter how well assembled.

**Specific decisions.** Is there a choice you can point to and say a person made this?
An unexpected chord that turns out to have been prepared.
A phrase that is five bars long because the material wanted five.
A metric displacement, an unusual instrument pairing, a section that ends on the wrong beat deliberately, a texture that inverts halfway through.
One such choice, executed well, is worth more than a piece full of unmotivated oddities.

**Coherent voice.** Do the decisions add up?
The highest scores go to pieces where the surprising choices share a logic, so the piece has an identity rather than a list of features.

Two things this dimension explicitly does not reward.
It does not reward similarity to the reference recording; a separate deterministic check measures melodic n-gram overlap against the reference and rejects candidates that reproduce its material, so proximity to the reference is a liability here, not an asset.
It does not reward random variation: velocity jitter, arbitrary chromaticism, and unmotivated metre changes are noise, and noise is the cheapest possible imitation of invention.

## Anchored scale

**2 = entirely generic, or incoherently strange.**
Every element is the genre default, assembled in the default order, with nothing anywhere that could not be predicted from the brief alone.
Score 2 also for a piece whose unusual features are unmotivated: keys, metres, or textures that change without preparation or consequence.

**4 = one small departure from default.**
The piece is built from stock material but does one thing slightly its own way: a passing chord outside the loop, an instrument you would not have guessed, an asymmetric phrase.
The departure is real but isolated and does not affect anything else.

**6 = the piece has recognisable choices.**
Two or three decisions are clearly the composer's rather than the genre's, and they are executed well enough not to sound like accidents.
The overall shape and material are still conventional, and the choices are decorative rather than structural: you could remove them and the piece would still work.
This is the honest score for competent genre writing with some personality.

**8 = the identity of the piece rests on its choices.**
At least one decision is structural: it shapes the form, the harmony, or the melody such that removing it would change what the piece is.
The choices are prepared and paid off rather than merely present, and they are consistent with each other.
Conventions are used knowingly, including being broken at points where breaking them means something.

**10 = you have not heard this exact piece before, and it works.**
The piece has a voice: the harmonic language, the rhythmic identity, the form, and the orchestration all reflect the same set of decisions, and those decisions are not the defaults.
Nothing is strange for its own sake, and nothing is generic.
You could describe what makes it distinctive in one sentence and that sentence would not fit any other piece.

## Between the anchors

A 5 has one structural choice that half works.
A 7 has strong choices that do not quite share a logic.
A 9 is a 10 with one stock passage that gives the game away.

## Reading the evidence

Originality is judged from the material itself, so the NOTES and HARMONY blocks carry most of the weight.
Look for the defaults: a HARMONY line that is the same four symbols repeating, a melody whose first and last notes are the tonic in every phrase, drum bars that are all `= bar N` after the first.
The SECTIONS block shows whether section lengths are all powers of two, which is not a fault but is a sign that nothing about the form was chosen.
The DENSITY table shows whether arrangement decisions were made at all.
On the piano-roll image, look for whatever breaks the pattern: an asymmetry, a passage that does not look like the rest, a register shift.
If the picture is perfectly regular, the piece almost certainly is.

## What a finding needs

Findings on this dimension are harder to anchor than elsewhere, and they must still be anchored.
Every finding must carry:

* a bar range (`bar_start` and `bar_end`) and/or a `track` name identifying the generic or unmotivated passage
* what specifically is default about it, or what specifically is arbitrary about it
* a `suggested_revision` proposing a concrete alternative, not an invitation to be more creative

"Lacks originality" is not a finding.
"Bars 0-15 (chords) are an unaltered I-V-vi-IV loop with the tonic on every downbeat, which is the first progression anyone would write for this brief; keep the loop but delay the tonic to beat 3 of bar 0 and substitute the vi with a IV/vi in bar 10 to give the phrase a distinct cadence" is a finding.
