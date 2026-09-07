"""Counting what a channel actually did, without flattering it.

A radio link is measured by reading what already crosses it. Every MAVLink message
carries an 8-bit sequence number, so the holes in that run give packet loss with
nothing added to the protocol: no extra field, no probe message, no agreement with
the other end. That is a genuinely good instrument, and it is why this exists.

It is also an instrument that is easy to read wrong, and the three ways found in the
ported code are the three things this module is shaped around.

**A silence that is still going on is a silence.** The ported version took the
largest gap *between recorded emissions* inside a sliding window. After emissions at
0 s and 0.1 s and nothing since, it reported 0.1 s of maximum silence at t=2.9, and
then **0.0 s at t=3.2**, because the window had dropped both emissions. A link dead
for three seconds reported no silence at all. A window is for rates; continuity is
not a rate. The instant of the last emission is therefore kept outside the window
forever, and the gap that has not ended yet is counted like any other.

**Loss, reordering, a restart and an undecidable case are four different things.**
The ported version compared each sequence number with the previous one and called the
step a loss below a threshold and "disorder" above it. Two consequences, both
reproduced: a burst of 65 lost messages (0 -> 66) was classified as disorder and
reported **0 % loss**, while the perfectly complete run `[10, 12, 11, 13]` reported
**33 % loss** because 11 arrived after 12 and nothing went back to retract the two
gaps it had already counted. Here a gap is *provisional* until a message can no longer
plausibly arrive late, a message arriving behind the frontier retracts the gap it
fills, and a jump too large to attribute to anything is counted apart and never folded
into the loss figure.

**An attempt, an emission and an applied effect are three different events.** The
ported loop called ``on_tx`` on every cycle, including cycles where the byte counter
had not moved, so every cycle looked like an emission and the silence metric could
never fire. :meth:`LinkStats.on_tx` takes the bytes that actually left and records a
cycle that sent nothing as an *attempt*, which is counted and never mistaken for one.

**What an 8-bit counter cannot tell you, stated once.** A sender restarting its
counter, a long burst of loss and a very late message are the same arithmetic. This
module refuses to guess between them: what it cannot attribute, it counts as
ambiguous and leaves out of the loss estimate. A restart that happens to land inside
the gap limit is indistinguishable from loss and *is* counted as loss -- that is a
property of the counter, not a defect here, and it is the reason a wider sequence
number is worth having when the protocol offers one.

**A wrapped counter is not an identity, and the difference is where the arithmetic
goes wrong.** Two messages 256 apart carry the same number, so a gap left by one and a
gap left by the other are indistinguishable if the gap is remembered by the number it
wore on the wire. Two holes a run apart merged into one, and a late message from the
second run retracted the hole from the first. Everything about identity below --
duplicates, gaps, how many distinct messages were seen -- is therefore done on an
**ordinal**: the position reconstructed by unwrapping the counter against the frontier.
The wire number is what arrives; the ordinal is what it means.

**A rate is not a ratio.** Raw arrivals are what a throughput figure is made of, and
duplicates belong in it. They do not belong in a loss ratio: counting the same message
twice in the denominator lowers the reported loss without anything having been
delivered, and a hundred retransmissions of one packet turned a quarter of a run
missing into one per cent of it. Loss is therefore computed over **distinct** messages
and the raw counts are kept beside it for the throughput they are good for.

**"Loss" here means not delivered within the hold**, which is a choice and not the only
one available. A message confirmed missing and then arriving late stays counted: a
figure that silently revises itself downward describes a past that has changed. What
does not happen any more is counting that message twice in the denominator, once as a
loss and once as an arrival.

**Calls arrive in time order, and that is a requirement rather than a hope.** Every
series here is kept oldest-first and trimmed from the front, the last emission is the
last one reported, and a read confirms losses irreversibly -- so a call carrying a time
earlier than one already accepted does not simply measure something odd, it corrupts
what was measured before. A backdated emission left two of them inside a
three-second window and moved the silence reference backwards; a backdated latency
sample survived a trim that should have dropped it and took the 95th percentile with
it; a read taken in the past reported emissions from the future. All of that is now
refused, before anything moves, and the next call in order is processed normally.

Two things that look alike and are not: **sequence numbers arriving out of order** is
the ordinary condition this module exists to measure and keeps working exactly as
before, as long as the *reception times* it is given do not go backwards. Equal times
are fine -- several messages really can be handed over at one instant.

Pure Python: ``collections``, ``dataclasses``, ``enum``, ``math``. Nothing from this
project and nothing about MAVLink. It is handed numbers and returns numbers, which is
what makes it testable at a bench with no radio and reusable on a CRSF link that
shares none of MAVLink's framing.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum




def _is_finite(value) -> bool:
    """Whether ``value`` is a real number this module can compare against others.

    ``bool`` is excluded even though Python calls it an integer: a timestamp of ``True``
    is a type confusion rather than a value.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _clock_problem(now, label: str) -> str:
    return "" if _is_finite(now) else f"{label} is not finite: {now!r}"


