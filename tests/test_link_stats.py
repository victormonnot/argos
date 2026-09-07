"""Link accounting, tested against runs whose truth is known by construction.

No radio and no protocol: every sequence below is written out, so what the instrument
should say is not a matter of opinion. That is the point of keeping this module free of
dependencies -- the same tests would hold against a CRSF link.

The three defects being guarded against were all reproduced in the ported code, and
their numbers appear here as literals rather than as prose.
"""
from __future__ import annotations

import math

import pytest

from argos.harness import LinkStats, SeqCounts, SeqEvent, SeqPolicy, SequenceAudit


def run(sequence, policy=None, step=0.01, start=0.0):
    """Feed a sequence run at a steady cadence and return the audit."""
    audit = SequenceAudit(policy)
    for i, seq in enumerate(sequence):
        audit.on_rx(seq, now=start + i * step)
    return audit


# --------------------------------------------------------------------------
# The two reproductions from the audit
# --------------------------------------------------------------------------


def test_a_burst_too_large_to_attribute_is_not_reported_as_no_loss() -> None:
    """0 -> 66 is sixty-five missing messages, and the ported code called it 0 % loss.

    It classified any jump past a threshold as "disorder" and attributed nothing, so a
    burst larger than the threshold vanished from the measurement entirely -- the
    failure mode that flatters, on the metric a degraded-link result would be quoted
    from.

    It is still not attributed here, because on an 8-bit counter this shape is also
    what a restart and a very late message look like. The difference is that it is
    counted, and that the loss figure refuses to exist while it is unexplained.
    """
    counts = run([0, 66]).counts(1.0)

    assert counts.ambiguous == 1
    assert counts.loss is None, "a run with an unattributable jump has no loss figure"
    assert not counts.decidable
    assert counts.missing == 0        # nothing is invented either


def test_a_complete_but_reordered_run_reports_no_loss() -> None:
    """`[10, 12, 11, 13]` has nothing missing, and the ported code reported 33.33 %.

    It counted the gap at 12 immediately and never went back when 11 arrived. A gap is
    provisional here until a late message can no longer plausibly turn up.
    """
    counts = run([10, 12, 11, 13]).counts(0.04)

    assert counts.arrivals == counts.distinct == 4
    assert counts.reordered == 1
    assert counts.missing == 0
    assert counts.pending == 0
    assert counts.loss == 0.0


def test_a_real_gap_is_still_a_loss() -> None:
    """The control both reproductions need: nothing above weakens a genuine hole."""
    counts = run([10, 12, 13]).counts(1.0)

    assert counts.missing == 1
    assert counts.loss == pytest.approx(0.25)     # one lost out of four expected


# --------------------------------------------------------------------------
# What each sequence number is
# --------------------------------------------------------------------------


def test_an_uninterrupted_run_is_entirely_in_order() -> None:
    audit = run(range(20))
    counts = audit.counts(1.0)

    assert counts.in_order == 19
    assert counts.loss == 0.0
    assert counts.decidable


def test_the_first_message_is_not_compared_against_anything() -> None:
    audit = SequenceAudit()
    assert audit.on_rx(42, now=0.0) is SeqEvent.FIRST
    assert audit.counts(1.0).loss == 0.0


def test_the_same_number_twice_is_a_duplicate_and_not_a_loss() -> None:
    audit = SequenceAudit()
    audit.on_rx(7, now=0.0)
    assert audit.on_rx(7, now=0.01) is SeqEvent.DUPLICATE
    assert audit.counts(1.0).missing == 0


def test_a_gap_is_provisional_until_a_late_message_can_no_longer_arrive() -> None:
    """The mechanism the reordering case rests on, isolated.

    Confirming a gap the moment it is seen is what turned a complete run into a third
    of it lost. Waiting forever would be the opposite mistake, so the wait is a stated
    policy with a number on it.
    """
    audit = SequenceAudit(SeqPolicy(reorder_hold=0.5))
    audit.on_rx(1, now=0.0)
    audit.on_rx(3, now=0.1)

    provisional = audit.counts(0.2)
    assert provisional.pending == 1 and provisional.missing == 0

    confirmed = audit.counts(0.7)
    assert confirmed.pending == 0 and confirmed.missing == 1


def test_a_message_arriving_after_the_hold_no_longer_retracts_its_gap() -> None:
    """Once a loss is confirmed it stays confirmed: the run has already been reported.

    The message is still counted as reordered, so the two facts remain visible: one
    was declared lost, and one arrived very late. Quietly undoing the first would make
    a metric that changes retroactively.
    """
    audit = SequenceAudit(SeqPolicy(reorder_hold=0.2))
    audit.on_rx(1, now=0.0)
    audit.on_rx(3, now=0.1)
    audit.counts(0.5)                       # the gap is confirmed here

    assert audit.on_rx(2, now=0.6) is SeqEvent.REORDER
    counts = audit.counts(0.6)
    assert counts.missing == 1 and counts.reordered == 1


