"""A competent hand-written candidate, used to exercise the pipeline.

Deliberately written the way we want composer agents to write: absolute bar
numbers, sections declared, arrangement built in instrumentation tiers, a bare
opening, a climax in the final third, a gradual tempo lift, and a melody that is
introduced, developed, and paid off rather than merely present.

This is the "good" half of the judge calibration pair. If a judge cannot rank
this above ``bad_program.py``, the rubric is broken and no amount of agent work
will fix it.

The melody deserves a note, because the first version of this file did not have
one and the melody judge correctly scored it 2/10. It had chords and a scattering
of isolated single notes, which is not a tune. The motif below is now stated in
full, answered, fragmented under the bridge, lifted an octave for the climax, and
left hanging in the tag. That is the difference between a piece that has notes in
the top voice and a piece that has a melody.

Nothing here is humanised: velocities are shaped by hand but timing is dead on
the grid. That is intentional. It leaves the mechanical-groove gap for the judges
to find and the coach to close.
"""

from houseband.house import Score

s = Score(bpm=68, key="Am", time_sig=(4, 4))

# Long-form arc: bare intro, two verses that add layers, a bridge that strips
# back, then a full-band climax and a short tag.
s.mark_section("intro", 0, 8)
s.mark_section("verse1", 8, 16)
s.mark_section("verse2", 24, 16)
s.mark_section("bridge", 40, 8)
s.mark_section("climax", 48, 16)
s.mark_section("tag", 64, 4)
TOTAL = 68

# Gradual acceleration into the climax, the way long-form rock tends to move.
s.ramp_tempo(40, 60, 68, 78)

PROG = ["Am", "Am/G", "F", "G"]          # verses: descending bass, modal
BRIDGE = ["F", "C/E", "Dm7", "E7"]        # bridge: brighter, then dominant tension
CLIMAX = ["Am", "F", "C", "G"]            # climax: open, anthemic

# The motif, as (bar offset, pitch, beat, duration). Four bars: it rises a third
# and steps back down, answers itself an octave lower in contour, reaches for a
# new low, then resolves downward. Range is a ninth, which keeps it singable.
MOTIF = [
    (0, 1.0, "A4", 1.5),
    (0, 2.5, "C5", 1.0),
    (0, 3.5, "B4", 0.5),
    (1, 1.0, "C5", 1.0),
    (1, 2.0, "B4", 0.5),
    (1, 2.5, "A4", 1.5),
    (2, 1.0, "F4", 1.0),
    (2, 2.0, "G4", 1.0),
    (2, 3.0, "A4", 1.5),
    (3, 1.0, "G4", 2.0),
    (3, 3.0, "E4", 1.5),
]

# Ornamented answer for the second verse: same skeleton, extra passing notes, so
# it is recognisably the same tune rather than a different one.
ORNAMENTS = [
    (0, 2.0, "B4", 0.5),
    (1, 3.5, "G4", 0.5),
    (2, 3.5, "B4", 0.5),
    (3, 2.5, "F4", 0.5),
]

gtr = s.track("acoustic_gtr", patch="acoustic_guitar", pan=-0.35)
lead = s.track("lead_gtr", patch="overdriven_guitar", pan=0.15)
elec = s.track("rhythm_gtr", patch="clean_guitar", pan=0.40)
bass = s.track("bass", patch="fingered_bass", pan=0.0)
keys = s.track("electric_piano", patch="electric_piano", pan=0.25)
strings = s.track("strings", patch="strings", pan=-0.15)
drums = s.drum_track("drums")


# A natural minor, as pitch classes. Sequencing the motif by scale degree rather
# than by semitone is the difference between a restatement that stays in key and
# one that drags D#, G# and A# across an A minor progression.
SCALE = [9, 11, 0, 2, 4, 5, 7]  # A B C D E F G