def _seq_problem(seq) -> str:
    if isinstance(seq, bool) or not isinstance(seq, int):
        return f"sequence number is not an integer: {type(seq).__name__}"
    return ""


def _bytes_problem(nbytes) -> str:
    if isinstance(nbytes, bool) or not isinstance(nbytes, int):
        return f"byte count must be an integer, got {type(nbytes).__name__}: {nbytes!r}"
    if nbytes < 0:
        return f"byte count cannot be negative: {nbytes!r}"
    return ""


class SeqEvent(Enum):
    """What one received sequence number was, relative to the run so far.

    Closed and audited, like :class:`argos.safety.Intervention`. The whole point of
    this module is that these are not interchangeable, so they are named apart and
    counted apart rather than collapsed into one percentage.
    """

    FIRST = "first"            # nothing to compare against yet
    IN_ORDER = "in_order"      # exactly one past the frontier
    GAP = "gap"                # ahead by more than one: messages provisionally missing
    REORDER = "reorder"        # behind the frontier, within the reordering window
    DUPLICATE = "duplicate"    # a position already received, again
    AMBIGUOUS = "ambiguous"    # a jump this counter cannot attribute to anything
    REJECTED = "rejected"      # the call could not be used and changed nothing


@dataclass(frozen=True)
class SeqPolicy:
    """How much benefit of the doubt to give a sequence number. Policy, not measurement.

    None of these was derived from a measured property of a radio. They decide what
    this instrument is willing to claim, and changing them changes the claim.
    """

    modulus: int = 256
    """The counter's width. 256 for MAVLink v1's 8-bit sequence.

    The narrower it is, the more of the space is ambiguous: with 256 values, a jump of
    200 forward and one of 56 backward are the same number. A protocol offering a
    wider counter turns most of the ambiguity below into ordinary arithmetic, which is
    worth knowing before quoting a loss figure from this one.
    """

    gap_limit: int = 32
    """The largest forward jump still attributed to loss.

    Beyond it, a burst of loss, a sender that restarted its counter and a very late
    message are the same arithmetic, so nothing is attributed. Below it they are
    *still* the same arithmetic -- a restart landing inside this limit is counted as
    loss and cannot be told apart. That is what an 8-bit counter can do, stated rather
    than hidden.
    """

    reorder_window: int = 8
    """How far behind the frontier a message may arrive and still be called reordering
    rather than something unattributable."""

    reorder_hold: float = 0.5
    """Seconds a gap stays provisional before it is called a real loss.

    A gap is not a loss until the message can no longer plausibly arrive. Confirming
    immediately is what made a complete-but-reordered run report a third of its
    messages lost.
    """

    def __post_init__(self) -> None:
        """Reject a policy that cannot classify anything, at construction.

        The integer fields are checked for being integers as well as for their range: a
        modulus of 256.5 used to be accepted here and then fail with a ``TypeError`` on
        an ordinary reception, which reports the mistake at a point that has nothing to
        do with it.
        """
        for name in ("modulus", "gap_limit", "reorder_window"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"SeqPolicy.{name} must be an integer, got {type(value).__name__}: "
                    f"{value!r}"
                )
        if self.modulus < 2:
            raise ValueError(f"SeqPolicy.modulus must be at least 2, got {self.modulus!r}")
        if not 1 <= self.gap_limit < self.modulus:
            raise ValueError(
                f"SeqPolicy.gap_limit must be within 1..modulus-1, got {self.gap_limit!r}"
            )
        if not 1 <= self.reorder_window < self.modulus:
            raise ValueError(
                f"SeqPolicy.reorder_window must be within 1..modulus-1, got "
                f"{self.reorder_window!r}"
            )
        if self.gap_limit + self.reorder_window >= self.modulus:
            raise ValueError(
                f"SeqPolicy.gap_limit + reorder_window must stay below modulus, so that "
                f"a forward jump and a late message are not the same number: got "
                f"{self.gap_limit!r} + {self.reorder_window!r} >= {self.modulus!r}"
            )
        if not math.isfinite(self.reorder_hold) or self.reorder_hold < 0.0:
            raise ValueError(
                f"SeqPolicy.reorder_hold must be finite and not negative, got "
                f"{self.reorder_hold!r}"
            )