def test_the_counter_wrapping_is_ordinary_arithmetic() -> None:
    """255 -> 0 -> 1 is three consecutive messages, not a catastrophe."""
    counts = run([254, 255, 0, 1]).counts(1.0)

    assert counts.in_order == 3
    assert counts.loss == 0.0


def test_a_restart_landing_inside_the_gap_limit_is_counted_as_loss() -> None:
    """A limit of the instrument, asserted rather than described.

    A sender restarting its counter from 250 to 0 is the same arithmetic as five lost
    messages, and no amount of care distinguishes them on a counter this narrow. The
    measurement says loss because that is the more common cause, and the module says so
    in as many words rather than implying a certainty it does not have.
    """
    counts = run([250, 0]).counts(1.0)

    assert counts.missing == 5
    assert counts.loss == pytest.approx(5 / 7)
    assert counts.decidable       # wrongly confident, and knowably so: see the docstring


def test_a_restart_from_further_back_is_left_unattributed() -> None:
    """The same restart from 200 exceeds the gap limit and is not attributed at all."""
    counts = run([200, 0]).counts(1.0)

    assert counts.ambiguous == 1
    assert counts.loss is None


def test_two_senders_are_two_runs() -> None:
    """Folding them together produces holes that are arithmetic, not loss."""
    link = LinkStats(started_at=0.0)
    for i in range(10):
        link.on_rx(now=i * 0.01, seq=i, nbytes=20, source=(1, 1))
        link.on_rx(now=i * 0.01, seq=100 + i, nbytes=20, source=(2, 1))

    snapshot = link.snapshot(0.5)
    assert snapshot.sequence.loss == 0.0
    assert len(snapshot.by_source) == 2
    assert all(counts.missing == 0 for counts in snapshot.by_source.values())


def test_one_unattributable_source_removes_the_combined_loss_figure() -> None:
    """A single number over several runs is only a fact while every run is decidable."""
    link = LinkStats(started_at=0.0)
    link.on_rx(now=0.0, seq=0, nbytes=20, source="a")
    link.on_rx(now=0.01, seq=1, nbytes=20, source="a")
    link.on_rx(now=0.02, seq=0, nbytes=20, source="b")
    link.on_rx(now=0.03, seq=66, nbytes=20, source="b")

    snapshot = link.snapshot(1.0)
    assert snapshot.by_source["a"].loss == 0.0
    assert snapshot.sequence.loss is None
    assert snapshot.sequence.ambiguous == 1


def test_an_input_this_instrument_cannot_use_is_declined_and_changes_nothing() -> None:
    """Declined rather than raised: a counter is not worth stopping a control loop over.

    Refusing is only half of it. The refusal has to happen **before** anything moves,
    or a bad call leaves the instrument in a state the next good one cannot recover
    from: a reception timestamped ``NaN`` used to open a gap whose age could never be
    compared against anything, so it never expired and never became a loss.
    """
    audit = SequenceAudit()
    audit.on_rx(0, now=0.0)

    assert audit.on_rx(3.5, now=0.1) is SeqEvent.REJECTED      # type: ignore[arg-type]
    assert audit.on_rx(2, now=math.nan) is SeqEvent.REJECTED

    settled = audit.counts(0.15)
    assert settled.rejected == 2
    assert settled.arrivals == 1
    assert settled.pending == 0 and settled.missing == 0

    assert audit.on_rx(1, now=0.2) is SeqEvent.IN_ORDER        # and it carries on


@pytest.mark.parametrize(
    "kwargs",
    [
        {"modulus": 1},
        {"gap_limit": 0},
        {"gap_limit": 256},
        {"reorder_window": 0},
        {"gap_limit": 200, "reorder_window": 100},
        {"reorder_hold": -1.0},
        {"reorder_hold": math.nan},
    ],
    ids=str,
)
def test_a_policy_that_cannot_classify_anything_is_refused(kwargs) -> None:
    """In particular a gap limit and a reordering window that overlap.

    With `gap_limit + reorder_window >= modulus` there are numbers that are both "far
    ahead" and "slightly behind", and the classification stops being a partition of the
    counter. Refused at construction, where the traceback points at the choice.
    """
    with pytest.raises(ValueError):
        SeqPolicy(**kwargs)


# --------------------------------------------------------------------------
# Silence, and what an emission is
# --------------------------------------------------------------------------