def diatonic_shift(pitch: int, degrees: int) -> int:
    """Move a pitch by scale degrees within A natural minor.

    Anything not in the scale is shifted chromatically instead, which keeps
    passing tones intact rather than snapping them onto the nearest degree.
    """
    if degrees == 0:
        return pitch
    pitch_class = pitch % 12
    if pitch_class not in SCALE:
        return pitch + degrees * 2
    index = SCALE.index(pitch_class)
    target = index + degrees
    octaves, wrapped = divmod(target, len(SCALE))
    new_class = SCALE[wrapped]
    # Rebuild from the original octave, correcting for the scale wrapping past B.
    base = pitch - pitch_class
    shifted = base + new_class + 12 * octaves
    # A natural minor starts on A, so pitch classes below A belong to the octave above.
    if new_class < SCALE[0] and pitch_class >= SCALE[0]:
        shifted += 12
    elif new_class >= SCALE[0] and pitch_class < SCALE[0]:
        shifted -= 12
    return shifted


def play_motif(track, start_bar, notes=MOTIF, degrees=0, octaves=0, vel=70, stretch=1.0, gain=0):
    """Place the motif at ``start_bar``, optionally sequenced or stretched.

    Keeping this as one function rather than copying the notes around is the
    point: a restatement stays literally the same material, so development is
    development rather than a second unrelated tune.
    """
    from houseband.house import note_number

    for offset, beat, pitch, dur in notes:
        track.note(
            start_bar + int(offset * stretch),
            beat,
            diatonic_shift(note_number(pitch), degrees) + 12 * octaves,
            dur * stretch,
            vel + gain,
        )


# -- intro: solo fingerpicked guitar, with a hint of the motif to come -----
for bar in range(0, 8):
    chord = PROG[bar % 4]
    # Arpeggio rather than a block chord, rolled slightly for a picked feel.
    gtr.chord(bar, 1, symbol=chord, dur=3.6, vel=52, octave=3, spread=0.18)

# Only the motif's opening gesture, quiet and unaccompanied: a question the
# verses will answer.
gtr.note(6, 3.0, "A4", 1.0, 48)
gtr.note(7, 1.0, "C5", 1.0, 46)
gtr.note(7, 2.5, "B4", 1.5, 44)

# -- verse1: bass and light kit enter, motif stated in full ----------------
for bar in range(8, 24):
    i = bar - 8
    chord = PROG[i % 4]
    gtr.chord(bar, 1, symbol=chord, dur=3.8, vel=58, octave=3, spread=0.15)

    root = ["A1", "G1", "F1", "G1"][i % 4]
    bass.note(bar, 1, root, 2.0, 66)
    bass.note(bar, 3, root, 1.5, 60)

    drums.hit(bar, 1, "kick", 78)
    drums.hit(bar, 3, "snare", 70)
    for beat in (1, 2, 3, 4):
        drums.hit(bar, beat, "hat_closed", 48 if beat % 2 else 40)

# Two full statements, the second a touch louder: the tune establishes itself.
play_motif(lead, 8, vel=62)
play_motif(lead, 12, vel=66)
play_motif(lead, 16, vel=64)
# Fourth pass ends open, on the dominant, to pull into verse 2.
play_motif(lead, 20, vel=68, notes=MOTIF[:9])
lead.note(23, 1.0, "B4", 2.5, 66)

# -- verse2: keys and rhythm guitar thicken it, motif ornamented -----------
for bar in range(24, 40):
    i = bar - 24
    chord = PROG[i % 4]
    gtr.chord(bar, 1, symbol=chord, dur=3.8, vel=62, octave=3, spread=0.12)
    keys.chord(bar, 1, symbol=chord, dur=3.6, vel=54, octave=4)
    elec.note(bar, 2.5, ["C4", "B3", "A3", "B3"][i % 4], 1.5, 52)

    root = ["A1", "G1", "F1", "G1"][i % 4]
    bass.note(bar, 1, root, 1.5, 72)
    bass.note(bar, 2.5, root, 0.5, 62)
    bass.note(bar, 4, ["E2", "D2", "C2", "D2"][i % 4], 0.5, 66)

    drums.hit(bar, 1, "kick", 84)
    drums.hit(bar, 2.5, "kick", 70)
    drums.hit(bar, 3, "snare", 78)
    for beat in (1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5):
        drums.hit(bar, beat, "hat_closed", 52 if beat == int(beat) else 38)
    if i % 8 == 7:
        drums.hit(bar, 4, "crash", 92)

