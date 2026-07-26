# Production

Would this sound like a finished record, given the notes that are here?

This dimension judges everything about the presentation of the material that is not the material itself: velocity and dynamics, balance between parts, stereo placement, and the technical hygiene of the MIDI.
It is weighted lowest of the eight, because a piece with excellent production and no melody is still a bad piece, and because the palette available through General MIDI limits how much production can be judged at all.
Judge what is controllable: velocity, balance, panning, note lengths, and cleanliness.

**Dynamics.** Does velocity do work?
Within a bar it should mark accents; within a phrase it should shape a line; across sections it should support the form.
A piece where every note is velocity 100 has thrown away the one expressive control MIDI reliably gives you.
A piece where velocity is randomised uniformly has thrown it away differently.

**Balance.** Are the mean velocities of the parts sensible relative to their roles?
A lead at mean velocity 70 under pads at mean 110 will be buried.
A kick quieter than a hi-hat is a mix error.

**Stereo image.** Are parts panned, and does the panning make sense?
Bass and kick belong near the centre; a duplicated or doubled part benefits from separation; hard-panning the lead is almost always wrong.
Everything at pan 0.00 is a mono mix, which is not a failure but is a missed opportunity, and a piece with four midrange parts all at centre will sound cluttered for a reason that panning would fix.

**Hygiene.** Are note durations musical, or are they all identical regardless of context?
Are there overlapping same-pitch notes on one track, which will sound like stuck notes?
Do notes end before the next one starts where the instrument needs them to?
Does the piece end, or does it stop mid-phrase with notes cut off?

## Anchored scale

**2 = the presentation is broken.**
Every note at one velocity, or velocities so erratic that phrases jump between inaudible and clipping.
A part that will be completely masked by another.
Stuck notes from overlapping same-pitch events, notes with near-zero duration, or an abrupt cut at the end mid-phrase.

**4 = flat but functional.**
Velocities are constant within each part and differ only between parts, so nothing is masked but nothing is shaped.
All parts at pan centre.
Note durations are uniform (every note exactly one beat, or every note staccato) regardless of the part's role.
The piece begins and ends without artefacts and that is the extent of the production.

**6 = competent presentation.**
Velocity varies within parts to mark accents and there is at least some dynamic difference between sections.
Balance is sensible: the lead sits above the accompaniment, the low end is present without dominating.
Note lengths differ by role (sustained pads, shorter comping).
What is missing is intent: dynamics are local rather than architectural, panning is absent or perfunctory, and the ending is a fade or a final chord rather than a considered close.

**8 = production that supports the arrangement.**
Velocity shapes phrases and builds across sections so that the form's climax is also the loudest and densest point.
Parts are panned to open the stereo field, with low frequencies centred and mid-register parts separated.
Note lengths and overlaps are idiomatic per instrument, including deliberate legato where a line should connect.
The ending is composed.

**10 = the presentation is indistinguishable from an intentional mix.**
Dynamics operate at all three scales at once (accent, phrase, section) and are consistent with the instruments' behaviour.
Balance and panning make every part audible and give the texture depth.
Note articulation is specific to each part's role, transitions are clean, and there is nothing you would fix before shipping it.

## Between the anchors

A 5 has shaped velocities in one part and flat velocities everywhere else.
A 7 has good dynamics and no panning.
A 9 is a 10 with one part a few velocity points too loud.

## Reading the evidence

The TRACKS block reports, per track, velocity min, max, and mean, plus pan.
That is most of what you need: `vel 100-100 (mean 100)` is a flat part, and comparing means across tracks tells you the balance.
Pan is reported as a signed value where 0.00 is centre.
The NOTES block gives per-note velocity and duration in beats, so accent patterns and articulation are checkable bar by bar.
The piano-roll image encodes velocity as intensity, so a picture of uniformly shaded blocks is a flat mix and visible variation in shading is dynamic shaping; it is the fastest way to see whether the loudest moment coincides with the formal climax.
Note that the deterministic gate already warns about overlapping same-pitch notes, so if you see them mentioned they are real.

## What a finding needs

Every finding must carry:

* the `track` name and a bar range where the problem is local
* the specific production fault with numbers: which velocities, which pan value, which durations
* a `suggested_revision` stating the concrete target

"Mix needs work" is not a finding.
"The pads track sits at velocity 105-110 (mean 108) across bars 0-63 while the lead averages 82, so the melody is masked for the whole piece; drop the pads to a mean near 70 and let them rise to 90 only in bars 48-55" is a finding.