def test_a_silence_that_is_still_going_on_is_counted() -> None:
    """The audit's reproduction, with its numbers.

    Emissions at 0 and 0.1 and nothing since. The ported code reported 0.1 s of maximum
    silence at t=2.9 and **0.0 s at t=3.2**, because its three-second window had
    dropped both emissions: the longer the link stayed dead, the healthier it looked.
    """
    link = LinkStats(window=3.0, started_at=0.0)
    link.on_tx(0.0, 280)
    link.on_tx(0.1, 280)

    assert link.snapshot(2.9).longest_silence == pytest.approx(2.8)
    assert link.snapshot(3.2).longest_silence == pytest.approx(3.1)
    assert link.snapshot(30.0).longest_silence == pytest.approx(29.9)


def test_a_link_that_never_emitted_has_been_silent_since_the_run_started() -> None:
    """Not zero. Nothing having happened yet is not the same as nothing being wrong."""
    link = LinkStats(started_at=100.0)

    assert link.snapshot(105.0).ongoing_silence == pytest.approx(5.0)


def test_a_gap_that_ended_is_still_part_of_the_record() -> None:
    """Continuity is not a rate, so recovering does not erase the outage.

    A window answers "how fast lately". It cannot answer "did this stop", because the
    evidence of having stopped is exactly what falls out of it.
    """
    link = LinkStats(window=1.0, started_at=0.0)
    link.on_tx(0.0, 100)
    link.on_tx(5.0, 100)                       # five seconds of nothing
    for i in range(10):
        link.on_tx(5.1 + i * 0.02, 100)        # and then a healthy stream

    snapshot = link.snapshot(5.3)
    assert snapshot.ongoing_silence < 0.1      # right now it is fine
    assert snapshot.longest_silence == pytest.approx(5.0)   # and it was not


def test_a_cycle_that_sent_nothing_is_an_attempt_and_not_an_emission() -> None:
    """The ported loop called this every cycle, whatever the byte counter did.

    Every cycle then looked like an emission, so the silence figure could never fire
    however dead the link was. An attempt, an emission and an applied effect are three
    events, and only the middle one is evidence that anything left.
    """
    link = LinkStats(started_at=0.0)
    for i in range(50):
        assert link.on_tx(i * 0.02, 0) is False

    snapshot = link.snapshot(1.0)
    assert snapshot.tx == 0
    assert snapshot.tx_attempts == 50
    assert snapshot.ongoing_silence == pytest.approx(1.0)


def test_an_emission_says_so_and_moves_the_silence() -> None:
    link = LinkStats(started_at=0.0)
    assert link.on_tx(0.5, 280) is True
    assert link.snapshot(0.6).ongoing_silence == pytest.approx(0.1)
    assert link.snapshot(0.6).tx == 1


# --------------------------------------------------------------------------
# Rates and latency
# --------------------------------------------------------------------------


def test_rates_are_measured_over_the_window_and_only_over_it() -> None:
    link = LinkStats(window=1.0, started_at=0.0)
    for i in range(100):
        link.on_rx(now=i * 0.01, seq=i % 256, nbytes=10)
        link.on_tx(i * 0.01, 20)

    inside = link.snapshot(0.99)
    assert inside.rx_hz == pytest.approx(100.0, rel=0.05)
    assert inside.rx_bytes_per_s == pytest.approx(1000.0, rel=0.05)
    assert inside.tx_bytes_per_s == pytest.approx(2000.0, rel=0.05)

    later = link.snapshot(10.0)
    assert later.rx_hz == 0.0                  # the traffic is old news
    assert later.longest_silence > 8.0         # but the outage is not


def test_a_latency_sample_has_to_say_which_stage_it_measured() -> None:
    """"Capture to command" is a claim about the whole chain, earned stage by stage.

    A producer that cannot date its own capture cannot contribute to that number at
    all, which is why the name is required and has no default.
    """
    link = LinkStats(started_at=0.0)
    assert link.on_latency(0.0, 0.02, "") is False
    assert link.on_latency(0.0, math.nan, "detect") is False
    assert link.on_latency(0.0, -0.2, "detect") is False, "a duration cannot be negative"

    assert link.snapshot(1.0).latency == {}
    assert link.snapshot(1.0).rejected == 3


def test_latency_is_reported_per_stage() -> None:
    link = LinkStats(window=10.0, started_at=0.0)
    for i in range(10):
        link.on_latency(i * 0.1, 0.010 + i * 0.001, "detect")
        link.on_latency(i * 0.1, 0.050 + i * 0.001, "receipt_to_command")

    latency = link.snapshot(1.0).latency
    assert set(latency) == {"detect", "receipt_to_command"}
    assert latency["detect"].samples == 10
    assert latency["detect"].p50 < latency["receipt_to_command"].p50


def test_an_empty_snapshot_is_readable() -> None:
    """Nothing has happened is a state a display has to be able to render."""
    snapshot = LinkStats(started_at=0.0).snapshot(0.0)

    assert snapshot.rx == 0 and snapshot.tx == 0
    assert snapshot.sequence == SeqCounts()
    assert snapshot.sequence.loss == 0.0
    assert snapshot.latency == {}


