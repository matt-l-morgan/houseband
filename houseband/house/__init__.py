"""The house library that composer agents write against.

``core`` is hand-written and stable. ``learned`` grows over a run: when the
judges keep flagging the same weakness, the coach stages a new helper there and
every composer gains it. Both are re-exported here so a composer only ever
needs ``from houseband.house import ...``.
"""

from houseband.house.core import (  # noqa: F401
    DRUMS,
    GM,
    DrumTrack,
    Score,
    Section,
    Track,
    chord_pitches,
    note_number,
)
from houseband.house.learned import *  # noqa: F401,F403
from houseband.house.learned import __all__ as _learned_all

__all__ = [
    "Score",
    "Track",
    "DrumTrack",
    "Section",
    "note_number",
    "chord_pitches",
    "DRUMS",
    "GM",
    *_learned_all,
]
