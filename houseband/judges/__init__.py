"""The judge panel.

Three judges of different kinds, deliberately:

* :mod:`~houseband.judges.rubric` scores each candidate on eight dimensions
  against anchored rubrics, which is what produces actionable findings.
* :mod:`~houseband.judges.pairwise` asks which of two pieces is better, in both
  presentation orders, which produces a ranking that absolute scores cannot.
* :mod:`~houseband.judges.elo` turns those comparisons into ratings on a stable
  scale by pinning the human reference.

And one gate: :mod:`~houseband.judges.calibration` checks that the reference
actually out-scored the agents before anything is learned from a round.

Ranking is not the only way to choose what to keep, and for starters it is the
wrong one: :mod:`~houseband.judges.diversity` selects a spread of usable takes
without an LLM in the loop, because a producer wants six different ideas rather
than one winner.
"""

from houseband.judges.calibration import (
    STRUCTURAL_DIMENSIONS,
    Breach,
    CalibrationReport,
    check_calibration,
)
from houseband.judges.diversity import (
    DESCRIPTOR_KEYS,
    DESCRIPTOR_WEIGHTS,
    descriptors,
    distance,
    diversity_matrix,
    mean_distance,
    niche_coverage,
    niche_of,
    select_varied,
)
from houseband.judges.elo import (
    DEFAULT_K,
    DEFAULT_RATING,
    REFERENCE_RATING,
    expected,
    run_ratings,
    update,
)
from houseband.judges.pairwise import compare, tournament
from houseband.judges.rubric import (
    RUBRIC_DIR,
    judge_candidate,
    judge_dimension,
    load_rubric,
    missing_rubrics,
    missing_rubrics_for_mode,
    run_panel,
)

__all__ = [
    # rubric panel
    "RUBRIC_DIR",
    "judge_dimension",
    "judge_candidate",
    "run_panel",
    "load_rubric",
    "missing_rubrics",
    "missing_rubrics_for_mode",
    # pairwise
    "compare",
    "tournament",
    # elo
    "DEFAULT_RATING",
    "REFERENCE_RATING",
    "DEFAULT_K",
    "expected",
    "update",
    "run_ratings",
    # calibration
    "STRUCTURAL_DIMENSIONS",
    "Breach",
    "CalibrationReport",
    "check_calibration",
    # diversity
    "DESCRIPTOR_KEYS",
    "DESCRIPTOR_WEIGHTS",
    "descriptors",
    "distance",
    "diversity_matrix",
    "mean_distance",
    "niche_of",
    "niche_coverage",
    "select_varied",
]