@pytest.mark.parametrize("kwargs", [{"window": 0.0}, {"window": math.nan},
                                    {"started_at": math.inf}], ids=str)
def test_a_link_configured_with_nonsense_is_refused(kwargs) -> None:
    with pytest.raises(ValueError):
        LinkStats(**kwargs)


def test_a_provisional_gap_is_visible_in_the_pessimistic_bound() -> None:
    """`loss` alone reads flatteringly while a gap is still inside the hold.

    A display polling faster than ``reorder_hold`` would otherwise show a link losing
    half its traffic as losing none: every gap is always still provisional. The two
    figures are the decided and the undecided readings of the same run, and they
    converge once the run outlasts the hold.
    """
    audit = SequenceAudit(SeqPolicy(reorder_hold=0.5))
    audit.on_rx(1, now=0.0)
    audit.on_rx(3, now=0.1)

    early = audit.counts(0.2)
    assert early.loss == 0.0                       # nothing confirmed lost yet
    assert early.pending == 1
    assert early.loss_bound == pytest.approx(1 / 3)

    settled = audit.counts(0.7)
    assert settled.loss == settled.loss_bound == pytest.approx(1 / 3)


def test_the_pessimistic_bound_is_also_withheld_when_nothing_can_be_attributed() -> None:
    counts = run([0, 66]).counts(1.0)
    assert counts.loss is None and counts.loss_bound is None


# --------------------------------------------------------------------------
# Traffic and identity are two different counts
# --------------------------------------------------------------------------


def test_a_message_delivered_twice_was_delivered_once() -> None:
    """A duplicate is traffic, not a delivery, and the loss ratio needs the second.

    Counting arrivals in the denominator lowers the reported loss without anything
    having been received that was not received before. One retransmission took a
    quarter of the run being missing down to a fifth of it.
    """
    counts = run([10, 12, 13, 13]).counts(1.0)

    assert counts.arrivals == 4
    assert counts.distinct == 3
    assert counts.duplicates == 1
    assert counts.missing == 1
    assert counts.loss == pytest.approx(0.25)


def test_a_hundred_retransmissions_do_not_repair_a_link() -> None:
    """The same thing at a scale that makes it unmistakable.

    A sender repeating one packet a hundred times over a link that dropped a quarter
    of its traffic reported **0.96 % loss**. Nothing had arrived that had not arrived
    already.
    """
    audit = run([10, 12, 13] + [13] * 100)
    counts = audit.counts(2.0)      # after the run, which is 103 arrivals long

    assert counts.arrivals == 103
    assert counts.distinct == 3
    assert counts.loss == pytest.approx(0.25)


def test_a_duplicate_behind_the_frontier_is_not_a_new_message_either() -> None:
    """And it does not arrive labelled as one.

    `[10, 12, 11, 14, 11]` repeats 11 *behind* the frontier, so the second copy is
    classified as reordering rather than as a duplicate of the frontier. Subtracting a
    duplicate counter from the denominator would therefore not have fixed this; asking
    the position whether it had already been delivered does.
    """
    audit = SequenceAudit()
    events = [audit.on_rx(seq, now=0.01 * i) for i, seq in enumerate([10, 12, 11, 14, 11])]
    counts = audit.counts(1.0)

    assert events[-1] is SeqEvent.DUPLICATE
    assert counts.arrivals == 5
    assert counts.distinct == 4
    assert counts.missing == 1              # 13 never came
    assert counts.loss == pytest.approx(0.2)


def test_a_message_that_arrives_after_being_declared_lost_stays_declared_lost() -> None:
    """The semantics, stated: loss here means *not delivered within the hold*.

    A figure that silently revises itself downward describes a past that has changed,
    so the message stays counted. What it must not do is count twice -- once as a loss
    and once as an arrival -- which is what turned a defect on one message in three
    into a quarter.
    """
    audit = SequenceAudit(SeqPolicy(reorder_hold=0.2))
    audit.on_rx(1, now=0.0)
    audit.on_rx(3, now=0.1)
    audit.counts(0.5)                        # the gap is confirmed here
    audit.on_rx(2, now=0.6)                  # and then it turns up anyway

    counts = audit.counts(0.6)
    assert counts.missing == 1
    assert counts.late == 1
    assert counts.distinct == 3
    assert counts.expected == 3, "three distinct messages were sent, not four"
    assert counts.loss == pytest.approx(1 / 3)


