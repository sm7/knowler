# Architecture Deviations

This file records any deliberate deviations from the architecture document, with rationale.

---

## D001 — Python version: 3.12 → 3.9+ compatible code

**Architecture says:** Python 3.12
**Deviation:** Code is written to be compatible with Python 3.10+ (uses `match` statements which require 3.10). The pyproject.toml specifies `requires-python = ">=3.10"`.
**Reason:** The packaged distribution targets modern Python runtimes published through PyPI. During development on machines that only have 3.9 system Python, developers should install 3.10+ via pyenv or uv. The dev_setup.md documents this requirement.
**Impact:** None on production. Dev setup docs are clear.

---

## D002 — No deviations recorded yet

All other implementation choices are covered by the assumptions file (docs/implementation_assumptions.md) and represent the smallest reasonable interpretation of architecture constraints, not deviations from them.
