"""Helpers the coach has added in response to judge feedback.

This file starts almost empty on purpose. It is the capability half of the
learning loop: when judges repeatedly flag the same weakness across rounds, the
coach stages a function here (with a test) rather than just writing another line
of advice into a playbook. Advice has to be re-read and re-applied every round;
a function is permanent leverage that every composer inherits.

Deliberately absent at the start: anything to do with humanising velocity or
timing. Judges reliably flag mechanical, dead-on-the-grid rhythm in early
rounds, and watching the coach close that specific gap on its own is the
clearest demonstration of the loop working. Do not pre-empt it here.

Everything in ``__all__`` is re-exported from ``houseband.house``.
"""

from __future__ import annotations

__all__: list[str] = []
