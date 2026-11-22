"""The three input tiers. Each turns calibrated channels into the same cue stream.

Project report, three tiers, and perception/README.md "Tier" (how the
calibration step proposes one):

* Tier 1, proportional: two or more usable channels, at least one broad
  and repeatable. A continuous channel's value maps into the envelope, with
  the profile's deadband applied.
* Tier 2, discrete: two or more usable channels, none broad. Threshold
  crossings become discrete cues.
* Tier 3, scanning: exactly one reliable channel. Options cycle at a dwell
  interval and that one signal selects the highlighted option.

Every reader returns a list of control/cues.Cue. The task machine does not
know or care which tier produced a cue. A reader decides nothing about
motion and never sees a velocity.

The calibration profile is perception's own record (`record_type`
"calibration_profile", its own `schema_version` 1). It is not part of
docs/contracts.md. This module reads only `accepted`, `session_quality`,
`proposed_tier`, `stop_binding` and `bindings`, and refuses a profile that
is not accepted, not "ok", or proposes no tier.

Directional cue bindings
------------------------
A discrete cue is bound to one SIDE of one channel: a CueBinding is
(channel, direction, cue) with direction ABOVE or BELOW the channel's
measured rest mean. One channel can therefore carry two distinct cues, one
on each side (head lift and head down on pitch_deg, tilt right and tilt
left on roll_deg).

Each side uses the profile's own numbers (perception/calibration.py,
"Rest reference and thresholds"): the activation threshold is
threshold.upper above rest and threshold.lower below it, and the deadband
[deadband.lower, deadband.upper] sits around rest on both sides. A side is
bindable only if its threshold lies outside the deadband. For a channel
bound on both sides this is the separation rule: the deadband must fit
entirely between the two thresholds,

    threshold.lower < deadband.lower <= deadband.upper < threshold.upper

so the two thresholds are separated by at least the deadband width and a
value cannot be past one threshold while inside the other's re-arm band.
A binding that breaks it is refused, never clamped.

What the profile does not carry, and so is an explicit argument here:

* Which cue each side of each discrete channel means. The profile assigns
  roles ("discrete", "proportional_primary", ...), not cues. `cue_map` is
  either a list of CueBinding or, for one cue per channel on its measured
  side, a dict channel -> cue.
* The scanning dwell interval and option list.

Stop: the profile's `stop_binding` channel always stops a moving chair. Its
measured side is either plain Stop, or the context-dependent binding
CueBinding(channel, side, GO, moving_cue=STOP): Go while the task is Hold,
Stop whenever it is not. Either way any press while moving is Stop, the
rule Tier 3 already uses. Nothing else may be bound to either side of that
channel. If that channel is also a proportional channel, Tier 1 refuses the
profile: one movement cannot mean both "faster" and "stop".

Context-dependent cues. A CueBinding with `moving_cue` emits `moving_cue`
when the reader is told the task is moving (any task other than Hold, so
Through counts as moving) and `cue` otherwise. The state is the task's
state before this tick's cues are applied. One press is still one cue: the
detector re-arms only after the channel is back inside its deadband, so the
press that stops the chair cannot also restart it. The hardware cutoff and the
signal-loss stop remain the stop path whatever is bound
(perception/README.md, `stop_binding.stop_path`).

Crossings
---------
A crossing needs MIN_SUSTAINED_FRAMES (2) consecutive frames past the
threshold. That is calibration's min_sustained_frames, so the false
positives and cross-talk measured at calibration are counted exactly the
way control detects them; a one-frame detector would fire on spikes the
measurements never counted.

Channels are read the way perception/filters.py channel_values() reads
them: blendshape scores from `features`, yaw_deg / pitch_deg / roll_deg from
`head_pose`, x_cm / y_cm / z_cm from the matrix translation column. That
logic is repeated here rather than imported so control/ stays standard
library only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from control.cues import CANCEL, CONFIRM, CUES, FASTER, GO, LEFT, RIGHT, SLOWER, STOP, THROUGH, Cue

ROLES = ("proportional_primary", "proportional_secondary", "discrete", "scan_switch")
TIER_MODES = {1: "proportional", 2: "discrete", 3: "scanning"}
POSE_ANGLE_CHANNELS = ("yaw_deg", "pitch_deg", "roll_deg")
TRANSLATION_CHANNELS = ("x_cm", "y_cm", "z_cm")

ABOVE = "above"
BELOW = "below"
SIDE = {ABOVE: 1, BELOW: -1}

# perception/calibration.py ScoringConstants.min_sustained_frames.
MIN_SUSTAINED_FRAMES = 2

# Tier 3 defaults (provisional). Stop is not an option: while the chair is
# moving every select is Stop, so Stop never waits for its turn in a cycle.
DEFAULT_SCAN_OPTIONS = (GO, LEFT, RIGHT, SLOWER, FASTER, THROUGH, CONFIRM, CANCEL)
DEFAULT_DWELL_MS = 1500
MIN_DWELL_MS = 300

# Tier 1 magnitudes are reported to two decimals; anything under one step
# beyond the deadband is not a cue.
MAGNITUDE_STEP = 0.01


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class Binding:
    """One channel binding from the profile, with the numbers the readers use.

    `direction` is the side the calibration measured the movement on.
    `threshold_lower` and `threshold_upper` are the profile's activation
    thresholds below and above rest. A profile without them gets the
    symmetric pair calibration always writes: rest_mean -/+ |active - rest_mean|.
    """

    channel: str
    role: str
    direction: int
    rest_mean: float
    active: float
    threshold_lower: float
    threshold_upper: float
    deadband_lower: float
    deadband_upper: float
    usable_range: float

    @classmethod
    def from_dict(cls, raw) -> "Binding":
        if not isinstance(raw, dict):
            raise ValueError("binding is not an object")
        channel = raw.get("channel")
        role = raw.get("role")
        direction = raw.get("direction")
        threshold = raw.get("threshold")
        deadband = raw.get("deadband")
        if not isinstance(channel, str) or not channel:
            raise ValueError("binding channel is missing")
        if role not in ROLES:
            raise ValueError(f"binding {channel}: unknown role {role!r}")
        if direction not in (1, -1) or isinstance(direction, bool):
            raise ValueError(f"binding {channel}: direction must be 1 or -1")
        if not isinstance(threshold, dict) or not isinstance(deadband, dict):
            raise ValueError(f"binding {channel}: threshold and deadband are required")
        numbers = {
            "rest_mean": raw.get("rest_mean"),
            "active": threshold.get("active"),
            "deadband_lower": deadband.get("lower"),
            "deadband_upper": deadband.get("upper"),
            "usable_range": raw.get("usable_range"),
        }
        for name, value in numbers.items():
            if not _finite(value):
                raise ValueError(f"binding {channel}: {name} must be a finite number")
        numbers = {k: float(v) for k, v in numbers.items()}
        # Without lower/upper: the measured side is `active` exactly, the other its mirror.
        mirror = 2 * numbers["rest_mean"] - numbers["active"]
        lower = threshold.get("lower", numbers["active"] if direction < 0 else mirror)
        upper = threshold.get("upper", numbers["active"] if direction > 0 else mirror)
        if not _finite(lower) or not _finite(upper):
            raise ValueError(f"binding {channel}: threshold lower and upper must be finite numbers")
        binding = cls(channel=channel, role=role, direction=direction, threshold_lower=float(lower),
                      threshold_upper=float(upper), **numbers)
        if not binding.deadband_lower <= binding.rest_mean <= binding.deadband_upper:
            raise ValueError(f"binding {channel}: rest mean lies outside its deadband")
        if binding.active != binding.threshold(direction):
            raise ValueError(f"binding {channel}: active threshold is not the threshold on its measured side")
        if not binding.side_bindable(direction):
            raise ValueError(f"binding {channel}: active threshold is not beyond the deadband")
        if binding.usable_range <= binding.edge():
            raise ValueError(f"binding {channel}: usable range does not reach past the deadband")
        return binding

    def deviation(self, value: float) -> float:
        """Signed distance from rest, positive in the binding's direction."""
        return self.direction * (value - self.rest_mean)

    def edge(self, side: int = 1) -> float:
        """Distance from rest to the deadband edge on `side` (1: the binding's direction)."""
        toward_upper = self.direction * side > 0
        return (self.deadband_upper - self.rest_mean) if toward_upper else (self.rest_mean - self.deadband_lower)

    def threshold(self, side: int) -> float:
        """The activation threshold above (side +1) or below (side -1) rest."""
        return self.threshold_upper if side > 0 else self.threshold_lower

    def side_bindable(self, side: int) -> bool:
        """True when that side's threshold lies outside the deadband."""
        return self.threshold_upper > self.deadband_upper if side > 0 else self.threshold_lower < self.deadband_lower

    def inside_deadband(self, value: float) -> bool:
        return self.deadband_lower <= value <= self.deadband_upper

    def past(self, value: float, side: int) -> bool:
        """At or beyond the threshold on `side` (+1 above rest, -1 below)."""
        return value >= self.threshold_upper if side > 0 else value <= self.threshold_lower