def test_the_combined_figure_uses_distinct_messages_too() -> None:
    """The aggregation across sources has the same denominator as each source."""
    link = LinkStats(started_at=0.0)
    for i, seq in enumerate([10, 12, 13, 13]):
        link.on_rx(now=0.01 * i, seq=seq, nbytes=20, source="a")
    for i, seq in enumerate([50, 51, 52]):
        link.on_rx(now=0.04 + 0.01 * i, seq=seq, nbytes=20, source="b")

    combined = link.snapshot(1.0).sequence
    assert combined.arrivals == 7
    assert combined.distinct == 6
    assert combined.loss == pytest.approx(1 / 7)


# --------------------------------------------------------------------------
# A wrapped counter is not an identity
# --------------------------------------------------------------------------


def wire(ordinals, modulus=256):
    """What a sender emitting these positions actually puts on the wire."""
    return [o % modulus for o in ordinals]


def test_two_holes_a_full_turn_apart_are_two_holes() -> None:
    """They wore the same number, and were remembered as one.

    Positions 1 and 257 are both `1` on an 8-bit counter. Keyed by that number, the
    second gap found the first already recorded and vanished into it: two lost messages
    were reported as one, with no ambiguity declared. Keyed by position, they are two.
    """
    sent = [o for o in range(259) if o not in (1, 257)]
    audit = SequenceAudit()
    for i, seq in enumerate(wire(sent)):
        audit.on_rx(seq, now=i / 1024.0)

    counts = audit.counts(1.0)
    assert counts.missing == 2
    assert counts.decidable
    assert counts.distinct == 257


def test_a_late_message_cannot_retract_a_hole_from_another_turn() -> None:
    """The same collision, reached from the other side.

    Position 1 is missing. Position 257 arrives after 258 -- ordinary reordering, one
    turn later, and also wearing the number `1`. It used to fill the hole left by
    position 1 and take the run to zero loss.
    """
    sent = [0, *range(2, 257), 258, 257]
    audit = SequenceAudit()
    for i, seq in enumerate(wire(sent)):
        audit.on_rx(seq, now=i / 1024.0)

    counts = audit.counts(1.0)
    assert counts.missing == 1
    assert counts.reordered == 1
    assert counts.decidable


def test_a_long_complete_run_across_several_turns_is_still_complete() -> None:
    """The control: wrapping on its own is ordinary arithmetic, as it was before."""
    audit = SequenceAudit()
    for i, seq in enumerate(wire(range(600))):
        audit.on_rx(seq, now=i / 1024.0)

    counts = audit.counts(1.0)
    assert counts.distinct == 600
    assert counts.missing == 0
    assert counts.loss == 0.0


# --------------------------------------------------------------------------
# Nothing changes before the input has been checked
# --------------------------------------------------------------------------


def test_an_emission_at_an_unusable_time_does_not_move_the_silence() -> None:
    """Refused, and refused *first*.

    A timestamp of ``NaN`` reaching the record left the silence measured from a moment
    that cannot be compared with anything: at t=30 the link reported no silence at all.
    """
    link = LinkStats(started_at=0.0)
    link.on_tx(0.0, 280)

    assert link.on_tx(math.nan, 280) is False

    snapshot = link.snapshot(30.0)
    assert snapshot.longest_silence == pytest.approx(30.0)
    assert snapshot.rejected == 1


def test_a_bad_byte_count_does_not_move_the_last_emission_first() -> None:
    """The half-applied call, which is worse than either outcome on its own.

    The instant was recorded, then the byte count raised. The exception was visible;
    what was not is that the silence had already been shortened by exactly the interval
    the caller got wrong.
    """
    link = LinkStats(started_at=0.0)
    link.on_tx(0.0, 280)

    assert link.on_tx(1.0, math.nan) is False          # type: ignore[arg-type]
    assert link.on_tx(1.0, -5) is False

    assert link.snapshot(2.0).ongoing_silence == pytest.approx(2.0)


def test_a_reception_with_an_unusable_byte_count_reaches_no_counter() -> None:
    """Both counters or neither: the sequence audit used to move before the refusal."""
    link = LinkStats(started_at=0.0)
    link.on_rx(now=0.0, seq=0, nbytes=20)

    assert link.on_rx(now=0.1, seq=1, nbytes=math.nan) is SeqEvent.REJECTED  # type: ignore[arg-type]

    snapshot = link.snapshot(0.2)
    assert snapshot.rx == 1
    assert snapshot.sequence.arrivals == 1


def test_a_read_at_an_unusable_time_raises_without_destroying_the_history() -> None:
    """A read is a question, and there is no answer to one asked at infinity.

    It raises rather than declining, because the caller has made a mistake at the call
    site. It raises *before* pruning, because an earlier version emptied every series
    on the way to failing, so one bad read cost the history the next good one would
    have reported.
    """
    link = LinkStats(started_at=0.0)
    link.on_tx(0.0, 280)

    with pytest.raises(ValueError):
        link.snapshot(math.inf)
    with pytest.raises(ValueError):
        SequenceAudit().counts(math.nan)

    assert link.snapshot(0.5).tx == 1


