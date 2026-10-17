"""Final knowledge-loop DB tests reuse the throwaway-PostgreSQL fixture of tests/pitch_brief (real import path, no provider call).

Needs PITCH_BRIEF_TEST_DSN or the default loopback database (skipped, which is NOT a pass, when none answers)."""
from tests.pitch_brief.conftest import env  # noqa: F401  (re-exported fixture)
