"""Channel policy: the numbers in the link layer that describe the *channel*
rather than the protocol.

whale/link.py grew up against one channel -- two FM handhelds on 2 m, a few
metres apart -- and every timing constant in it was measured or reasoned
against that. None of those numbers is wrong, but most of them are not facts
about the protocol either: they are facts about VHF FM. A packet type id, a
sync word, a sequence mask and a header size are the same on any channel,
because both ends have to agree on them byte for byte. A retry budget, an
inactivity timeout and a keying length are not: they are a bet about how a
particular radio path behaves, and the same ARQ engine wants a different bet
on HF SSB, where the path fades, keying is slower, and the band is shared
with stations this modem cannot hear well.

So the channel bets are hoisted here, into one frozen dataclass a Link is
constructed with, and the protocol facts stay as module globals in
whale/link.py where they belong. Nothing in this module changes what goes on
air, and nothing here is negotiated: two stations may run different policies
against each other quite happily, which is the point -- policy is local.

Each field keeps the comment that was written where the constant used to
live. That reasoning is the reason the number is what it is, and it is the
first thing anyone changing it needs.
"""

import dataclasses
from typing import Callable

from whale import afsk, modes


@dataclasses.dataclass(frozen=True)
class ChannelPolicy:
    """One channel's worth of assumptions, handed to Link at construction.

    Frozen because a Link reads these throughout a session and nothing
    should be able to move the goalposts mid-transfer; a station that wants
    different numbers builds a new policy with `dataclasses.replace`.
    """

    #: Name for logs and diagnostics only. Never goes on air.
    name: str

    # -- turnaround ------------------------------------------------------
    #
    # Kept as a compatibility name for diagnostics/tests. On VHF FM no fixed
    # dead-air delay is applied: replies begin after the checked frame ends,
    # and calibrated head audio absorbs the effective direction-change loss.
    # A channel whose radios need real settling time before the reply is
    # believable sets this rather than widening the head pad.
    tx_turnaround_delay: float

    # -- how long a silent peer is tolerated -----------------------------
    #
    # How long a CONNECTED station will go without decoding anything at all
    # from its peer before tearing the session down.
    #
    # MEASURED on the VHF bench, both stations logged, by
    # scripts/measure_peer_gap.py -- which reads the worst gap between frames
    # decoded off the air out of each station's log. Three runs, because the
    # silences that matter are not the ones a clean run produces:
    #
    #   clean acceptance run, 1 KB each way          5.2s  (ht->ic705 leg;
    #                                                       4.0s the other way)
    #   a full max_retries cycle at 300 baud, forced
    #     by suppressing the first five DATA_ACKs
    #     (WHALE_DROP_PTYPE=DATA_ACK
    #      WHALE_DROP_NTH=1,2,3,4,5)                44.4s
    # The full retry cycle remains the longest active-session silence; 150s is
    # a little over 3x the measured 44.4s.
    #
    # Note the retry cycle measured 44.4s where the arithmetic says 34.8s (six
    # data_ack_timeouts). The formula counts only the waiting; the five
    # retransmissions in between are each a keying of their own, and their
    # airtime, PTT lead and turnaround land inside the same silence. That gap
    # between the computed and the measured figure is the reason this is
    # measured at all -- a reasoned constant here would have been ~20% short of
    # a case that occurs in normal operation.
    #
    # The margin on top is deliberately wide, because the cost of being wrong
    # is asymmetric: too long only delays a teardown that something else
    # usually beats to it (the peer's DISC, or the caller's parting DISC in
    # connect()), while too short kills a session that was about to recover.
    #
    # What this deliberately does NOT do is keep an idle session alive. A
    # station with a connection up and no user data to send transmits nothing,
    # so its peer decodes nothing, and after this long the session is torn
    # down. That is the accepted trade for not adding keepalive frames -- every
    # keepalive is a keying, on a link where a keying costs seconds of air time
    # and PTT wear. If sessions that idle longer than this ever need to
    # survive, the answer is a keepalive probe (send one, retry it, tear down
    # only when the probe itself goes unanswered), not a bigger number here.
    inactivity_timeout: float

    # -- ARQ patience ----------------------------------------------------
    #
    # How many times one chunk is sent before the transfer is declared
    # failed. Note what is NOT here: CHUNK_SIZE (payload bytes per DATA
    # frame -- kept small so a single real-hardware bit error, observed near
    # the tail of longer frames, only costs a short retransmit instead of
    # derailing a large chunk) and the frame-airtime-derived ACK timeout both
    # depend on the active mode, so they are computed per instance in
    # Link._apply_tx_profile / _apply_rx_profile rather than being either a
    # module constant or a policy field.
    max_retries: int

    #: Slack added on top of computed frame airtime when sizing an ACK
    #: timeout -- see Link._recompute_timings and Link.control_ack_timeout,
    #: which use the same figure for the data and control planes. It covers
    #: everything the airtime arithmetic does not: PTT lead and tail,
    #: output-stream startup, the peer's decode poll interval, and the
    #: scheduling jitter of two Python stations. Sized against VHF FM
    #: turnaround; a channel with slower T/R or a non-negligible propagation
    #: delay needs more.
    ack_timeout_slack: float

    # -- mid-session speed adaptation ------------------------------------
    #
    # Deliberately just ARQ-outcome based (no SNR estimate, no throughput
    # math). React fast to trouble, be conservative about speeding up.

    #: A chunk needing this many tries triggers an immediate step down.
    step_down_after_attempts: int

    # The clean-streak length needed to step up is not fixed: it starts at
    # `step_up_after_clean_streak_initial` (on VHF, one clean chunk earns a
    # step up) and grows by 1 every time a step down happens, so a session
    # that has been burned needs more evidence before it is trusted to speed
    # up again. Capped at `step_up_after_clean_streak_max` so a persistently
    # bad link doesn't make the threshold unbounded.
    step_up_after_clean_streak_initial: int
    step_up_after_clean_streak_max: int

    # -- keying length ---------------------------------------------------
    #
    # Sync-through-CRC audio is capped in duration, and every CPFSK profile's
    # chunk_size is whatever fits inside that cap. The outer head pad and
    # transport startup do not consume this budget because adaptive timing
    # varies them by radio pair.
    #
    # See afsk.MAX_USEFUL_FRAME_SECONDS for the full reasoning behind the
    # VHF figure -- retransmit granularity, half-duplex responsiveness, and
    # the clock tolerance a rigid symbol grid imposes. All three are
    # channel-shaped rather than protocol-shaped, which is why the budget is
    # policy: a receiver is happy to decode any frame it is told the length
    # of, so nothing about the on-air format changes when this does.
    #
    # Threaded into CPFSK profile construction via afsk.default_registry's
    # `budget` (see Link.__init__), so the number is not restated anywhere.
    # VF3's chunk_size is deliberately not derived from it: that mode's
    # payload is fixed by its OFDM frame structure, not by a time budget.
    max_useful_frame_seconds: float

    # -- listen before transmit ------------------------------------------
    #
    # Whether this station must hear the channel clear before keying. On the
    # VHF simplex pair this modem was built against, the two stations are the
    # only occupants and the ARQ turnaround alone keeps them off each other.
    # A shared HF band is not like that.
    #
    # UNUSED FOR NOW: there is no busy-channel detector in this codebase yet,
    # so nothing reads this. It is here so the policy that will need it can
    # already say so, and so the gap is visible in one place rather than
    # rediscovered when the detector lands.
    require_clear_channel: bool = False

    # -- which waveforms suit this channel -------------------------------
    #
    # Called as `mode_ladder(max_useful_frame_seconds)` by Link.__init__ when
    # it is not handed a registry outright, and returning the ModeRegistry
    # this station offers.
    #
    # This is on the same footing as every other field here: local, never on
    # air, and a bet about the channel rather than a protocol fact. Two
    # stations still negotiate from the mode ids they each advertise, so a
    # peer offering a ladder this one does not know simply never has those
    # rungs selected.
    #
    # It lives here rather than at each call site because the pairing is not
    # free to vary: the CPFSK profiles carry no carrier-frequency estimate,
    # so running the VHF ladder against HF_SSB's timeouts is not a slower
    # link but a broken one, and the reverse wastes the FM bench's whole
    # speed ladder. Keeping the two together makes that impossible to get
    # half right.
    mode_ladder: Callable[[float], object] = dataclasses.field(
        default=modes.default_registry, compare=False, repr=False)