@dataclass(frozen=True)
class SeqCounts:
    """What a sequence run contained, with traffic and identity kept apart.

    ``arrivals`` is traffic: every message that turned up, duplicates included, which
    is what a throughput figure is made of. ``distinct`` is identity: how many
    different messages those arrivals represent. Ratios below use the second, because
    a message delivered twice was delivered once.
    """

    arrivals: int = 0
    """Messages that physically arrived, duplicates included."""

    distinct: int = 0
    """Distinct positions received at least once."""

    in_order: int = 0
    duplicates: int = 0
    reordered: int = 0

    missing: int = 0
    """Positions that stayed empty past ``reorder_hold``. Confirmed, not provisional."""

    late: int = 0
    """Positions confirmed missing that arrived afterwards anyway.

    They stay counted as missing -- the run really did go without them for longer than
    the hold -- and they are *not* counted a second time in the denominator, which is
    what used to make a late delivery look like an improvement in the loss rate.
    """

    pending: int = 0
    """Gaps still inside ``reorder_hold``. Not yet losses, and not yet not-losses."""

    ambiguous: int = 0
    """Jumps this counter cannot attribute: a long burst, a restart, or a very late
    message, which are the same arithmetic on a wrapping counter. Once one has
    occurred, positions can no longer be reconstructed reliably and the ratios below
    stop being facts."""

    rejected: int = 0
    """Calls that could not be used and changed nothing."""

    @property
    def decidable(self) -> bool:
        """Whether every event in this run could be placed on the sequence."""
        return self.ambiguous == 0

    @property
    def expected(self) -> int:
        """Distinct messages the sender is known to have emitted.

        ``distinct + missing - late``: everything seen, plus the holes nothing filled,
        minus the holes that were counted as holes and then turned up, so that no
        message contributes twice.
        """
        return self.distinct + self.missing - self.late

    @property
    def loss(self) -> float | None:
        """Confirmed losses over distinct traffic, or ``None`` if that is not a fact.

        ``None`` whenever an ambiguous event occurred: positions cannot be
        reconstructed across one, so the run contains an unmeasured remainder and
        folding it into a ratio would report the part that was measured as if it were
        the whole. Read :attr:`missing` and :attr:`ambiguous` in that case; they are
        what is actually known.

        The figure is for the whole run. A caller wanting loss over a window builds a
        :class:`SequenceAudit` per window rather than asking this one to forget.
        """
        if not self.decidable:
            return None
        return 0.0 if self.expected == 0 else self.missing / self.expected

    @property
    def loss_bound(self) -> float | None:
        """The same estimate with every still-provisional gap counted as lost.

        The pessimistic reading, and the one that keeps :attr:`loss` from being read
        flatteringly. A gap inside ``reorder_hold`` is not yet a loss, so it is absent
        from ``loss`` -- which means a display polling faster than that hold shows a
        link losing half its traffic as losing none of it. With a steady loss rate the
        two converge once the run is longer than the hold; between them lies exactly
        what has not been decided yet.
        """
        if not self.decidable:
            return None
        lost = self.missing + self.pending
        total = self.expected + self.pending
        return 0.0 if total == 0 else lost / total