@dataclass(frozen=True)
class CueBinding:
    """A cue on one side of one channel: (channel, ABOVE or BELOW the rest mean, cue).

    `moving_cue`, when set, replaces `cue` while the task is moving (see
    "Context-dependent cues").
    """

    channel: str
    direction: str
    cue: str
    moving_cue: str | None = None

    def __post_init__(self):
        if self.direction not in SIDE:
            raise ValueError(f"cue binding {self.channel}: direction must be {ABOVE!r} or {BELOW!r}")
        if self.cue not in CUES:
            raise ValueError(f"cue binding {self.channel}: {self.cue!r} is not a cue")
        if self.moving_cue is not None and (self.moving_cue not in CUES or self.moving_cue == self.cue):
            raise ValueError(f"cue binding {self.channel}: moving_cue must be a different cue")

    @property
    def side(self) -> int:
        return SIDE[self.direction]

    def resolve(self, moving: bool) -> str:
        return self.moving_cue if moving and self.moving_cue is not None else self.cue


@dataclass(frozen=True)
class Profile:
    """The parts of an accepted calibration profile the control tiers use."""

    tier: int
    bindings: tuple[Binding, ...]
    stop_channel: str | None

    @classmethod
    def from_dict(cls, raw) -> "Profile":
        if not isinstance(raw, dict) or raw.get("record_type") != "calibration_profile":
            raise ValueError("not a calibration profile")
        if raw.get("accepted") is not True:
            raise ValueError("calibration profile was not accepted at confirm")
        if raw.get("session_quality") != "ok":
            raise ValueError("calibration session quality is not ok")
        proposed = raw.get("proposed_tier")
        tier = proposed.get("tier") if isinstance(proposed, dict) else None
        if tier not in TIER_MODES or isinstance(tier, bool):
            raise ValueError("calibration profile proposes no control tier")
        if proposed.get("mode") != TIER_MODES[tier]:
            raise ValueError("proposed tier and mode disagree")
        raw_bindings = raw.get("bindings")
        if not isinstance(raw_bindings, list) or not raw_bindings:
            raise ValueError("calibration profile has no bindings")
        bindings = tuple(Binding.from_dict(b) for b in raw_bindings)
        names = [b.channel for b in bindings]
        if len(set(names)) != len(names):
            raise ValueError("a channel is bound twice")
        stop = raw.get("stop_binding")
        stop_channel = stop.get("channel") if isinstance(stop, dict) else None
        if stop_channel is not None and stop_channel not in names:
            raise ValueError("stop_binding names a channel that has no binding")
        roles = [b.role for b in bindings]
        if tier == 3 and roles != ["scan_switch"]:
            raise ValueError("tier 3 needs exactly one scan_switch binding")
        if tier == 2 and set(roles) != {"discrete"}:
            raise ValueError("tier 2 bindings must all be discrete")
        if tier == 1 and roles.count("proportional_primary") != 1:
            raise ValueError("tier 1 needs exactly one proportional_primary binding")
        if tier != 1 and ("proportional_primary" in roles or "proportional_secondary" in roles):
            raise ValueError("proportional roles belong to tier 1 only")
        return cls(tier=tier, bindings=bindings, stop_channel=stop_channel)

    def binding(self, channel: str) -> Binding:
        for b in self.bindings:
            if b.channel == channel:
                return b
        raise KeyError(channel)

    def with_role(self, role: str) -> Binding | None:
        return next((b for b in self.bindings if b.role == role), None)


