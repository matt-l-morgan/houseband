# Melody

Is there a tune, and does it go anywhere?

A melody is a line you could sing back after two hearings.
That is the working test, and it is stricter than it sounds: most machine-composed music produces pitch sequences that are locally plausible and globally shapeless, because a note-by-note process has no reason to build a contour that arrives somewhere.
Judge the line's identity first, then its development.

Four things carry a melody.
**Contour**: does it have a shape, with a high point that feels earned and a resting point that feels like rest?
**Rhythm**: is the rhythmic profile of the phrase distinctive enough to recognise even if you transposed every pitch?
**Phrasing**: does it breathe, with phrase lengths that answer each other, or does it run on in undifferentiated eighth notes?
**Development**: when the tune returns, has anything happened to it?

Beware the two default failures.
A line that walks up and down a scale in even notes has contour and rhythm in the technical sense and is not a melody.
A line that is only an arpeggiation of the underlying chords is accompaniment promoted to the top of the texture; it will sound harmonically correct and melodically absent.

## Anchored scale

**2 = there is no melody.**
The top voice is a scale run, a chord arpeggiation, or a random walk with no repeated motif.
Nothing recurs, so nothing can be recognised, and no bar is more important than any other.

**4 = there is a motif but no line.**
A recognisable two-to-four-bar figure exists and repeats, but it repeats unchanged, sits in one narrow register, and never resolves into a longer phrase.
Rhythm is uniform (all eighths, all quarters) so the figure has no profile.
Phrase endings land arbitrarily rather than on a downbeat or a held note.

**6 = a real tune with clear phrasing.**
There is a singable phrase of four to eight bars with an identifiable contour, a mixture of note lengths, and phrase endings that arrive somewhere.
It returns recognisably.
What is missing is development: the second appearance is a copy or a plain transposition, the high point of the phrase is not placed to matter, and the melody's range is comfortable rather than expressive.

**8 = the tune develops.**
The phrase has a distinctive rhythmic profile and a climax placed deliberately, and later appearances are genuinely varied: sequenced, extended, fragmented, reharmonised, or answered by a counter-phrase.
Leaps are prepared and resolved rather than scattered.
The melody's register expands over the piece so that its later statements have more weight than its first.

**10 = the melody is the reason the piece exists.**
Every element of the line earns its place: the contour builds to a single unmistakable high point, the rhythm is memorable independent of pitch, phrases answer each other in periods, and each return is transformed in a way that reveals something the first statement implied.
You could sing it back after one hearing, and the variations would still read as the same tune.

## Between the anchors

A 5 is a tune whose phrasing works but whose rhythm is monotone.
A 7 is a developing tune with one weak return.
A 9 is a 10 whose climax is a bar early or late.

## Reading the evidence

Find the melodic track in the TRACKS block: usually the highest-register non-drum track with a moderate note count.
Read its bars in the NOTES block, where the format is `beat:pitch/duration@velocity`.
`= bar N` means the bar is an exact repeat of bar N in the same track, which is the fastest way to see whether a melody develops or loops: a melodic track that is mostly `= bar N` lines cannot score above 4.
The REPETITION percentage is the whole-piece version of the same signal.
On the piano-roll image, a melody with real contour draws a visibly rising and falling upper edge; a flat horizontal band at the top of the image is a melody stuck in one register.

## What a finding needs

Every finding must carry:

* a bar range (`bar_start` and `bar_end`) and the `track` name the melody is in
* what specifically is wrong with the line there, in melodic terms: contour, rhythm, phrasing, register, or development
* a `suggested_revision` naming a concrete musical change

"Improve the melody" is not a finding.
"The melodic figure in bars 8-15 (lead) repeats bar 8 exactly four times, so the chorus has no lift; on the third and fourth repetitions raise the phrase to start on the fifth and extend the final note by two beats" is a finding.