class SequenceAudit:
    """One sender's sequence run, classified message by message.

    One instance per source. Two senders sharing a counter would produce a run that is
    not a run, and the holes in it would be arithmetic rather than loss.

    **Positions, not wire numbers.** The counter wraps; the run does not. Each arrival
    is placed on an ordinal reconstructed against the frontier, and every question
    about identity is asked of that ordinal. Remembering a gap by the number it wore on
    the wire merged holes from different turns of the counter and let a late message
    from one turn retract a hole from another.
    """

    #: Received and confirmed positions are remembered this far behind the frontier.
    #: Anything older can only come back as an arrival too far behind to be placed at
    #: all, which is already handled as ambiguous, so keeping it would cost memory to
    #: answer a question that cannot be asked.
    def __init__(self, policy: SeqPolicy | None = None) -> None:
        self.policy = policy or SeqPolicy()
        self._horizon = self.policy.gap_limit + self.policy.reorder_window

        self._frontier_seq: int | None = None
        self._frontier_ordinal = 0
        self._segment_start = 0
        """The oldest position this generation can speak for.

        A jump nothing can attribute ends one generation and starts another, and the
        reconstruction cannot see across that break: how far the counter really moved
        is exactly what was unattributable. Reaching back past this point would be
        placing a message on positions belonging to the previous run, which is how a
        late arrival came to fill a hole it had nothing to do with.
        """

        self._clock: float | None = None          # the latest time accepted so far
        self._slots: dict[int, str] = {}          # ordinal -> received | pending | missing
        self._missed_at: dict[int, float] = {}    # ordinal -> when the gap was noticed

        self.arrivals = 0
        self.distinct = 0
        self.in_order = 0
        self.duplicates = 0
        self.reordered = 0
        self.missing = 0
        self.late = 0
        self.ambiguous = 0
        self.rejected = 0
        self.last_rejection = ""

    def on_rx(self, seq: int, now: float) -> SeqEvent:
        """Record one received sequence number and say what it was.

        Returns the classification so a caller can log the event that happened rather
        than infer it from a counter that moved.

        **Declines rather than raises, and validates before touching anything.** This
        is fed from a control loop, where a malformed value is an ordinary event and
        stopping the loop over a counter is the worse failure. It used to admit a
        ``NaN`` timestamp, which left a gap whose age could never be compared against
        anything and which therefore never expired.
        """
        problem = self._input_problem(seq, now)
        if problem:
            self.rejected += 1
            self.last_rejection = problem
            return SeqEvent.REJECTED

        policy = self.policy
        seq %= policy.modulus
        self._clock = now
        self.arrivals += 1
        self._confirm_expired(now)

        if self._frontier_seq is None:
            self._frontier_seq = seq
            self._frontier_ordinal = 0
            self._receive(0)
            return SeqEvent.FIRST

        ahead = (seq - self._frontier_seq) % policy.modulus
        behind = policy.modulus - ahead

        if ahead == 0:
            self._receive(self._frontier_ordinal)
            return SeqEvent.DUPLICATE

        if ahead <= policy.gap_limit:
            # Forward. Everything skipped is missing *for now*: a message that
            # overtook these will retract them when it arrives.
            for step in range(1, ahead):
                self._miss(self._frontier_ordinal + step, now)
            self._frontier_seq = seq
            self._frontier_ordinal += ahead
            self._receive(self._frontier_ordinal)
            self._prune()
            if ahead == 1:
                self.in_order += 1
                return SeqEvent.IN_ORDER
            return SeqEvent.GAP

        if behind <= policy.reorder_window and \
                self._frontier_ordinal - behind >= self._segment_start:
            # Behind the frontier by a plausible amount, and still inside the run this
            # frontier belongs to: the position is known exactly.
            #
            # The second half of that condition is not redundant. With a reordering
            # window wider than the smallest unattributable jump -- `gap_limit=2,
            # reorder_window=8`, say, which the policy accepts -- the window reaches
            # back past the start of the current generation and lands on positions from
            # the one before it, erasing a hole that had nothing to do with this
            # message. "The frontier only moves forward" does not rule that out,
            # because it is the *reach behind* the frontier that crosses the break.
            if not self._receive(self._frontier_ordinal - behind):
                return SeqEvent.DUPLICATE
            self.reordered += 1
            return SeqEvent.REORDER

        # Neither. On a wrapping counter this shape is produced by a long burst of
        # loss, by a sender restarting its counter, and by a message so late it has
        # lapped the window. Nothing is attributed, and the run goes on being counted:
        # the ratios stop being facts from here, but the raw counts do not, and they
        # are what is left to read.
        #
        # The new generation cannot land on a position already recorded, because
        # positions only ever move forward -- every branch above adds to the frontier
        # or reaches back behind it, and none of them lowers it. That invariant is what
        # keeps the two generations apart; an earlier version also jumped a whole
        # modulus here, which no test could break because it was doing nothing.
        self.ambiguous += 1
        self._frontier_seq = seq
        self._frontier_ordinal += ahead
        self._segment_start = self._frontier_ordinal
        self._receive(self._frontier_ordinal)
        self._prune()
        return SeqEvent.AMBIGUOUS

    def counts(self, now: float) -> SeqCounts:
        """The run so far, with gaps older than ``reorder_hold`` confirmed as losses.

        Raises on an unusable clock rather than confirming gaps against it. A read is a
        caller asking a question, and there is no answer to give to a question asked
        with ``NaN``; refusing before touching anything is what keeps a bad read from
        costing the history.
        """
        if not _is_finite(now):
            raise ValueError(f"SequenceAudit.counts needs a finite clock, got {now!r}")
        if self._clock is not None and now < self._clock:
            raise ValueError(
                f"SequenceAudit.counts was asked about {now!r}, before {self._clock!r} "
                f"which it has already accepted: it keeps no history to answer with"
            )
        self._clock = now
        self._confirm_expired(now)
        return SeqCounts(
            arrivals=self.arrivals,
            distinct=self.distinct,
            in_order=self.in_order,
            duplicates=self.duplicates,
            reordered=self.reordered,
            missing=self.missing,
            late=self.late,
            pending=len(self._missed_at),
            ambiguous=self.ambiguous,
            rejected=self.rejected,
        )

    # --- internals ---------------------------------------------------------

    def _input_problem(self, seq, now) -> str:
        problem = _seq_problem(seq)
        if problem:
            return problem
        if not _is_finite(now):
            return f"reception time is not finite: {now!r}"
        if self._clock is not None and now < self._clock:
            # Sequence numbers may arrive in any order; the clock may not. A gap
            # confirmed by a read at one instant cannot be un-confirmed by a message
            # claiming to predate it, and accepting one meant two different notions of
            # "the time" were deciding the same question.
            return (
                f"reception time {now!r} is earlier than {self._clock!r}, already "
                f"accepted: this instrument requires calls in time order"
            )
        return ""

    def _receive(self, ordinal: int) -> bool:
        """Mark a position as delivered. ``False`` if it had already been delivered.

        The one place a position's state changes, so the counters cannot disagree
        about what a message was.
        """
        state = self._slots.get(ordinal)

        if state == "received":
            self.duplicates += 1
            return False

        if state == "pending":
            # It turned up inside the hold: the gap was never a loss.
            self._missed_at.pop(ordinal, None)
        elif state == "missing":
            # Confirmed lost, then delivered anyway. It stays counted as missing --
            # the run really did go without it for longer than the hold -- and `late`
            # is what stops it being counted a second time in the denominator.
            self.late += 1

        self._slots[ordinal] = "received"
        self.distinct += 1
        return True

    def _miss(self, ordinal: int, now: float) -> None:
        if ordinal in self._slots:
            return
        self._slots[ordinal] = "pending"
        self._missed_at[ordinal] = now

    def _confirm_expired(self, now: float) -> None:
        """A gap nothing filled in time stops being provisional and becomes a loss."""
        hold = self.policy.reorder_hold
        for ordinal, first_missed in list(self._missed_at.items()):
            if now - first_missed > hold:
                del self._missed_at[ordinal]
                self._slots[ordinal] = "missing"
                self.missing += 1

    def _prune(self) -> None:
        """Forget settled positions too far behind to be asked about again."""
        floor = self._frontier_ordinal - self._horizon
        for ordinal, state in list(self._slots.items()):
            if ordinal < floor and state != "pending":
                del self._slots[ordinal]