#: The channel this modem was built, measured and accepted against: two FM
#: handhelds on 2 m simplex, a few metres apart, no other occupants. Every
#: value below is exactly what the corresponding constant held before it was
#: hoisted, so a Link constructed with this policy -- the default -- behaves
#: identically to the pre-policy code.
VHF_FM = ChannelPolicy(
    name="VHF FM",
    tx_turnaround_delay=0.0,
    inactivity_timeout=150.0,
    max_retries=6,
    ack_timeout_slack=3.0,
    step_down_after_attempts=3,
    step_up_after_clean_streak_initial=1,
    step_up_after_clean_streak_max=8,
    max_useful_frame_seconds=afsk.MAX_USEFUL_FRAME_SECONDS,
    require_clear_channel=False,
    mode_ladder=modes.default_registry,
)

#: A documented starting point for HF SSB.
#:
#: THESE NUMBERS ARE UNVALIDATED PLACEHOLDERS. Nothing in this repo has been
#: run on HF, no measurement stands behind any figure here, and none of them
#: should be quoted as if one did -- contrast VHF_FM's inactivity_timeout,
#: which has a bench run and a script behind it. They are reasoned from how
#: an HF path differs from the VHF one, and they exist so the shape of the
#: difference is written down and so the engine has a second policy proving
#: it is actually policy-driven rather than accidentally still hardcoded.
#: Every one of them wants replacing with a measured value;
#: scripts/measure_peer_gap.py is the tool for the timeout, exactly as it
#: was on VHF.
#:
#: What is different about HF, and what each guess follows from:
#:
#:   - The path fades rather than simply being weak, and a frame lost to a
#:     fade is usually followed by more. So: more retries, and a longer
#:     inactivity window, giving the path time to come back before the peer
#:     is declared gone.
#:   - Radios are slower round T/R (relays, ALC settling, some with QSK off
#:     entirely) and propagation delay is no longer negligible, so both the
#:     turnaround and the ACK slack grow.
#:   - Longer frames are worth more when turnaround is expensive, and HF
#:     modems conventionally key for several seconds. A bigger budget widens
#:     every CPFSK chunk_size. The counter-pressure is that a fade takes a
#:     whole frame with it, so this is a real trade and 8s is a guess at
#:     where it sits, not a finding.
#:   - Conditions change over minutes, and a step up that fails costs a full
#:     retry cycle. So: step up far more reluctantly, and let the reluctance
#:     grow further after each burn.
#:   - The band is shared and crowded, and a station this modem can barely
#:     hear may be in QSO. Transmitting without listening first is
#:     antisocial and, in most regulatory regimes, not merely rude. Hence
#:     require_clear_channel, which stays unenforced until a busy detector
#:     exists -- so this policy is not yet safe to actually put on air.
HF_SSB = ChannelPolicy(
    name="HF SSB",
    tx_turnaround_delay=0.3,
    inactivity_timeout=300.0,
    max_retries=10,
    ack_timeout_slack=5.0,
    step_down_after_attempts=2,
    step_up_after_clean_streak_initial=4,
    step_up_after_clean_streak_max=32,
    max_useful_frame_seconds=8.0,
    require_clear_channel=True,
    mode_ladder=modes.hf_registry,
)


#: The channels a station can be started on, by the name the CLI takes.
#: See whale/vara_server.py's --channel and scripts/run_acceptance_test.py.
CHANNELS = {"vhf-fm": VHF_FM, "hf-ssb": HF_SSB}


def by_name(name: str) -> ChannelPolicy:
    try:
        return CHANNELS[name]
    except KeyError:
        raise ValueError(
            f"unknown channel {name!r}; have {sorted(CHANNELS)}") from None
