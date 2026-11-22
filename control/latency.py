"""Stop latency: from the evidence that the user wants to stop, to torque off.

This module only does arithmetic on timestamps that the caller collected. It
reads no clock, opens no socket and sends nothing. Every timestamp passed in
must come from the same clock, in milliseconds (fractions allowed).

Two kinds of stop are measured, each split into four parts so the total can
be explained:

Cued stop (the user's Stop cue while the chair is moving)

    t0  capture time of the FIRST frame whose stop channel was past the stop
        threshold, in the run of frames that produced the cue
    t1  the tier reader returned the Stop cue
    t2  the task controller returned its request for that tick (Hold, so no
        MotionRequest: the publisher's default stop)
    t3  the stop datagram was handed to the transport
    t4  the receiver reported torque off after handling that datagram

Signal-loss stop (frame missing, not actionable or stale while moving)

    t0  capture time of the LAST actionable frame before the loss
    t1  the task controller began the tick that found the loss (the loss
        check is the first thing it does)
    t2..t4 as above

t0 -> t1 for a cued stop includes the detector's inference time and the
sustained-crossing rule: a crossing needs two consecutive frames past the
threshold (control/tiers.py, MIN_SUSTAINED_FRAMES), so at least one frame
interval is spent by design. For a camera that stops delivering, t0 -> t1
includes the 250 ms staleness limit (control/publisher.py,
PERCEPTION_MAX_AGE_MS), also by design.

BUDGET_MS is the project report's end-to-end budget. The verdict states
whether the measured total fits it and, when it does not, which part spends
the most. Nothing here tunes anything to fit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BUDGET_MS = 150.0

CUED = "cued stop"
SIGNAL_LOSS = "signal-loss stop"

PARTS = {
    CUED: (
        "frame capture -> cue detected",
        "cue -> MotionRequest",
        "request -> datagram sent",
        "datagram -> receiver torque off",
    ),
    SIGNAL_LOSS: (
        "last actionable frame -> loss detected",
        "loss -> MotionRequest",
        "request -> datagram sent",
        "datagram -> receiver torque off",
    ),
}
TOTAL = "total"


@dataclass
class StopSample:
    """One measured stop. `stamps` is (t0, t1, t2, t3, t4) on one clock, in ms."""

    kind: str
    stamps: tuple[float, float, float, float, float]
    note: str = ""

    def __post_init__(self):
        if self.kind not in PARTS:
            raise ValueError(f"unknown stop kind {self.kind!r}")
        if len(self.stamps) != 5:
            raise ValueError("a stop sample needs five timestamps")

    @property
    def parts(self) -> tuple[float, float, float, float]:
        s = self.stamps
        return (s[1] - s[0], s[2] - s[1], s[3] - s[2], s[4] - s[3])

    @property
    def total(self) -> float:
        return self.stamps[4] - self.stamps[0]


@dataclass
class Unmeasured:
    """A stop that happened but could not be measured, and why. Counted, never dropped silently."""

    kind: str
    reason: str


@dataclass
class LatencyLog:
    samples: list[StopSample] = field(default_factory=list)
    unmeasured: list[Unmeasured] = field(default_factory=list)

    def add(self, sample: StopSample) -> None:
        self.samples.append(sample)

    def skip(self, kind: str, reason: str) -> None:
        self.unmeasured.append(Unmeasured(kind, reason))

    def of(self, kind: str) -> list[StopSample]:
        return [s for s in self.samples if s.kind == kind]


def median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        raise ValueError("median of nothing")
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def summarize(samples: list[StopSample]) -> dict[str, dict[str, float]]:
    """{part name: {"count", "median", "worst"}} plus TOTAL. Empty when there are no samples."""
    if not samples:
        return {}
    kind = samples[0].kind
    if any(s.kind != kind for s in samples):
        raise ValueError("summarize one kind of stop at a time")
    out = {}
    for i, name in enumerate(PARTS[kind]):
        values = [s.parts[i] for s in samples]
        out[name] = {"count": len(values), "median": median(values), "worst": max(values)}
    totals = [s.total for s in samples]
    out[TOTAL] = {"count": len(totals), "median": median(totals), "worst": max(totals)}
    return out


def verdict(samples: list[StopSample], budget_ms: float = BUDGET_MS) -> str:
    """One sentence: does the total fit the budget, and if not, which part spends it."""
    if not samples:
        return "no samples: nothing to compare with the budget"
    table = summarize(samples)
    names = PARTS[samples[0].kind]
    worst_total = table[TOTAL]["worst"]
    median_total = table[TOTAL]["median"]
    spender = max(names, key=lambda n: table[n]["median"])
    share = table[spender]["median"] / median_total * 100.0 if median_total > 0 else 0.0
    if worst_total <= budget_ms:
        return (f"WITHIN the {budget_ms:.0f} ms budget: median {median_total:.1f} ms, worst {worst_total:.1f} ms. "
                f"Largest part: {spender} ({share:.0f}% of the median).")
    if median_total <= budget_ms:
        return (f"OVER the {budget_ms:.0f} ms budget in the worst case: worst {worst_total:.1f} ms, "
                f"median {median_total:.1f} ms. Largest part: {spender} ({share:.0f}% of the median).")
    return (f"OVER the {budget_ms:.0f} ms budget: median {median_total:.1f} ms, worst {worst_total:.1f} ms. "
            f"The part that spends it: {spender} (median {table[spender]['median']:.1f} ms, "
            f"{share:.0f}% of the median total).")


def format_table(log: LatencyLog, kind: str, budget_ms: float = BUDGET_MS, prefix: str = "") -> list[str]:
    """Printable lines for one kind of stop: one row per part and the total, then the verdict."""
    samples = log.of(kind)
    skipped = [u for u in log.unmeasured if u.kind == kind]
    lines = [f"{prefix}{kind} latency ({len(samples)} measured, {len(skipped)} not measurable)"]
    if samples:
        table = summarize(samples)
        lines.append(f"{prefix}  {'part':<40} {'count':>5} {'median ms':>10} {'worst ms':>9}")
        for name in (*PARTS[kind], TOTAL):
            row = table[name]
            lines.append(f"{prefix}  {name:<40} {row['count']:>5} {row['median']:>10.1f} {row['worst']:>9.1f}")
        lines.append(f"{prefix}  {verdict(samples, budget_ms)}")
    else:
        lines.append(f"{prefix}  no {kind} was measured in this run")
    for u in skipped:
        lines.append(f"{prefix}  not measured: {u.reason}")
    return lines