@dataclass(frozen=True)
class LatencySummary:
    """One named stage's latency over the window, in seconds."""

    stage: str
    samples: int = 0
    p50: float | None = None
    p95: float | None = None


@dataclass(frozen=True)
class LinkSnapshot:
    """What the channel did. Rates over a window; continuity over the whole run."""

    window: float = 0.0

    rx: int = 0
    rx_hz: float = 0.0
    rx_bytes_per_s: float = 0.0

    tx: int = 0
    """Emissions: cycles where bytes actually left."""

    tx_hz: float = 0.0
    tx_bytes_per_s: float = 0.0

    tx_attempts: int = 0
    """Cycles that tried and sent nothing. Counted apart from emissions, because
    counting them together is what made every cycle look like an emission and left the
    silence figure unable to fire."""

    rejected: int = 0
    """Calls this instrument could not use and did not act on."""

    ongoing_silence: float = 0.0
    """Seconds since the last emission, or since the run started if there was none.

    **Not windowed**, and this is the number a loop-continuity check has to read: the
    silence that matters is the one happening now, and it is exactly the one a sliding
    window drops as it grows.
    """

    longest_silence: float = 0.0
    """The longest gap between emissions seen in the whole run, including the wait
    before the first one and the gap still in progress.

    Also not windowed: a link that died two minutes ago did not recover because the
    window moved on, and a link that took ten seconds to say anything at all did not
    become punctual the moment it finally did.
    """

    sequence: SeqCounts = field(default_factory=SeqCounts)
    """Every source's counts added together. ``loss`` is ``None`` as soon as any one
    of them contains something unattributable."""

    by_source: dict = field(default_factory=dict)
    latency: dict = field(default_factory=dict)


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[index]


