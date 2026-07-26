# The house library

You write a Python program that builds a score and writes it to `out.mid`.
This is the complete API available to you.

Everything is placed in **musical time**: an absolute bar number plus a beat within that bar.
The library converts to seconds once, correctly, so you never do timing arithmetic and timing drift is impossible.

## Skeleton

```python
from houseband.house import Score

s = Score(bpm=72, key="Am", time_sig=(4, 4))

s.mark_section("intro", 0, 8)          # name, start_bar, length_in_bars
s.mark_section("verse", 8, 16)

gtr = s.track("acoustic_gtr", patch="acoustic_guitar", pan=-0.3)
drums = s.drum_track("drums")

for bar in range(8):
    gtr.chord(bar, 1, symbol="Am7", dur=3.5, vel=58)

s.write("out.mid")                      # required, exactly this filename
```

## Conventions

- `bar` is absolute and 0-indexed across the whole song.
- `beat` is 1-indexed, the way musicians count: 1, 2, 3, 4.
- `dur` is measured in beats. Fractional values are fine (`0.5` is an eighth note in 4/4).
- `vel` is MIDI velocity, 1 to 127. This is your dynamics control.
- `pan` runs -1.0 (hard left) through 0.0 (centre) to 1.0 (hard right).

## `Score`

```python
Score(bpm=120.0, key="C", time_sig=(4, 4))
```

| Method | Purpose |
| --- | --- |
| `s.mark_section(name, start_bar, bars)` | Label a span of bars. Judges read these to reason about form, so declare them. |
| `s.track(name, patch=0, pan=0.0)` | Add an instrument. `patch` takes a GM number or a name (see below). |
| `s.drum_track(name="drums", pan=0.0)` | Add a percussion track (GM channel 10). |
| `s.tempo(bar, bpm)` | Change tempo from `bar` onward. |
| `s.ramp_tempo(start_bar, end_bar, start_bpm, end_bpm)` | Step tempo linearly, bar by bar. Useful for gradual acceleration. |
| `s.write("out.mid")` | Write the file. Required as the last line. |
| `s.summary()` | Returns a text summary. Printing it is a good sanity check. |

## `Track`

```python
t.note(bar, beat, pitch, dur, vel=72)
```
One note. `pitch` accepts a MIDI number or a name: `"C4"` is middle C, also `"F#3"`, `"Bb2"`, `"A"` (defaults to octave 4).

```python
t.chord(bar, beat, symbol="Am7", dur=1.0, vel=72, octave=3, spread=0.0)
t.chord(bar, beat, pitches=["A3", "C4", "E4"], dur=1.0, vel=72)
```
Several notes at once. Pass **either** `symbol` **or** `pitches`, not both.
`octave` places the chord root. `spread` offsets each voice by that many beats, which turns a block chord into a strum or roll.

```python
drums.hit(bar, beat, "kick", vel=96, dur=0.25)
```
Drum tracks only. Named hits, listed below.

All three return the track, so calls chain.

## Chord symbols

Root plus quality, with slash chords supported: `"Am"`, `"F#m9"`, `"Cmaj7"`, `"G7sus4"`, `"C/G"`, `"Am/G"`.

Recognised qualities: `maj` (or empty), `m`, `min`, `-`, `5`, `dim`, `aug`, `sus2`, `sus4`, `6`, `m6`, `7`, `maj7`, `M7`, `m7`, `mmaj7`, `m7b5`, `dim7`, `7sus4`, `add9`, `madd9`, `9`, `maj9`, `m9`, `69`, `7b9`, `7#9`, `7b5`, `7#5`, `7#11`, `11`, `m11`, `13`, `m13`.

An unrecognised quality raises an error, so stick to this list.

## Instrument names for `patch`

`grand_piano`, `electric_piano`, `harpsichord`, `vibraphone`, `organ`, `nylon_guitar`, `acoustic_guitar`, `jazz_guitar`, `clean_guitar`, `overdriven_guitar`, `distorted_guitar`, `acoustic_bass`, `fingered_bass`, `picked_bass`, `fretless_bass`, `violin`, `cello`, `strings`, `choir`, `trumpet`, `trombone`, `sax`, `flute`, `recorder`, `square_lead`, `saw_lead`, `warm_pad`, `sweep_pad`.

Any General MIDI program number 0 to 127 also works if you want something not in this list.

## Drum names for `hit`

`kick`, `kick2`, `snare`, `snare_rim`, `snare2`, `clap`, `hat` / `hat_closed`, `hat_pedal`, `hat_open`, `ride`, `ride_bell`, `crash`, `splash`, `china`, `tom_low`, `tom_mid`, `tom_high`, `cowbell`, `tambourine`, `shaker`.

## Helpers

`from houseband.house import note_number, chord_pitches, GM, DRUMS`

- `note_number("F#3")` gives the MIDI number.
- `chord_pitches("Am7", octave=3)` gives the note numbers, if you want to manipulate voicings yourself.
- `GM` and `DRUMS` are the name-to-number maps.

The library may also expose helpers added during earlier rounds in response to judge feedback.
If any exist, they will be listed in your task prompt.

## Instruments have playable ranges

A submission is rejected outright if a part sits far outside its instrument's range.
Approximate practical ranges: bass 28 to 67, guitar 40 to 88, piano 21 to 108, strings 36 to 96, brass 34 to 94, flute and recorder 60 to 103.
Writing a bass line an octave too high is the most common way to fail this check.

## What you may import

Only `houseband.house`, plus `math`, `random`, `itertools`, `functools`, `collections`, `dataclasses`, `typing`, `statistics`, `copy`, `enum`, `fractions`.
Anything else is rejected before the program runs.
File and network access are not available, and `s.write("out.mid")` is the only output you need.
