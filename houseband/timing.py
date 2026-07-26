"""Seconds-to-musical-time conversion.

MIDI stores absolute seconds; every part of this system that reasons about music
wants bars and beats. Doing that conversion in one place, from the tempo map the
composer actually declared, is what keeps a judge's "bars 81-96" anchor pointing
at the same music the composer wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TempoMap:
    """Piecewise-constant tempo, keyed by starting bar."""

    entries: list[tuple[int, float]] = field(default_factory=lambda: [(0, 120.0)])
    quarters_per_bar: float = 4.0
    beats_per_bar: int = 4

    # -- constructors ------------------------------------------------------

    @classmethod
    def from_structure(cls, structure: dict) -> "TempoMap":
        num, den = structure.get("time_sig", [4, 4])
        raw = structure.get("tempo_map") or [[0, 120.0]]
        return cls(
            entries=sorted((int(b), float(v)) for b, v in raw),
            quarters_per_bar=num * 4.0 / den,
            beats_per_bar=int(num),
        )

    @classmethod
    def from_midi(cls, midi) -> "TempoMap":
        """Best effort when no structural sidecar exists (a reference MIDI).

        Uses the file's own tempo changes, mapped onto bars via a constant
        4/4 assumption. Approximate by nature, which is why the sidecar path is
        preferred wherever we control the writer.
        """
        try:
            times, tempi = midi.get_tempo_changes()
        except Exception:
            return cls()
        if len(tempi) == 0:
            return cls()

        beats_per_bar = 4
        try:
            if midi.time_signature_changes:
                ts = midi.time_signature_changes[0]
                beats_per_bar = int(ts.numerator)
                quarters = ts.numerator * 4.0 / ts.denominator
            else:
                quarters = 4.0
        except Exception:
            quarters = 4.0

        # Convert each tempo change's time into a bar index under the tempo that
        # preceded it, accumulating as we go.
        entries: list[tuple[int, float]] = [(0, float(tempi[0]))]
        elapsed = 0.0
        bar = 0
        for t, bpm in zip(times[1:], tempi[1:]):
            bar_len = quarters * 60.0 / entries[-1][1]
            while elapsed + bar_len <= t + 1e-9:
                elapsed += bar_len
                bar += 1
                bar_len = quarters * 60.0 / entries[-1][1]
            entries.append((bar, float(bpm)))
        return cls(entries=entries, quarters_per_bar=quarters, beats_per_bar=beats_per_bar)

    # -- queries -----------------------------------------------------------

    def bpm_at(self, bar: int) -> float:
        bpm = self.entries[0][1]
        for start_bar, value in self.entries:
            if start_bar <= bar:
                bpm = value
            else:
                break
        return bpm

    def bar_seconds(self, bar: int) -> float:
        return self.quarters_per_bar * 60.0 / self.bpm_at(bar)

    def bar_start_seconds(self, bar: int) -> float:
        total = 0.0
        for b in range(bar):
            total += self.bar_seconds(b)
        return total

    # Notes written exactly on a bar line accumulate float error on the way
    # through seconds, and can land a hair *before* the boundary. Without a
    # tolerance that shows up as "bar 7, beat 5" instead of "bar 8, beat 1",
    # which would put every judge's bar citation one bar off the music the
    # composer actually wrote. A millisecond is far below any musical
    # distinction, so snapping is free.
    BOUNDARY_EPSILON = 1e-3

    def seconds_to_bar(self, seconds: float, max_bars: int = 8192) -> float:
        """Fractional bar position. Bar 0 starts at 0.0."""
        elapsed = 0.0
        for bar in range(max_bars):
            length = self.bar_seconds(bar)
            if elapsed + length > seconds + self.BOUNDARY_EPSILON:
                offset = max(0.0, seconds - elapsed)
                return bar + min(offset / length, 1.0)
            elapsed += length
        return float(max_bars)

    def seconds_to_bar_beat(self, seconds: float) -> tuple[int, float]:
        """Return ``(bar, beat)`` with ``beat`` 1-indexed, the way musicians count.

        ``beat`` is guaranteed to satisfy ``1 <= beat < beats_per_bar + 1``.
        """
        position = self.seconds_to_bar(seconds)
        bar = int(position)
        beat = 1.0 + (position - bar) * self.beats_per_bar
        # Snap a value sitting a hair under the next downbeat onto it.
        if beat >= self.beats_per_bar + 1 - 1e-6:
            return bar + 1, 1.0
        return bar, beat

    def beats_to_seconds(self, bar: int, beats: float) -> float:
        """Duration in beats starting at ``bar``, expressed in seconds.

        Approximate across a tempo change, which is acceptable: this is used for
        reporting note lengths, not for placing them.
        """
        return beats * (self.bar_seconds(bar) / self.beats_per_bar)
