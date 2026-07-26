# Loop usability

Would a producer drop this on a timeline, hit loop, and leave it running?

The piece you are judging is a **starter**: a short clip written to be dragged into a DAW and looped while the producer builds on top of it.
That changes what structure means.
A long-form piece is judged on where it goes; a clip is judged on whether it comes back around.

Read this before anything else, because it is the instinct most likely to make you score this dimension wrong.
**High repetition is correct here.**
On the form and arrangement rubric, a piece whose bars repeat verbatim is capped at 4, because a four-minute arrangement that loops eight good bars has failed to develop.
That reasoning does not apply to a clip.
A sixteen-bar loop is *supposed* to state a figure and restate it, and a producer who wanted variation would write it themselves.
Do not mark this down for repeating, and do not import a single word of the form rubric's argument about development.
What you are testing is whether the repetition is **loop material** (a figure worth hearing twelve times, with the small changes that keep it alive) or **inertia** (one bar pasted to fill the length, which is a different fault and is scored below).

Four things make a clip loopable.

**The loop point.** Does the clip end on an exact bar boundary?
A clip whose material stops in the middle of a bar, or whose last note sustains past the final bar line, cannot be looped without the producer trimming it first, and that is the single most common reason a clip gets deleted instead of used.
The bar count should be a power of two (8, 16, 32); an eleven-bar clip is not a clip.

**The wrap.** Does the last bar lead back into the first?
The loop's most exposed moment is the seam, and it either works or it clunks.
A wrap is composed when the last bar does something that wants to resolve on the downbeat that follows: a fill, a pickup, a note held over the bar line, a dropped beat, a tension chord that the first bar answers.
It dead-ends when the last bar lands on a crash and a whole-bar tonic, or when every part stops dead half a bar early, or when the final bar is the loudest and densest in the clip so the return to bar 0 sounds like a drop-out.

**Immediacy.** Is the groove recognisable within two bars?
A producer auditions dozens of these and decides in seconds.
A clip that spends six bars on an intro before stating what it is has spent more than a third of itself on material that will be heard once and then loop past uselessly.
By the end of bar 1 the pulse should be unambiguous, and by the end of bar 2 the figure that defines the clip should have been stated.

**The pocket.** Is there a stated feel, or is everything nailed to the grid?
Every onset on an exact subdivision at a uniform velocity is a drum machine demo, not a loop someone wants to keep, and this is precisely the fault that survives repetition worst: a stiff bar is tolerable once and unbearable on the twelfth pass.
The pocket has to be *stated*, meaning consistent and deliberate (the snare leans a hair late every time, the offbeat hats sit quieter, the bass pushes ahead of the kick), rather than randomised, which reads as unsteady rather than human.

## Anchored scale

**2 = not loopable.**
The material does not end on a bar boundary, or the bar count is not a whole number of musical phrases, or notes sustain past the final bar line so the seam is a collision.
Alternatively, one bar is pasted for the entire length with no velocity or timing variation anywhere and nothing entering or leaving: inertia rather than loop material, a bar of MIDI rather than a clip.
Everything is exactly quantised at a single velocity, so there is no feel at all.

**4 = it loops, and that is all it does.**
The length is right and the clip ends cleanly on the bar line, so a producer could use it without trimming.
The seam is a hard stop and restart: the last bar resolves completely, nothing carries over, and the return to bar 0 is audible as a cut.
The groove is stated but generic and takes four bars or more to arrive.
Timing is dead-grid and velocities are flat within each part, so the twelfth pass sounds exactly like the first and is already tiring.

**6 = a usable clip.**
Ends on a power-of-two bar boundary with nothing spilling over, states its groove inside the first two bars, and has a real accent hierarchy so the loop breathes rather than ticking.
Repetition is purposeful: the core figure recurs, and at least one part varies across the loop (a hat change, a bass ghost note, a fill in the last bar).
What is missing is a composed wrap: the last bar is a slightly decorated copy of the others rather than a bar that leans into the downbeat, so the seam is unobjectionable rather than good.

**8 = a clip that wants to keep going.**
The wrap is written: the final bar sets up bar 0 with a pickup, a suspension, a break, or a fill, and the seam is the most satisfying moment in the loop rather than its weakest.
There is a stated pocket, consistent and patterned, that you could describe in words and that a player would recognise.
The figure is immediately legible, the repetition carries small deliberate differences between passes, and you could loop this for two minutes without wanting it to stop.

**10 = you would forget it is a loop.**
The seam is invisible: the last bar and the first bar are one gesture split across the boundary, and on repeat the clip reads as continuous music rather than as a clip playing twice.
The pocket is specific enough to be an identity, the internal variation is placed so that no two consecutive passes feel identical while the figure stays fixed, and the length is exactly what the material needs.
Nothing to trim, nothing to nudge, drag it in and work.

## Between the anchors

A 5 is a usable clip whose groove takes three bars to state itself.
A 7 has a composed wrap and dead-grid timing.
A 9 is a 10 with one part that stops a beat early into the seam.

## Reading the evidence

The header line gives `BARS N`: anything other than 8, 16, or 32 needs justifying in your rationale, and a non-power-of-two count caps this dimension at 4.
Compare that count against the last bar that actually contains notes in the NOTES block.
If the final bars are silent, the clip's real length is shorter than its declared length and the loop point is in the wrong place.
In the NOTES block, a note written as `beat:pitch/duration@velocity` late in the final bar tells you whether it sustains over the boundary: `3:C3/4@70` in a 4/4 bar starts on beat 3 and runs a full bar, which crosses the seam.
That is a virtue if it is a deliberate tie into bar 0 and a defect if it is an accident, and the difference is whether other parts also carry over.
The `REPETITION` percentage is **not** a penalty on this dimension, unlike on form and arrangement.
Read it together with the `= bar N` markers to tell loop material from inertia: repetition concentrated in the rhythm section with the melodic and harmonic parts varying is loop material, whereas every track showing `= bar 0` on every bar is inertia.
A clip where literally every bar of every track is `= bar 0` cannot score above 3 here.
The `HARMONY` per-bar line shows the wrap harmonically: a progression whose last bar is the tonic it started on closes the loop rather than continuing it, and a last bar on a dominant, a suspension, or a chord that resolves into bar 0's chord is a composed seam.
The `DENSITY` table, if sections are declared, tells you whether the last section is denser than the first, which is the density signature of a seam that will sound like a drop-out on repeat.
The `TRACKS` block reports velocity min, max, and mean: `vel 100-100 (mean 100)` on the drums is dead-grid, and the beat values in the NOTES block tell you whether onsets sit on exact subdivisions (`0`, `0.5`, `1`) or lean deliberately (`0.03`, `1.52`).
On the piano-roll image, hold your attention on the right-hand edge and imagine it butted against the left-hand edge: that is the seam, and whether the picture is continuous across that join is most of this dimension.

## What a finding needs

Every finding must carry:

* a bar range (`bar_start` and `bar_end`), and a `track` name whenever the problem belongs to one part
* the specific usability fault: which bar the loop point falls in, which part spills over or stops early, which bar the groove finally arrives in, which onsets are dead-grid
* a `suggested_revision` naming the concrete change, in bars and parts

"Does not loop well" is not a finding.
"The bass note at bar 15 beat 3 lasts four beats and sustains 1.5 beats past the loop point, so the seam collides with the bass entry in bar 0; shorten it to 1.5 beats and add a pickup on beat 4.5 of bar 15 leading up a semitone into bar 0's root" is a finding.