class LinkStats:
    """One channel's accounting. Handed numbers, returns numbers.

    One instance per link. The day there are two -- a real radio and a WiFi bridge --
    there are two of these and they are compared, which is the point of having
    separated the measurement from the transport.

    **Calls arrive in time order.** Each series is trimmed from its front, the last
    emission is whichever was reported last, and a read settles gaps for good, so a call
    carrying a time earlier than one already accepted does not measure something odd --
    it invalidates what was measured before. Equal times are accepted: several messages
    really can be handed over at one instant. Out-of-order *sequence numbers* are a
    different thing entirely and remain the ordinary case.

    **Every input is checked before anything changes, and a refused call changes
    nothing.** These methods are called from a control loop, so they decline instead of
    raising: a counter is not worth stopping a loop over. What they must not do is
    half-apply -- an earlier version moved the last-emission instant and *then* raised
    on the byte count, which shortened the measured silence by exactly the interval the
    caller had got wrong. The reads are the other way round: they raise, because a
    caller asking for a snapshot at ``NaN`` has made a mistake at the call site and
    there is no answer to hand back, but they raise before touching the history.
    """

    def __init__(
        self,
        window: float = 3.0,
        policy: SeqPolicy | None = None,
        started_at: float = 0.0,
    ) -> None:
        if not _is_finite(window) or window <= 0.0:
            raise ValueError(f"LinkStats.window must be finite and positive, got {window!r}")
        if not _is_finite(started_at):
            raise ValueError(f"LinkStats.started_at must be finite, got {started_at!r}")

        self.window = float(window)
        self.policy = policy or SeqPolicy()
        self._started_at = float(started_at)

        self._rx: deque = deque()          # (t, bytes, source)
        self._tx: deque = deque()          # (t, bytes)
        self._latency: deque = deque()     # (t, stage, seconds)

        self._sources: dict = {}           # source -> SequenceAudit
        self._clock: float | None = None   # the latest time accepted so far
        self.tx_attempts = 0
        self.rejected = 0
        self.last_rejection = ""

        # Continuity, kept outside the window on purpose. A window answers "how fast
        # lately"; it cannot answer "has this stopped", because the evidence of having
        # stopped is precisely what falls out of it.
        self._last_tx_at: float | None = None
        self._longest_gap = 0.0

    # --- what the loop reports --------------------------------------------

    def on_rx(self, now: float, seq: int, nbytes: int, source=0) -> SeqEvent:
        """One message received, with the sender's own sequence counter.

        ``source`` keys the sequence run. Two senders folded into one run produce holes
        that are arithmetic rather than loss, so they are kept apart; for MAVLink it is
        ``(sysid, compid)``.

        **The whole stream has to be reported.** Filtering by message type upstream
        manufactures holes indistinguishable from loss.
        """
        # Everything that can decline this reception is checked here, before the shared
        # clock moves and before a source is registered. Delegating the sequence check
        # and committing the clock first meant a reception refused for a malformed
        # number still imposed its date on everything after it: an instrument last
        # updated at t=0 and handed a bad number stamped t=10 then refused every valid
        # call until t=10 arrived, receptions, emissions, latency samples and reads
        # alike. A refusal has to leave the instrument where it was.
        problem = (_clock_problem(now, "reception time")
                   or self._order_problem(now, "reception")
                   or _bytes_problem(nbytes))
        if problem:
            return self._decline(problem, SeqEvent.REJECTED)

        # The sequence number is judged by the audit, which is where that rule lives.
        # **Nothing is kept until it accepts** -- not the source, and above all not the
        # clock. Committing the date first meant a reception declined for a malformed
        # number still imposed it on everything afterwards: an instrument last updated
        # at t=0 and handed a bad number stamped t=10 refused every valid call until
        # t=10 arrived, receptions, emissions, latency samples and reads alike.
        audit = self._sources.get(source)
        first_from_this_source = audit is None
        if first_from_this_source:
            audit = SequenceAudit(self.policy)

        event = audit.on_rx(seq, now)
        if event is SeqEvent.REJECTED:
            return self._decline(audit.last_rejection, SeqEvent.REJECTED)

        if first_from_this_source:
            self._sources[source] = audit
        self._clock = now
        self._rx.append((now, int(nbytes), source))
        return event

    def on_tx(self, now: float, nbytes: int) -> bool:
        """A send cycle. Returns whether anything actually went out.

        ``nbytes`` is what left on this cycle, normally a byte-counter delta. **Zero is
        not an emission**: it is a cycle that produced nothing, which is a different
        event and is counted as an attempt. The ported loop called this every cycle
        regardless, so the link looked continuously active while nothing was leaving,
        and the silence metric could never fire.
        """
        problem = (_clock_problem(now, "emission time")
                   or self._order_problem(now, "emission")
                   or _bytes_problem(nbytes))
        if problem:
            self._decline(problem, None)
            return False

        self._clock = now
        if nbytes == 0:
            self.tx_attempts += 1
            return False

        # The wait before the first emission is a silence like any other. Measuring
        # only the gaps *between* emissions meant that a link which said nothing for
        # ten seconds and then spoke had, from that moment, never been silent at all.
        since = self._last_tx_at if self._last_tx_at is not None else self._started_at
        self._longest_gap = max(self._longest_gap, now - since)
        self._last_tx_at = now
        self._tx.append((now, int(nbytes)))
        return True

    def on_latency(self, now: float, seconds: float, stage: str) -> bool:
        """One latency sample, in seconds, for a **named** stage.

        The name is required and there is no default. A latency figure means nothing
        without saying which part of the chain it spans, and "capture to command" is a
        claim about the whole of it that has to be earned stage by stage: a producer
        that cannot date its own capture cannot contribute to that number at all. See
        :class:`argos.perception.Frame`.

        A duration cannot be negative. One used to be accepted and went straight into
        the published percentiles.
        """
        problem = _clock_problem(now, "sample time") or self._order_problem(now, "sample")
        if not problem and (not isinstance(stage, str) or not stage):
            problem = f"a latency sample needs a stage name, got {stage!r}"
        if not problem and not _is_finite(seconds):
            problem = f"latency is not finite: {seconds!r}"
        if not problem and seconds < 0.0:
            problem = f"latency cannot be negative: {seconds!r}"
        if problem:
            self._decline(problem, None)
            return False

        self._clock = now
        self._latency.append((now, stage, float(seconds)))
        return True

    # --- what a display or a report reads ----------------------------------

    def snapshot(self, now: float) -> LinkSnapshot:
        """Rates over the window, continuity over the run.

        Validated before anything is pruned or confirmed: reading at ``inf`` used to
        empty every series, so one bad call destroyed the history the next good one
        would have reported.
        """
        if not _is_finite(now):
            raise ValueError(f"LinkStats.snapshot needs a finite clock, got {now!r}")
        if self._clock is not None and now < self._clock:
            # A read in the past would report emissions that had not happened yet and
            # a silence measured against a future instant. Nothing here keeps the
            # history such a question would need, so it is refused rather than answered
            # with whatever the current state happens to look like.
            raise ValueError(
                f"LinkStats.snapshot was asked about {now!r}, before {self._clock!r} "
                f"which it has already accepted: it keeps no history to answer with"
            )

        self._clock = now
        self._prune(now)
        window = self.window

        by_source = {key: audit.counts(now) for key, audit in self._sources.items()}
        combined = SeqCounts(
            arrivals=sum(c.arrivals for c in by_source.values()),
            distinct=sum(c.distinct for c in by_source.values()),
            in_order=sum(c.in_order for c in by_source.values()),
            duplicates=sum(c.duplicates for c in by_source.values()),
            reordered=sum(c.reordered for c in by_source.values()),
            missing=sum(c.missing for c in by_source.values()),
            late=sum(c.late for c in by_source.values()),
            pending=sum(c.pending for c in by_source.values()),
            ambiguous=sum(c.ambiguous for c in by_source.values()),
            rejected=sum(c.rejected for c in by_source.values()),
        )

        stages: dict[str, list[float]] = {}
        for _, stage, seconds in self._latency:
            stages.setdefault(stage, []).append(seconds)

        since = self._last_tx_at if self._last_tx_at is not None else self._started_at
        ongoing = max(0.0, now - since)

        return LinkSnapshot(
            window=window,
            rx=len(self._rx),
            rx_hz=len(self._rx) / window,
            rx_bytes_per_s=sum(b for _, b, _ in self._rx) / window,
            tx=len(self._tx),
            tx_hz=len(self._tx) / window,
            tx_bytes_per_s=sum(b for _, b in self._tx) / window,
            tx_attempts=self.tx_attempts,
            rejected=self.rejected,
            ongoing_silence=ongoing,
            longest_silence=max(self._longest_gap, ongoing),
            sequence=combined,
            by_source=by_source,
            latency={
                stage: LatencySummary(
                    stage=stage,
                    samples=len(values),
                    p50=_percentile(values, 0.50),
                    p95=_percentile(values, 0.95),
                )
                for stage, values in stages.items()
            },
        )

    # --- internals ---------------------------------------------------------

    def _order_problem(self, now: float, what: str) -> str:
        if self._clock is not None and now < self._clock:
            return (
                f"{what} time {now!r} is earlier than {self._clock!r}, already "
                f"accepted: this instrument requires calls in time order"
            )
        return ""

    def _decline(self, problem: str, result):
        self.rejected += 1
        self.last_rejection = problem
        return result

    def _prune(self, now: float) -> None:
        limit = now - self.window
        for series in (self._rx, self._tx, self._latency):
            while series and series[0][0] < limit:
                series.popleft()