for start in (24, 32):
    play_motif(lead, start, vel=72)
    play_motif(lead, start, notes=ORNAMENTS, vel=58)
# Second half climbs: same shape a third higher, reaching for the bridge.
play_motif(lead, 28, degrees=2, vel=74)
play_motif(lead, 36, degrees=2, vel=76)

# -- bridge: strip back to keys and strings, no drums, motif augmented ----
for bar in range(40, 48):
    i = bar - 40
    chord = BRIDGE[i % 4]
    keys.chord(bar, 1, symbol=chord, dur=3.9, vel=50, octave=4)
    strings.chord(bar, 1, symbol=chord, dur=3.9, vel=44, octave=5)
    bass.note(bar, 1, ["F1", "E1", "D1", "E1"][i % 4], 3.5, 58)

# Only the first two bars of the motif, stretched to twice the length over new
# harmony. Recognisable, but suspended rather than resolved.
play_motif(strings, 40, notes=MOTIF[:6], stretch=2.0, vel=52)
play_motif(strings, 44, notes=MOTIF[:6], stretch=2.0, degrees=-1, vel=48)

# -- climax: everything, motif an octave up, doubled ---------------------
for bar in range(48, 64):
    i = bar - 48
    chord = CLIMAX[i % 4]
    swell = min(1.0, i / 12.0)
    gtr.chord(bar, 1, symbol=chord, dur=3.8, vel=int(66 + 14 * swell), octave=3, spread=0.1)
    elec.chord(bar, 1, symbol=chord, dur=3.8, vel=int(64 + 14 * swell), octave=3)
    keys.chord(bar, 1, symbol=chord, dur=3.6, vel=int(58 + 12 * swell), octave=4)
    strings.chord(bar, 1, symbol=chord, dur=3.9, vel=int(52 + 16 * swell), octave=5)

    root = ["A1", "F1", "C2", "G1"][i % 4]
    bass.note(bar, 1, root, 1.0, int(78 + 10 * swell))
    bass.note(bar, 2, root, 0.5, int(68 + 8 * swell))
    bass.note(bar, 3, root, 1.0, int(74 + 8 * swell))
    bass.note(bar, 4, root, 0.5, int(66 + 8 * swell))

    drums.hit(bar, 1, "kick", int(92 + 8 * swell))
    drums.hit(bar, 2.5, "kick", 78)
    drums.hit(bar, 3, "snare", int(86 + 8 * swell))
    drums.hit(bar, 4.5, "snare", 64)
    for beat in (1, 2, 3, 4):
        drums.hit(bar, beat, "ride", 58)
    if i % 4 == 0:
        drums.hit(bar, 1, "crash", 100)

# The payoff: the tune at its highest, doubled by strings an octave below so it
# reads as one big line rather than two competing ones.
for start, gain in ((48, 0), (52, 4), (56, 6), (60, 8)):
    play_motif(lead, start, octaves=1, vel=80, gain=gain)
    play_motif(strings, start, vel=60, gain=gain)

# -- tag: back to one guitar, the motif left unresolved -------------------
for bar in range(64, TOTAL):
    gtr.chord(bar, 1, symbol="Am", dur=3.9, vel=44, octave=3, spread=0.25)

# Opening gesture only, and it stops before the resolution. The piece ends on a
# question, which is why the last chord can be quiet without feeling unfinished.
gtr.note(64, 1.0, "A4", 1.5, 46)
gtr.note(64, 2.5, "C5", 1.0, 44)
gtr.note(65, 1.0, "B4", 3.0, 42)
strings.chord(TOTAL - 1, 1, symbol="Am", dur=4.0, vel=36, octave=5)

s.write("out.mid")
print(s.summary())
