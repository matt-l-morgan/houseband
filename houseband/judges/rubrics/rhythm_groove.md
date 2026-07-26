# Rhythm and groove

Does it move, and would a player recognise the feel?

Groove is not "are there drums".
It is the relationship between a stated pulse, the pattern that decorates it, and the small timing and velocity deviations that make the pattern feel human.
Three layers matter, and a piece can fail at any one of them.

**The pulse.** Is there an unambiguous beat, and is it stated by something (kick, bass, comping) rather than merely implied?
**The pattern.** Is the rhythmic figure idiomatic for the genre, and does it interlock across instruments so that the parts answer each other rather than all landing together?
**The feel.** Do velocities differentiate accented from unaccented notes, and does the timing sit exactly on the grid or lean deliberately ahead or behind it?

The characteristic machine failure is the perfectly quantised, uniformly loud, one-bar loop.
It has a pulse and a pattern and no feel, and it is instantly recognisable as not-played.
The second characteristic failure is the opposite: randomised timing and velocity applied evenly to everything, which does not sound human, it sounds unsteady.
Humanisation is patterned, not random: backbeats are louder, offbeat hats are quieter, ghost notes are much quieter, and the deviations recur.

Also judge kick-and-bass alignment.
If the bass note onsets and the kick onsets disagree by a sixteenth for no reason, the low end will sound muddy no matter how good the pattern is.

## Anchored scale

**2 = no groove.**
No steady pulse, or a rhythm section playing a single repeated note value with no accent pattern at all.
Timing is either rigidly identical everywhere with uniform velocity, or scattered enough that the beat is unfindable.
Kick and bass contradict each other.

**4 = a pulse and a loop.**
There is a clear beat and a genre-plausible one-bar or two-bar pattern, repeated identically for the whole piece.
Velocities are flat or vary only between instruments, not within a pattern.
Everything lands on the same subdivisions, so the parts double each other rather than interlocking.
No fills, no variation at section boundaries.

**6 = a working groove.**
The pattern is idiomatic, kick and bass agree, there is a real accent hierarchy (downbeats and backbeats louder than the rest), and at least one instrument plays offbeats that complement rather than duplicate the others.
Some variation exists across sections, typically a fill or a hat change.
What is missing is life: timing is exactly quantised, the accent pattern is the same every bar, and the groove does not develop.

**8 = a groove with feel.**
Velocity shaping is patterned and instrument-appropriate, with ghost notes, accents that recur where the genre expects them, and dynamic build within sections.
Timing has deliberate placement: something leans slightly ahead or behind the grid, and it does so consistently.
Parts interlock, with a syncopation somewhere that creates tension against the pulse and resolves it.
Fills mark transitions and are written, not pasted.

**10 = the groove is the engine.**
The rhythm section reads as players listening to each other: the pattern evolves across the piece, syncopations are set up and paid off, the accent hierarchy shifts to mark form, and micro-timing gives the whole thing a consistent identifiable feel.
Nothing is randomised and nothing is rigid.
You would tap along without meaning to.

## Between the anchors

A 5 is a working groove whose kick and bass are slightly out of agreement.
A 7 has real velocity shaping but perfectly quantised timing.
A 9 is a 10 with one fill that does not belong.

## Reading the evidence

Drum bars in the NOTES block use `beat:name@velocity`, so the accent pattern is directly visible: a bar where every velocity is the same number is unshaped, whatever the pattern is.
Non-drum bars use `beat:pitch/duration@velocity`, and the `beat` values tell you the subdivision grid and whether timing is exactly on it (`0`, `0.5`, `1`) or deliberately displaced (`0.03`, `1.48`).
`= bar N` on every drum bar means a pasted loop and caps this dimension at 4.
The TRACKS block reports velocity min, max, and mean per track: a range of two or three points is a flat part.
Compare kick onsets against bass onsets bar by bar to check low-end alignment.
On the piano-roll image, a flat groove shows as a perfectly regular grid of identically shaded blocks; velocity shaping shows as visible variation in intensity.

## What a finding needs

Every finding must carry:

* a bar range (`bar_start` and `bar_end`) and the `track` name
* the specific rhythmic fault: which subdivision, which accent, which misalignment, which repetition
* a `suggested_revision` a composer could implement directly

"Groove needs work" is not a finding.
"Every bar of the drums track from bar 8 to bar 39 is an exact repeat of bar 8 at velocity 100 throughout, so the chorus has no lift; accent the backbeats on 2 and 4 to velocity 112, drop the offbeat hats to 60, and write a two-beat fill into bar 39" is a finding.