def channel_value(frame: dict, channel: str) -> float | None:
    """The channel's value in an actionable frame, or None when the frame does not carry it."""
    features = frame.get("features")
    if isinstance(features, dict) and channel in features:
        return float(features[channel])
    pose = frame.get("head_pose")
    if not isinstance(pose, dict):
        return None
    if channel in POSE_ANGLE_CHANNELS:
        return float(pose[channel])
    if channel in TRANSLATION_CHANNELS:
        return float(pose["matrix"][TRANSLATION_CHANNELS.index(channel)][3])
    return None


class EdgeDetector:
    """A sustained threshold crossing on one side of a channel, with hysteresis.

    Fires once when the value has been at or past the threshold on `side`
    for MIN_SUSTAINED_FRAMES consecutive frames, then re-arms only after the
    value is back inside the deadband. It starts disarmed, so a channel that
    is already active when reading begins (or after a signal loss) does not
    fire until the user has returned to rest once. `side` defaults to the
    binding's measured direction.
    """

    def __init__(self, binding: Binding, side: int | None = None):
        self.binding = binding
        self.side = binding.direction if side is None else side
        if not binding.side_bindable(self.side):
            raise ValueError(f"{binding.channel}: the threshold on that side lies inside the deadband")
        self.armed = False
        self._run = 0

    def reset(self) -> None:
        self.armed = False
        self._run = 0

    def update(self, value: float | None) -> bool:
        if value is None:
            self.reset()
            return False
        if self.binding.inside_deadband(value):
            self.armed = True
            self._run = 0
            return False
        if not self.binding.past(value, self.side):
            self._run = 0
            return False
        self._run += 1
        if self.armed and self._run >= MIN_SUSTAINED_FRAMES:
            self.armed = False
            self._run = 0
            return True
        return False


