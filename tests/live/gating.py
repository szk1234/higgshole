"""The opt-in predicate for the one paid test in this repository.

It lives in its own module so the gate itself can be asserted offline, without
importing (and therefore without risking running) the live test.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

LIVE_TESTS_ENV = "HIGGSHOLE_LIVE_TESTS"


def live_tests_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the operator has opted in to billable tests.

    Any non-empty value enables them; an unset or empty variable does not.
    """
    env = os.environ if environ is None else environ
    return bool(env.get(LIVE_TESTS_ENV, "").strip())
