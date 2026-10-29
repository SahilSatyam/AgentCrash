"""Phase 0 smoke: the package imports and exposes its version."""

import agentcrash


def test_version_is_set() -> None:
    assert agentcrash.__version__
