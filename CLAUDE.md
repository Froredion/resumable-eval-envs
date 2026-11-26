# resumable-evals

Read `docs/DESIGN.md` before changing behaviour; it is the source of truth for record/rebuild semantics.

- Python, managed with uv: `uv sync`, `uv run pytest`, `uv run ruff check .`. No runtime dependencies in the core modules.
- The record path must stay crash-safe: every event is appended and fsynced before the call it records returns to the caller.
- Never relax result comparison to make a rebuild pass. A mismatch is a divergence to flag, or a volatile field to add to normalization rules, with a test.
- Determinism controls (clock, seeds) apply to the original run as well as the rebuild, or recorded timestamps cannot match.
- Contributor rules for humans are in `CONTRIBUTING.md`; keep the two in sync.
