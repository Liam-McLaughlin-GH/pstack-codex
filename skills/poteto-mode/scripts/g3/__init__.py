"""Offline helper tooling adapted for the Codex pstack port.

The modules in this package deliberately stop at local policy and bookkeeping
boundaries.  Provider, stack, activity, and clock inputs are explicit so the
same behavior can be exercised without a network, agent runtime, merge, or
worktree cleanup.
"""

from .watch import (  # noqa: F401
    Check,
    CheckRead,
    DeterministicClock,
    FakeReader,
    JsonProviderAdapter,
    PollOptions,
    PRContext,
    PRFacts,
    QueryFailure,
    UnknownProviderData,
    classify_pr,
    read_snapshot,
    run_queued,
    run_simple,
    select_stack_decision,
)