class Excursion:
    """Tier 1: the peak of one excursion past the deadband, reported on return.

    An excursion starts when the value leaves the deadband and ends when it
    comes back. Its peak is reported once, at the end, as a fraction of the
    span from the deadband edge to the edge of the usable range. Reporting
    the peak (not the live value) is what lets the user set a speed and
    then relax: returning to rest does not drag the setting down with it.
    With `signed`, an excursion on the side opposite the binding's direction
    is reported with side -1; the profile measures only the probed
    direction, so that side reuses the same usable range (provisional).
    """

    def __init__(self, binding: Binding, signed: bool):
        self.binding = binding
        self.signed = signed
        self.armed = False
        self.side = 0
        self.peak = 0.0

    def reset(self) -> None:
        self.armed = False
        self.side = 0
        self.peak = 0.0

    def update(self, value: float | None) -> tuple[int, float] | None:
        """Return (side, magnitude) when an excursion ends, else None."""
        if value is None:
            self.reset()
            return None
        b = self.binding
        if b.inside_deadband(value):
            result = None
            if self.armed and self.side != 0:
                edge = b.edge(self.side)
                fraction = (self.peak - edge) / (b.usable_range - edge)
                magnitude = round(min(1.0, fraction), 2)
                if magnitude >= MAGNITUDE_STEP:
                    result = (self.side, magnitude)
            self.armed = True
            self.side = 0
            self.peak = 0.0
            return result
        if not self.armed:
            return None
        deviation = b.deviation(value)
        side = 1 if deviation > 0 else -1
        if side < 0 and not self.signed:
            return None
        if self.side not in (0, side):
            # Swung straight across the deadband without settling: not a clean
            # excursion, so it is dropped and reading waits for rest again.
            self.reset()
            return None
        self.side = side
        self.peak = max(self.peak, abs(deviation))
        return None


def _side_name(side: int) -> str:
    return ABOVE if side > 0 else BELOW


