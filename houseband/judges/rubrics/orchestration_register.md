# Orchestration and register

Are the right instruments playing the right parts in the right octaves?

Orchestration is the assignment of musical material to instruments, and register is where in an instrument's range that material sits.
Both are judged against playability and against clarity.

**Playability.** Does each part sit in the register the instrument actually has, and is it something a player could execute?
A bass part above middle C is not a bass part.
A part with three simultaneous notes on a monophonic instrument is not written for that instrument.
A flute part in its bottom octave will be inaudible under anything.
Note that a separate deterministic gate already rejects notes far outside an instrument's range, so anything that reaches you is nominally playable; what remains for you is whether the part sits in a *good* part of the range and whether the instrument is the right choice at all.

**Clarity.** Do the parts occupy distinct registral space, or do they collide?
Two mid-register parts of similar timbre doubling the same octave will smear into one indistinct part.
The classic failures are a piano left hand and a bass part occupying the same two octaves, and pads sitting exactly where the melody is so the melody loses its edge.
Good orchestration separates parts by register, by timbre, or by rhythmic role, and usually by two of the three.

Also judge the shape of the ensemble.
Is the low end covered by one instrument rather than three?
Is there anything in the upper octaves, or does the whole piece live between C2 and C5?
Are there gaps that make the texture sound hollow, or is every octave crowded so nothing stands out?
And is the instrument choice idiomatic: does each part do something that instrument is good at, or is it generic material dealt out to whatever patches were available?

## Anchored scale

**2 = instruments are misused.**
A part is written in a register where its instrument cannot speak (a bass line an octave above where a bass plays, a lead in the bottom of a flute's range), or two or more parts sit in the same octave with the same timbre throughout so the texture is a single mud.
Instrument choices contradict the material: sustained pad material given to a plucked patch, or fast runs given to a slow-attack pad.

**4 = playable but undifferentiated.**
Every part is in a defensible range, but the ensemble is registrally flat: most parts crowd the middle two octaves, the low end is either absent or tripled, and nothing occupies the top.
Instrument choices are generic rather than wrong: the parts would sound much the same on any patch.
No part is written to exploit anything specific about its instrument.

**6 = sensible orchestration with clear roles.**
Bass, harmony, and melody occupy distinct registral bands, the low end is covered once, and each instrument has a defined job in the texture.
Ranges are comfortable.
What is missing is craft: registers are static across the whole piece, doublings are at the unison or octave where a third or a tenth would open the texture, and instrument choices are reasonable defaults rather than decisions.

**8 = orchestration that serves the music.**
Parts are separated by register and by timbre, doublings are chosen for colour rather than volume, and the register of at least one part changes across the piece to support the form (a bass dropping an octave into the final section, a melody rising for its last statement).
Each instrument is asked to do something it is good at.
Voicing spacing is idiomatic: wider intervals in the low register, closer above.

**10 = every timbre and octave is a decision.**
The registral layout is deliberate at every point, with the texture opening and closing to shape the arc, no unintended collisions anywhere, and every doubling and gap doing audible work.
Instrument choices are inseparable from the material: you could not reassign a part to a different patch without losing something.
The ensemble sounds like it was scored, not assembled.

## Between the anchors

A 5 has clear roles but a bass and a left hand fighting for the same two octaves.
A 7 has good separation and static registers throughout.
A 9 is a 10 with one thin patch in the texture.

## Reading the evidence

The TRACKS block gives, per track, the General MIDI program name and number and the pitch range as `low-high` in note names.
That is the primary evidence: overlapping ranges between tracks of similar type are a collision, and a bass whose low note is above C3 is not covering the bottom.
Check the program number against the part's material; a track named "bass" with a program in the 40s is a string patch, not a bass.
The DENSITY table shows whether registral roles shift by section.
The piano-roll image is the fastest way to see register collision and coverage: look for bands of notes overlapping vertically, for an empty top third, and for whether the vertical spread of the texture changes over the piece.

## What a finding needs

Every finding must carry:

* the `track` name (this dimension is almost always track-specific) and a bar range when the problem is local rather than global
* the specific registral or orchestrational fault, with pitches or octaves named
* a `suggested_revision` naming the concrete change: which part, which direction, how far

"Poor orchestration" is not a finding.
"The bass (program 33) spans C3-A4 for the whole piece, overlapping the piano's left hand at C3-C4 and leaving nothing below C3, so the low end is both muddy and thin; drop the bass an octave to C2-A3 and leave the piano where it is" is a finding.
