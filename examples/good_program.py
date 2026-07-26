"""A competent hand-written candidate, used to exercise the pipeline.

Deliberately written the way we want composer agents to write: absolute bar
numbers, sections declared, arrangement built in instrumentation tiers, a bare
opening, a climax in the final third, and a gradual tempo lift. This is the
"good" half of the judge calibration pair -- if a judge cannot rank this above
``bad_program.py``, the rubric is broken and no amount of agent work will fix it.

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

gtr = s.track("acoustic_gtr", patch="acoustic_guitar", pan=-0.35)
elec = s.track("electric_gtr", patch="overdriven_guitar", pan=0.40)
bass = s.track("bass", patch="fingered_bass", pan=0.0)
keys = s.track("electric_piano", patch="electric_piano", pan=0.25)
strings = s.track("strings", patch="strings", pan=-0.15)
drums = s.drum_track("drums")

# -- intro: solo fingerpicked guitar, nothing else -------------------------
for bar in range(0, 8):
    chord = PROG[bar % 4]
    # Arpeggio rather than a block chord, rolled slightly for a picked feel.
    gtr.chord(bar, 1, symbol=chord, dur=3.6, vel=52, octave=3, spread=0.18)
    gtr.note(bar, 3.5, "E4" if bar % 2 == 0 else "C4", 0.5, 46)

# -- verse1: bass and light kit enter ---------------------------------------
for bar in range(8, 24):
    i = bar - 8
    chord = PROG[i % 4]
    gtr.chord(bar, 1, symbol=chord, dur=3.8, vel=58, octave=3, spread=0.15)
    gtr.note(bar, 3, "A4" if i % 4 < 2 else "G4", 1.0, 54)

    root = ["A1", "G1", "F1", "G1"][i % 4]
    bass.note(bar, 1, root, 2.0, 66)
    bass.note(bar, 3, root, 1.5, 60)

    drums.hit(bar, 1, "kick", 78)
    drums.hit(bar, 3, "snare", 70)
    for beat in (1, 2, 3, 4):
        drums.hit(bar, beat, "hat_closed", 48 if beat % 2 else 40)

# -- verse2: keys and a second guitar thicken it ---------------------------
for bar in range(24, 40):
    i = bar - 24
    chord = PROG[i % 4]
    gtr.chord(bar, 1, symbol=chord, dur=3.8, vel=62, octave=3, spread=0.12)
    keys.chord(bar, 1, symbol=chord, dur=3.6, vel=54, octave=4)
    elec.note(bar, 2.5, ["C5", "B4", "A4", "B4"][i % 4], 1.5, 58)

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

# -- bridge: strip back to keys and strings, no drums ---------------------
for bar in range(40, 48):
    i = bar - 40
    chord = BRIDGE[i % 4]
    keys.chord(bar, 1, symbol=chord, dur=3.9, vel=50, octave=4)
    strings.chord(bar, 1, symbol=chord, dur=3.9, vel=44, octave=5)
    bass.note(bar, 1, ["F1", "E1", "D1", "E1"][i % 4], 3.5, 58)

# -- climax: everything, highest register, loudest ------------------------
for bar in range(48, 64):
    i = bar - 48
    chord = CLIMAX[i % 4]
    swell = min(1.0, i / 12.0)
    gtr.chord(bar, 1, symbol=chord, dur=3.8, vel=int(66 + 14 * swell), octave=3, spread=0.1)
    elec.chord(bar, 1, symbol=chord, dur=3.8, vel=int(70 + 16 * swell), octave=4)
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

# -- tag: back to one guitar, resolving -----------------------------------
for bar in range(64, TOTAL):
    gtr.chord(bar, 1, symbol="Am", dur=3.9, vel=44, octave=3, spread=0.25)
strings.chord(TOTAL - 1, 1, symbol="Am", dur=4.0, vel=36, octave=5)

s.write("out.mid")
print(s.summary())