def cue_bindings(profile: Profile, cue_map) -> list[CueBinding]:
    """Normalise and check cue_map. Returns the bindings, Stop first.

    cue_map is a list of CueBinding, or a dict channel -> cue meaning that
    cue on the channel's measured side. Refused, with a ValueError naming
    the problem: a channel without a discrete binding; on the stop channel,
    anything but Stop or Go-in-Hold/Stop-while-moving on its measured side;
    a side bound twice; a side whose threshold
    lies inside the deadband, which for a channel bound on both sides is
    the two thresholds failing to straddle the deadband.
    """
    if cue_map is None:
        requested: list[CueBinding] = []
    elif isinstance(cue_map, dict):
        requested = []
        for channel, cue in cue_map.items():
            try:
                side = profile.binding(channel).direction
            except KeyError:
                raise ValueError(f"cue_map: {channel} has no binding in the profile") from None
            requested.append(CueBinding(channel, _side_name(side), cue))
    else:
        requested = list(cue_map)
        if not all(isinstance(b, CueBinding) for b in requested):
            raise ValueError("cue_map must be a dict or a list of CueBinding")

    result: list[CueBinding] = []
    if profile.stop_channel is not None:
        stop = profile.binding(profile.stop_channel)
        result.append(CueBinding(stop.channel, _side_name(stop.direction), STOP))
    for b in requested:
        try:
            binding = profile.binding(b.channel)
        except KeyError:
            raise ValueError(f"cue_map: {b.channel} has no binding in the profile") from None
        if b.channel == profile.stop_channel:
            plain_stop = b.cue == STOP and b.moving_cue in (None, STOP)
            go_or_stop = b.cue == GO and b.moving_cue == STOP
            if b.side != binding.direction or not (plain_stop or go_or_stop):
                raise ValueError(f"cue_map: {b.channel} is the stop channel and cannot be remapped; it must "
                                 "stop a moving chair (Stop, or Go in Hold and Stop while moving, on its measured side)")
            if go_or_stop:
                result[0] = b  # replaces the plain Stop the profile makes
            continue
        if binding.role != "discrete":
            raise ValueError(f"cue_map: {b.channel} is {binding.role}, not discrete")
        if any(r.channel == b.channel and r.direction == b.direction for r in result):
            raise ValueError(f"cue_map: {b.channel} {b.direction} is bound twice")
        if not binding.side_bindable(b.side):
            raise ValueError(
                f"cue_map: {b.channel} {b.direction}: threshold {binding.threshold(b.side):.6g} lies inside the "
                f"deadband [{binding.deadband_lower:.6g}, {binding.deadband_upper:.6g}]; the thresholds overlap"
            )
        result.append(b)
    for channel in {b.channel for b in result}:
        sides = [b for b in result if b.channel == channel]
        if len(sides) == 2:
            binding = profile.binding(channel)
            if not binding.threshold_lower < binding.deadband_lower <= binding.deadband_upper < binding.threshold_upper:
                raise ValueError(f"cue_map: {channel} thresholds do not straddle the deadband; bindings overlap")
    return result


def _detectors(profile: Profile, cue_map) -> list[tuple[CueBinding, EdgeDetector]]:
    return [(b, EdgeDetector(profile.binding(b.channel), b.side)) for b in cue_bindings(profile, cue_map)]


def _read_detectors(detectors, frame: dict, source: str, moving: bool) -> list[Cue]:
    cues = []
    for binding, detector in detectors:
        if detector.update(channel_value(frame, detector.binding.channel)):
            cues.append(Cue(binding.resolve(moving), source))
    return cues


class DiscreteReader:
    """Tier 2: each sustained threshold crossing, on its bound side, is one discrete cue."""

    source = "tier2"

    def __init__(self, profile: Profile, cue_map):
        self._detectors = _detectors(profile, cue_map)

    @property
    def bindings(self) -> list[tuple]:
        """(channel, side, cue), plus the moving cue for a context-dependent binding."""
        return [(b.channel, b.direction, b.cue) + ((b.moving_cue,) if b.moving_cue else ()) for b, _ in self._detectors]

    def reset(self) -> None:
        for _, detector in self._detectors:
            detector.reset()

    def read(self, frame: dict, now_ms: int, moving: bool) -> list[Cue]:
        return _read_detectors(self._detectors, frame, self.source, moving)