# --------------------------------------------------------------------------
# The silence before the first emission is a silence
# --------------------------------------------------------------------------


def test_the_wait_before_the_first_emission_stays_in_the_record() -> None:
    """A link that said nothing for ten seconds did not become punctual by speaking.

    The historical maximum was only ever updated *between* emissions, so the first one
    reset it to zero -- the moment the ten-second silence ended was the moment it
    stopped being reported.
    """
    link = LinkStats(window=3.0, started_at=0.0)
    assert link.snapshot(5.0).longest_silence == pytest.approx(5.0)

    link.on_tx(10.0, 280)

    settled = link.snapshot(10.0)
    assert settled.ongoing_silence == pytest.approx(0.0)
    assert settled.longest_silence == pytest.approx(10.0)


def test_the_initial_silence_is_kept_even_if_nobody_was_watching() -> None:
    """No read in between, and the answer is the same. It is a fact, not a side effect."""
    link = LinkStats(window=3.0, started_at=100.0)
    link.on_tx(110.0, 280)

    assert link.snapshot(110.0).longest_silence == pytest.approx(10.0)


def test_the_initial_silence_survives_the_rate_window_moving_on() -> None:
    """Continuity is not a rate, so the record does not expire with the traffic."""
    link = LinkStats(window=1.0, started_at=0.0)
    link.on_tx(10.0, 280)
    for i in range(20):
        link.on_tx(10.1 + i * 0.02, 280)

    snapshot = link.snapshot(10.5)
    assert snapshot.tx_hz > 0.0                        # busy right now
    assert snapshot.longest_silence == pytest.approx(10.0)


def test_a_policy_field_that_is_not_an_integer_is_refused_at_construction() -> None:
    """It used to be accepted and to fail later, on an ordinary reception.

    A modulus of 256.5 is a mistake in a line of configuration; reporting it from
    inside the arithmetic of a message that had nothing to do with it sends a reader to
    the wrong place.
    """
    with pytest.raises(ValueError):
        SeqPolicy(modulus=256.5)               # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SeqPolicy(gap_limit=8.0)               # type: ignore[arg-type]


def test_an_ambiguity_does_not_let_a_later_message_reach_a_hole_from_before_it() -> None:
    """Positions only ever move forward, and that is what keeps generations apart.

    After a jump nothing can attribute, the run carries on being counted -- the ratios
    stop being facts, the raw counts do not. What must not happen is the frontier
    dropping back onto positions already recorded, because a late message would then
    fill a hole belonging to an earlier turn of the counter and the remaining counts
    would be wrong too.

    Here a hole is left at position 1, the run continues past a full wrap, an
    unattributable jump lands on a low wire number, and a late message arrives just
    behind it. That message is a different message; the hole is still a hole.
    """
    audit = SequenceAudit(SeqPolicy(reorder_hold=1.0))
    step = 1 / 1024
    now = 0.0
    sent = [0, *range(2, 256), *(o % 256 for o in range(256, 307))]
    for seq in sent:
        audit.on_rx(seq, now=now)
        now += step

    assert audit.counts(now).pending == 1          # position 1, still open

    assert audit.on_rx(5, now=now) is SeqEvent.AMBIGUOUS
    now += step
    late = audit.on_rx(1, now=now)                 # wears the same number as the hole

    assert late is SeqEvent.AMBIGUOUS, (
        "a message behind the frontier but before this generation began cannot be "
        "placed either: which run it belongs to is exactly what was unattributable"
    )
    counts = audit.counts(5.0)
    assert counts.ambiguous == 2
    assert counts.missing == 1, "the hole from before the ambiguity is still a hole"
    assert counts.loss is None                     # and the ratio is still withheld


# --------------------------------------------------------------------------
# Calls arrive in time order, and sequence numbers do not have to
# --------------------------------------------------------------------------


def test_sequence_numbers_may_arrive_in_any_order_as_long_as_the_clock_does_not() -> None:
    """The distinction the whole contract rests on, asserted first.

    Out-of-order *sequence numbers* are the ordinary case and the thing this module
    exists to measure. Out-of-order *call times* are a broken clock or a replayed log,
    and they invalidate measurements already taken. Requiring the second must not cost
    the first.
    """
    audit = SequenceAudit()
    events = [audit.on_rx(seq, now=0.01 * i) for i, seq in enumerate([10, 12, 11, 13])]

    assert events[2] is SeqEvent.REORDER
    assert audit.counts(0.05).loss == 0.0


