"""Structural criteria: the shared, non-negotiable shape of a deliverable clip.

Every composer is briefed against these and every judge scores against them, so
they are the one place where "what a usable clip is" is written down. They are
deliberately deterministic rather than model-generated: the same brief must
produce the same criteria on every run, or a rising score across rounds could be
the criteria drifting rather than the music improving.

This module replaced an LLM analyst that derived criteria from a reference
recording. That approach had a failure mode worth recording, because it was
invisible until it bit: the criteria were cached per reference file, a run that
asked for no reference silently adopted whichever file sorted first in
``references/``, and a request for a house loop was therefore briefed against a
transcription of a six-minute rock song. Every composer was told to build toward
a climax in the final third, and the judges then marked the clips down for not
having one. Deriving the criteria from the brief instead cannot drift that way.

What is specified here is *shape*, never content. Key, tempo, chords, melody and
instrumentation are the composer's to choose, and the criteria say so explicitly:
a criterion that constrains material would make three teams converge, which
defeats the point of running them against each other.
"""

from __future__ import annotations

from houseband import config as cfg
from houseband.types import Brief

# Criteria that hold for any clip, whatever the genre. Each one names a failure
# mode seen in real output rather than a musical ideal: unusable clips fail on
# these, and good ones satisfy them without being told how.
_UNIVERSAL = (
    "State the central rhythmic or melodic idea within the first two bars. A "
    "producer auditions a clip by dropping it on a timeline, and decides inside "
    "two bars.",
    "The last bar must lead back into the first without a seam. Nothing may "
    "sound past the final bar except a short release tail.",
    "Do not write an intro, a breakdown or an ending. A clip is one continuous "
    "idea, and a section that only makes sense in a longer arrangement is "
    "material the producer has to delete before they can use it.",
    "Vary velocity across repeats. Identical velocities on every pass are the "
    "single clearest tell of programmed rather than played music.",
    "Leave the register a lead vocal occupies mostly clear, roughly C4 to C6. "
    "Anything competing there is the first thing deleted.",
    "Keep the parts separable. A producer keeps some stems and bins others, so "
    "each track has to make sense alone.",
    "Use the full register rather than confining every part to one octave.",
)

# Genre-specific pressure, keyed on a substring of the genre the brief names.
# Matched loosely and on purpose: the brief's genre is model-extracted free text,
# so "deep house" and "house music" both have to land on the house entry. Order
# matters, since the first match wins and some names contain others.
_BY_GENRE: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("drum and bass", "drum n bass", "dnb", "jungle", "breakcore"),
        (
            "Put the weight on a two-bar break rather than a four-on-the-floor "
            "pulse, and let the sub bass carry the harmony.",
            "The kick and snare placement is the hook. Treat everything else as "
            "support for it.",
        ),
    ),
    (
        ("house", "techno", "tech-house", "garage", "disco"),
        (
            "Keep a steady four-on-the-floor pulse, and make the interest come "
            "from what moves against it rather than from the kick.",
            "Give the bass and the chords different rhythmic subdivisions so "
            "they interlock instead of doubling each other.",
        ),
    ),
    (
        ("hip hop", "hip-hop", "boom bap", "boom-bap", "trap", "lo-fi", "lofi"),
        (
            "The drums should sit slightly off a rigid grid. Perfectly "
            "quantised hats and snares are what makes a beat sound programmed.",
            "Leave the middle of the mix open. This is a bed for a vocal, not a "
            "finished arrangement.",
        ),
    ),
    (
        ("rock", "metal", "punk", "grunge", "indie"),
        (
            "Build the clip on a riff that a guitarist would recognise as a "
            "riff, and lock the drums and bass to its accents.",
            "Let the kit play rather than pulse: fills and dynamic variation "
            "are what separate a played take from a drum machine.",
        ),
    ),
    (
        ("ambient", "drone", "cinematic", "score", "soundtrack"),
        (
            "Rhythm may be implied rather than stated, but the harmony still has "
            "to move over the length of the clip.",
            "Movement can come from timbre and register rather than from notes, "
            "but something must change between the first and last bar.",
        ),
    ),
    (
        ("jazz", "soul", "funk", "rnb", "r&b", "neo-soul"),
        (
            "Use extended harmony and voice-lead it: parallel block chords are "
            "the failure mode here.",
            "The groove should push and pull against the beat rather than "
            "sitting exactly on it.",
        ),
    ),
)


def _genre_lines(genre: str) -> tuple[str, ...]:
    lowered = (genre or "").strip().lower()
    if not lowered:
        return ()
    for names, lines in _BY_GENRE:
        if any(name in lowered for name in names):
            return lines
    return ()


def for_brief(brief: Brief, profile: cfg.SnippetProfile | None = None) -> str:
    """The structural criteria for one brief, as markdown.

    Deterministic: the same brief and clip length always produce the same text.
    That is what makes a score comparable across rounds, and it is why this is
    not a model call.
    """
    profile = profile or cfg.profile_for()
    genre = (brief.genre or "").strip()
    specific = _genre_lines(genre)

    heading = f"# Structural criteria{f' ({genre})' if genre else ''}"
    lines = [
        heading,
        "",
        f"The deliverable is a {profile.bars}-bar loopable clip, "
        f"{profile.approx_seconds}, that a producer imports into a DAW and "
        "builds on. These are the shared targets every take is judged against.",
        "",
        "## Required",
        "",
    ]
    lines += [f"- {line}" for line in _UNIVERSAL]
    if specific:
        lines += ["", f"## For {genre}", ""]
        lines += [f"- {line}" for line in specific]
    lines += [
        "",
        "## Deliberately not specified",
        "",
        "Key, tempo, chord progression, melodic material and instrumentation are "
        "yours to choose. Criteria that pinned those down would make every team "
        "hand in the same clip, and the point of running several is to get "
        "genuinely different starting points.",
        "",
    ]
    return "\n".join(lines)