class ProportionalReader:
    """Tier 1: the primary channel sets speed, the secondary sets heading.

    The peak of a primary excursion becomes Go with that magnitude: the
    speed setting becomes that fraction of the envelope, and the chair keeps
    it after the user relaxes. The peak of a secondary excursion becomes
    Left (binding direction) or Right (opposite side) with that magnitude:
    that fraction of one heading increment. Stop and any assigned discrete
    channels work as in Tier 2.
    """

    source = "tier1"

    def __init__(self, profile: Profile, cue_map=None):
        primary = profile.with_role("proportional_primary")
        secondary = profile.with_role("proportional_secondary")
        for b in (primary, secondary):
            if b is not None and b.channel == profile.stop_channel:
                raise ValueError(
                    f"{b.channel} is both the stop channel and {b.role}; tier 1 needs a separate stop channel"
                )
        self._primary = Excursion(primary, signed=False)
        self._secondary = None if secondary is None else Excursion(secondary, signed=True)
        self._discrete = _detectors(profile, cue_map)

    def reset(self) -> None:
        self._primary.reset()
        if self._secondary is not None:
            self._secondary.reset()
        for _, detector in self._discrete:
            detector.reset()

    def read(self, frame: dict, now_ms: int, moving: bool) -> list[Cue]:
        cues = _read_detectors(self._discrete, frame, self.source, moving)
        ended = self._primary.update(channel_value(frame, self._primary.binding.channel))
        if ended is not None:
            cues.append(Cue(GO, self.source, ended[1]))
        if self._secondary is not None:
            ended = self._secondary.update(channel_value(frame, self._secondary.binding.channel))
            if ended is not None:
                cues.append(Cue(LEFT if ended[0] > 0 else RIGHT, self.source, ended[1]))
        return cues


class ScanningReader:
    """Tier 3: one switch, options highlighted in turn.

    The cycle is a blank slot followed by each option, each shown for
    `dwell_ms`. The blank slot is the start of every cycle and also follows
    every selection, so the user sees the result and releases the switch
    before anything else can be picked. A select during the blank slot does
    nothing.

    While the task is moving, every select is Stop, whatever is highlighted
    (project report: a Stop is never queued behind anything). Steering and
    speed options are therefore chosen while stopped; Left and Right then
    take effect when Go starts (control/task.py).
    """

    source = "tier3"

    def __init__(self, profile: Profile, options: tuple[str, ...] = DEFAULT_SCAN_OPTIONS, dwell_ms: int = DEFAULT_DWELL_MS):
        switch = profile.with_role("scan_switch")
        if switch is None:
            raise ValueError("scanning needs a scan_switch binding")
        if not options or len(set(options)) != len(options):
            raise ValueError("scan options must be a non-empty list without repeats")
        if any(option not in CUES or option == STOP for option in options):
            raise ValueError("scan options must be cues other than Stop")
        if isinstance(dwell_ms, bool) or not isinstance(dwell_ms, int) or dwell_ms < MIN_DWELL_MS:
            raise ValueError(f"dwell_ms must be an integer of at least {MIN_DWELL_MS}")
        self.options = tuple(options)
        self.dwell_ms = dwell_ms
        self._switch = EdgeDetector(switch)
        self._epoch: int | None = None

    def reset(self) -> None:
        self._switch.reset()
        self._epoch = None

    def highlighted(self, now_ms: int) -> str | None:
        """The option on show at now_ms, or None during the blank slot or before scanning starts."""
        if self._epoch is None or now_ms < self._epoch:
            return None
        slot = ((now_ms - self._epoch) // self.dwell_ms) % (len(self.options) + 1)
        return None if slot == 0 else self.options[slot - 1]

    def read(self, frame: dict, now_ms: int, moving: bool) -> list[Cue]:
        if self._epoch is None:
            self._epoch = now_ms
        if not self._switch.update(channel_value(frame, self._switch.binding.channel)):
            return []
        if moving:
            self._epoch = now_ms
            return [Cue(STOP, self.source)]
        choice = self.highlighted(now_ms)
        if choice is None:
            return []
        self._epoch = now_ms
        return [Cue(choice, self.source)]


def reader_for(profile: Profile, cue_map=None, **scan_options):
    """The reader for the profile's proposed tier."""
    if profile.tier == 1:
        return ProportionalReader(profile, cue_map)
    if profile.tier == 2:
        if not cue_map:
            raise ValueError("tier 2 needs a cue_map: the profile does not say which channel means which cue")
        return DiscreteReader(profile, cue_map)
    if cue_map:
        raise ValueError("tier 3 has one switch; it takes no cue_map")
    return ScanningReader(profile, **scan_options)
