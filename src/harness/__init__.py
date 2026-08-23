"""swe-task-harness: containerized SWE-bench-style task packaging, execution, and grading."""

__version__ = "0.1.0"

# Bumped whenever environment-setup logic changes in a way that must invalidate
# cached BASE snapshots. Part of the environment cache key (see docs/DECISIONS.md #12).
ENV_SETUP_VERSION = 1