def test_messages_handed_over_at_the_same_instant_are_accepted() -> None:
    """Several really can arrive together; the order required is not a strict one."""
    link = LinkStats(started_at=0.0)
    assert link.on_rx(now=1.0, seq=0, nbytes=20, source="a") is not SeqEvent.REJECTED
    assert link.on_rx(now=1.0, seq=100, nbytes=20, source="b") is not SeqEvent.REJECTED
    assert link.on_tx(1.0, 280) is True

    assert link.snapshot(1.0).rejected == 0


def test_a_backdated_emission_is_refused_and_does_not_move_the_silence() -> None:
    """Emissions at 0 and 2, then one claiming to be at 1.

    The series is trimmed from its front, so the stale entry survived a window that
    should have dropped it; and the last emission is whichever was reported last, so
    the silence reference walked backwards. Two emissions inside a three-second window
    at t=4.5, and 3.5 s of silence where there were 2.5.
    """
    link = LinkStats(window=3.0, started_at=0.0)
    link.on_tx(0.0, 280)
    link.on_tx(2.0, 280)

    assert link.on_tx(1.0, 280) is False

    snapshot = link.snapshot(4.5)
    assert snapshot.tx == 1                                  # only the one at 2.0
    assert snapshot.ongoing_silence == pytest.approx(2.5)
    assert snapshot.rejected == 1


def test_a_backdated_reception_is_refused_whichever_source_it_claims() -> None:
    """The clock is the link's, not each sender's.

    Delivering one source's traffic and then another's replays the run from the start,
    which is a shape a test fixture falls into easily and a real loop does not.
    """
    link = LinkStats(window=3.0, started_at=0.0)
    link.on_rx(now=2.0, seq=0, nbytes=20, source="a")

    assert link.on_rx(now=1.0, seq=0, nbytes=20, source="b") is SeqEvent.REJECTED

    assert link.snapshot(4.5).rx == 1


def test_a_backdated_latency_sample_cannot_survive_a_trim_it_should_not_have() -> None:
    """A nine-second sample dated before one already accepted took the 95th percentile."""
    link = LinkStats(window=3.0, started_at=0.0)
    link.on_latency(2.0, 0.2, "detect")

    assert link.on_latency(1.0, 9.0, "detect") is False

    latency = link.snapshot(4.5).latency
    assert latency["detect"].samples == 1
    assert latency["detect"].p95 == pytest.approx(0.2)


def test_a_read_in_the_past_is_refused_rather_than_answered() -> None:
    """It would report emissions that had not happened yet.

    Nothing here keeps the history such a question needs -- the series hold only what
    the window still covers -- so answering it with whatever the current state happens
    to look like would be inventing a past.
    """
    link = LinkStats(window=3.0, started_at=0.0)
    link.on_tx(0.0, 280)
    link.on_tx(10.0, 280)

    with pytest.raises(ValueError):
        link.snapshot(1.0)

    # ... and the refused read destroyed nothing: the emission still inside the window
    # is there, and so is the ten-second gap that continuity keeps outside it.
    recovered = link.snapshot(10.0)
    assert recovered.tx == 1
    assert recovered.longest_silence == pytest.approx(10.0)


def test_a_reception_dated_before_a_read_that_already_settled_the_run_is_refused() -> None:
    """Two notions of "the time" were deciding the same question.

    A gap confirmed by a read at t=1 cannot be un-confirmed by a message claiming to
    have arrived at t=0.25. It used to be accepted and then counted as arriving after
    the hold, although the date it carried was inside it.
    """
    audit = SequenceAudit(SeqPolicy(reorder_hold=0.5))
    audit.on_rx(1, now=0.0)
    audit.on_rx(3, now=0.125)
    assert audit.counts(1.0).missing == 1                     # settled here

    assert audit.on_rx(2, now=0.25) is SeqEvent.REJECTED

    counts = audit.counts(1.0)
    assert counts.missing == 1 and counts.late == 0
    assert counts.rejected == 1


def test_the_next_call_in_order_is_processed_normally() -> None:
    """A refusal costs the call, not the instrument."""
    link = LinkStats(window=3.0, started_at=0.0)
    link.on_tx(2.0, 280)
    assert link.on_tx(1.0, 280) is False

    assert link.on_tx(2.5, 280) is True
    assert link.snapshot(2.5).tx == 2


# --------------------------------------------------------------------------
# A generation boundary is not the same as a frontier
# --------------------------------------------------------------------------


