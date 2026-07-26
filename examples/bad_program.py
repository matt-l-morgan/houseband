"""A deliberately bad candidate: the judge calibration floor.

Every failure mode we expect from an unguided model, on purpose:

* four bars of material looped sixteen times, no development at all
* one instrument, so no arrangement and no orchestration
* every velocity identical, so no dynamics
* no sections declared
* a single I-IV-V-I with no voice leading
* everything crammed into one octave

A judge that does not rank this far below ``good_program.py`` -- and far below a
real reference recording -- is miscalibrated, and any learning loop built on its
scores would be training on noise. This file exists to catch that before we build
anything on top.
"""

from houseband.house import Score

s = Score(bpm=120, key="C", time_sig=(4, 4))

piano = s.track("piano", patch="grand_piano", pan=0.0)

LOOP = ["C", "F", "G", "C"]

for bar in range(64):
    chord = LOOP[bar % 4]
    # Identical block chord, identical velocity, dead on the grid, every bar.
    piano.chord(bar, 1, symbol=chord, dur=4.0, vel=80, octave=4)

s.write("out.mid")
print(s.summary())
