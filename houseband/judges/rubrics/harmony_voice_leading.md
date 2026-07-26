# Harmony and voice leading

Do the chords mean anything, and do the voices move like players rather than like a chord chart?

Two separate questions live here and both must be answered.

**Harmony** is the chord succession: does it establish a key, move away from it, and return in a way that creates and discharges tension?
A progression is not good because it is complex or bad because it is simple.
I-V-vi-IV is a fine progression; it is a weak one if it loops for four minutes with no cadence, no substitution, and no change of harmonic rhythm.

**Voice leading** is how the individual lines get from one chord to the next.
The test is whether each voice moves the shortest sensible distance, keeps common tones, resolves its tendency tones, and stays out of the way of the others.
The audible symptoms of bad voice leading are: every chord in root position at the same inversion, so the bass jumps in parallel with the top; parallel fifths and octaves between outer voices; a leading tone that leaps down instead of resolving up; and inner voices that jump a sixth when a step was available.

Watch specifically for these failures, which machine-composed harmony produces constantly.
**Block-chord syndrome**: all chord tones struck together in root position, every bar, so there are no voices at all, only stacks.
**Nonfunctional shuffling**: a set of diatonic chords in an order that never implies a cadence, so the harmony has no direction even though every chord is in key.
**No harmonic rhythm**: exactly one chord per bar for the whole piece, so harmony never accelerates into a cadence or relaxes into a plateau.
**Modal ambiguity by accident**: the key is unclear not as a choice but because no cadence ever confirms it.

## Anchored scale

**2 = the harmony is broken or absent.**
Chords are unrelated to each other or to any key, there are audible wrong notes (a major seventh clashing with the melody's tonic, unresolved dissonance held under a cadence), or the harmony is a single chord for the whole piece.
Parallel octaves and fifths between the outer voices throughout.

**4 = correct but mechanical.**
The chords are diatonic and consistent with a key, but they are all root-position blocks, the progression loops without a cadence, harmonic rhythm is exactly one chord per bar, and voices leap wherever the chord tones happen to be.
Nothing is wrong; nothing moves.

**6 = functional harmony with some voice leading.**
The key is established and confirmed by at least one real cadence, the progression has a direction (a pre-dominant, a dominant, a resolution), and inversions are used so the bass line has some shape of its own.
Common tones are mostly held.
What is missing is variety: harmonic rhythm is uniform, there are no substitutions or borrowed chords where the form invites them, and inner voices are correct but characterless.

**8 = harmony that shapes the form.**
Harmonic rhythm varies deliberately, accelerating into cadences and relaxing in stable sections.
There is at least one convincing non-diatonic move: a secondary dominant, a borrowed chord, a deceptive cadence, a pivot to a related key that is prepared and resolved.
Voice leading is smooth in the inner parts, tendency tones resolve, and the bass line is a line rather than a series of chord roots.

**10 = the harmony is the argument of the piece.**
Every progression choice serves the form: the key is established, genuinely departed from, and returned to with a resolution that feels inevitable in hindsight.
Voice leading is idiomatic for the instruments, with independent inner lines you could follow individually, correct resolution of every tendency tone, and no unintended parallels.
Substitutions and reharmonisations are used where they reveal something about the melody rather than to demonstrate that they can be.

## Between the anchors

A 5 has one real cadence and otherwise mechanical blocks.
A 7 has good harmonic rhythm but at least one clumsy voice-leading moment you can name.
A 9 is a 10 with a single weak resolution.

## Reading the evidence

The HARMONY block gives a detected chord per bar, marked approximate.
It is derived from a template match over sounding pitch classes, so treat it as a strong hint rather than ground truth, and check it against the actual notes for any bar you intend to criticise.
A long run of the same symbol means static harmony; a run of `-` means the harmony was too ambiguous to detect, which is itself evidence.
For voice leading you must read the NOTES block: compare the pitches sounding on consecutive downbeats in each accompaniment track and see how far each voice moved.
On the piano-roll image, block-chord syndrome looks like identical vertical stacks repeating; real voice leading looks like near-horizontal lines with occasional steps.

## What a finding needs

Every finding must carry:

* a bar range (`bar_start` and `bar_end`) and, for a voice-leading problem, the `track` name
* the specific harmonic or contrapuntal fault, named: which chord, which voice, which interval, which unresolved tone
* a `suggested_revision` giving the concrete replacement

"Weak harmony" is not a finding.
"Bars 24-31 (pads) repeat the same root-position Am-F-C-G with one chord per bar and no cadence, so the section never arrives; put the F in first inversion and replace the last G with G7 resolving to Am on the downbeat of bar 32" is a finding.