def test_a_reordering_window_cannot_reach_across_an_unattributable_jump() -> None:
    """`gap_limit=2, reorder_window=8` is accepted, and it reaches back too far.

    Positions `[0, 2, 261, 257]` go out as `[0, 2, 5, 1]`. The jump to 261 is
    unattributable, so the reconstructed frontier is only 5; a message four positions
    behind it lands on position 1 -- the hole from the *previous* run -- and erased it.

    "The frontier only moves forward" does not rule this out: what crosses the break is
    the reach *behind* the frontier. With the default policy the smallest
    unattributable jump is wider than the reordering window, which is why this stayed
    invisible there.

    The ratios were already withheld after an ambiguity, so no false zero was
    published. What was destroyed is the partial information the documentation tells a
    reader to fall back on.
    """
    audit = SequenceAudit(SeqPolicy(gap_limit=2, reorder_window=8))
    events = [audit.on_rx(seq, now=i / 64) for i, seq in enumerate([0, 2, 5, 1])]

    assert events[2] is SeqEvent.AMBIGUOUS
    assert events[3] is SeqEvent.AMBIGUOUS, "which run it belongs to is unknowable"

    counts = audit.counts(1.0)
    assert counts.missing == 1, "the hole at position 1 is still a hole"
    assert counts.ambiguous == 2
    assert counts.loss is None


def test_reordering_still_works_normally_inside_one_generation() -> None:
    """The control: the boundary must not cost ordinary late messages.

    Same policy, no unattributable jump. A message arriving behind the frontier fills
    its gap exactly as before.
    """
    audit = SequenceAudit(SeqPolicy(gap_limit=2, reorder_window=8))
    events = [audit.on_rx(seq, now=i / 64) for i, seq in enumerate([0, 2, 1, 3])]

    assert events[2] is SeqEvent.REORDER
    assert audit.counts(1.0).loss == 0.0


def test_a_settled_run_cannot_be_asked_about_its_own_past() -> None:
    """A read confirms losses for good, so an earlier read has no answer left.

    ``counts`` is not a pure question: it is what turns a provisional gap into a
    confirmed loss. Asking it about an instant it has already moved past would report a
    run that was still undecided at the time, which is not a state this instrument
    keeps -- and answering with the settled one would date the confirmations wrongly.
    """
    audit = SequenceAudit(SeqPolicy(reorder_hold=0.5))
    audit.on_rx(1, now=0.0)
    audit.on_rx(3, now=0.1)
    assert audit.counts(1.0).missing == 1

    with pytest.raises(ValueError):
        audit.counts(0.2)

    assert audit.counts(1.0).missing == 1          # and it answers again in order


# --------------------------------------------------------------------------
# A refusal leaves the instrument where it was, the clock included
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seq", ["invalid", None, True, 3.5, math.nan],
    ids=["text", "none", "bool", "fractional", "nan"],
)
def test_a_reception_refused_for_its_sequence_number_does_not_take_the_clock_with_it(seq) -> None:
    """The chronological guard turned one bad number into a refusal of everything after.

    The date was committed before the sequence number was checked, so a reception
    declined at t=10 became a constraint the whole instrument had to meet: an
    instrument last updated at t=0 then refused every valid call until t=10 arrived --
    receptions, emissions, latency samples and reads alike. A refusal has to leave the
    instrument at its last accepted state.
    """
    link = LinkStats(started_at=0.0)
    link.on_rx(now=0.0, seq=0, nbytes=10)

    assert link.on_rx(now=10.0, seq=seq, nbytes=10) is SeqEvent.REJECTED  # type: ignore[arg-type]
    assert "sequence number" in link.last_rejection

    # ... and every path recovers at a time after the last accepted call and before
    # the one the refused call carried.
    assert link.on_rx(now=1.0, seq=1, nbytes=10) is SeqEvent.IN_ORDER
    assert link.on_tx(1.0, 280) is True
    assert link.on_latency(1.0, 0.02, "detect") is True

    snapshot = link.snapshot(1.0)
    assert snapshot.rx == 2 and snapshot.tx == 1
    assert snapshot.rejected == 1


def test_a_refused_reception_does_not_register_the_source_it_named() -> None:
    """A source the instrument never successfully heard from is not a source.

    Registering it before admission left a run in the report that had received nothing,
    which is the same partial application one level down.
    """
    link = LinkStats(started_at=0.0)

    assert link.on_rx(now=1.0, seq="bad", nbytes=10, source="ghost") is SeqEvent.REJECTED  # type: ignore[arg-type]

    assert link.snapshot(1.0).by_source == {}


def test_an_accepted_reception_does_advance_the_clock() -> None:
    """The control the fix must not trade away.

    Recovery is about refusals. A reception that was *accepted* at t=10 is a fact about
    when this instrument last heard something, and a call claiming to predate it is
    still the broken clock the guard exists for.
    """
    link = LinkStats(started_at=0.0)
    link.on_rx(now=10.0, seq=0, nbytes=10)

    assert link.on_rx(now=1.0, seq=1, nbytes=10) is SeqEvent.REJECTED
    assert link.on_tx(1.0, 280) is False
    with pytest.raises(ValueError):
        link.snapshot(1.0)

    assert link.snapshot(10.0).rx == 1
