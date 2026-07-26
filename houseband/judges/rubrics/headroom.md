# Headroom

Has the composer left the producer somewhere to work?

The piece you are judging is a **starter**: a clip a producer drags into a DAW as raw material for their own track.
The producer will add a vocal or a lead, replace some sounds, delete some parts, and build the arrangement themselves.
This dimension asks whether the clip makes that possible or gets in the way.

The scoring instinct to correct is the ordinary one: that more finished is better.
Here it is not.
**A starter that is fully arranged and fully mixed scores worse than one with a strong core and deliberate space**, because the finished one has already spent the register, the density, and the attention that the producer needed for their own idea.
The clip's job is to be a foundation.
A foundation that is already a building is not useful.

This is not a licence to hand in less.
Empty is not the same as spacious, and the two failures are symmetrical: a clip with four parts wall to wall gives the producer nowhere to go, and a clip with a lone quantised piano gives them nothing to go on.
Headroom is space that is *shaped by* a strong core, not space left where the core should have been.
The test is whether you can point at what the producer would add and say where it goes.

Five things to look at.

**A slot for the topline.** Is there an obvious place for a vocal or a lead?
That means a register band left largely unoccupied, usually somewhere around the octave above middle C, and it means nothing else competing for the ear in that band.
A clip whose busiest, loudest, most syncopated part sits exactly where a vocal sits has no slot, however good that part is.

**Register.** Is the vertical space congested or organised?
Parts stacked into the same two octaves fight each other and will keep fighting whatever the producer does, because the fix requires rewriting the parts rather than mixing them.
Look for deliberate separation: bass low and alone, harmonic material in the mid, and air above.
Gaps are the point here, not a shortfall.

**Air in time.** Is every bar filled, or does the clip breathe?
Rests are the cheapest headroom there is, and their absence is the most common way a starter suffocates.
A part that plays on every subdivision of every bar leaves no gap for anything to answer it.

**Foundation or finished product.** Would a producer describe this as a groove and a progression, or as a track?
Countermelodies, ear candy, risers, fills every fourth bar, and a fully realised counterpoint are all signs the composer wrote a finished piece and shortened it, rather than writing a starter.

**Separability.** A producer will delete parts.
Every part therefore has to make sense with the others gone, and the core has to survive losing any single part.
The failing pattern is interdependence: a bass line that only implies the harmony when the pad is present, two parts that interlock into one rhythm and read as broken when either is muted, a melody carrying a note the chord needs.
Judge this by mentally muting each part in turn and asking whether what remains is still usable.

## Anchored scale

**2 = nowhere to work, in one direction or the other.**
Either the clip is a finished production in miniature (every register from the bass to the top occupied, every bar filled on every part, a lead line and a countermelody already written, nothing a producer could add without deleting something first), or it is so thin that there is nothing to build on (one part, or two parts playing the same rhythm in the same octave, with no groove or progression established).
Both are unusable and both score here.

**4 = a core exists and it crowds itself.**
There is a recognisable groove and progression, so there is something to build on, but the clip fills its own space.
Mid-register parts overlap heavily, the top of the texture reaches into the vocal band and stays there, and most bars of most parts are continuously active.
Parts are separable in principle but muting any one of them leaves a noticeable hole because each was written to complete the others.

**6 = a usable foundation.**
The core is strong and the register is roughly organised: bass at the bottom, harmony in the mid, and the octave above middle C mostly free, so a producer can hear where a topline goes.
There is some air, with rests in at least one part and not every bar identical in density.
Parts stand up on their own well enough that deleting one leaves something workable.
What is missing is intent: the space is a by-product of a straightforward arrangement rather than something the composer shaped, the top of the texture is unclaimed rather than deliberately cleared, and one part is doing slightly more than it needs to.

**8 = space that was composed.**
You can name what the producer would add and point at where it goes: a specific register band and specific beats are left open, and the parts around them are written to leave that opening rather than happening not to fill it.
Rests are placed so the parts answer each other and there is a gap on the ear at least once per bar.
The core is complete without being finished, meaning groove, bass, and harmony are all stated and none of them reaches for a role that is not theirs.
Every part is independently sensible, and any single one can be muted with the remainder still usable as a starter.

**10 = an invitation.**
The clip is unmistakeably a foundation and unmistakeably deliberate about it: the register is arranged in clean layers with an open band that a topline drops straight into, the rhythmic parts leave holes exactly where a vocal phrase would land, and every part is a self-contained stem that a producer could keep or delete without consequence for the rest.
The material is strong enough that you would want to write over it, and restrained enough that you could.
Nothing here would have to be removed before the producer starts.

## Between the anchors

A 5 is a usable foundation whose pad reaches into the vocal register throughout.
A 7 has composed space but one part that stops making sense when its partner is muted.
A 9 is a 10 with one bar that has no gap in it anywhere.

## Reading the evidence

The `TRACKS` block is the most direct evidence for this dimension, because it reports each part's pitch range as `range C2-G3`.
Write those ranges out and look at what they cover together: overlapping ranges are register congestion, and an unoccupied band above the highest non-drum part is the slot for a topline.
A clip whose parts collectively span from the bass up past C5 has no vocal slot, and you should say which part is in the way and in which bars.
Track count and note counts from the same block separate a foundation from a finished piece: five or six melodic parts in a 16-bar clip is a production, and one melodic part plus drums is a sketch.
The `DENSITY` table, when sections are declared, gives notes per bar per track: figures of ten or more per bar on several parts at once describe a texture with no air, and the table also shows whether any part is ever absent.
In the `NOTES` block, read individual bars for their gaps.
`1:C3/1@70 2:D3/1@70 3:E3/1@70 4:F3/1@70` is a bar with no air in it; a bar whose onsets leave a beat and a half untouched is breathing.
The `HARMONY` per-bar line tells you whether the harmony is stated plainly (which is what a producer wants to build on) or so extended and chromatic that anything they write over it will clash.
Use the same line to test separability: if the detected chord depends on notes that live in the melodic part, the melody is carrying the harmony and cannot be deleted.
Pan values reveal whether space was thought about in the stereo field as well as in register.
On the piano-roll image, headroom is visible as empty area: look for a horizontal band with nothing in it above the main texture, and for vertical gaps where nothing sounds.
A picture that is a solid block from bottom to top and left to right is a 2 or a 4 on this dimension whatever else is true of it.

## What a finding needs

Every finding must carry:

* a bar range (`bar_start` and `bar_end`), and a `track` name whenever one part is the one crowding the space
* the specific fault, with the register or the beats named: which part occupies the vocal band, which bars have no rests, which part cannot survive its partner being muted
* a `suggested_revision` stating what to remove, thin, or move, and to where

"Leave more space" is not a finding.
"The strings track plays block chords from C4 to E5 on every beat of bars 0-15, which occupies the whole vocal register for the entire clip; drop it an octave to voice C3-E4 and cut it to beats 1 and 3 so the top of the texture and the offbeats are both free" is a finding.
