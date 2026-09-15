"""Session-local DATA-mode adaptation for :mod:`whale.link`."""

import logging
import math
import time

from whale import link_protocol as protocol


logger = logging.getLogger("whale.link")


class _AdaptationMixin:
    """Statistics and mode selection mixed into ``whale.link.Link``.

    Reads from the host ``Link``: ``mycall``, ``policy``, ``modes``,
    ``tx_profile``, ``peer_supported_modes``, ``_mode_step_script``,
    ``_channel`` and ``_apply_tx_profile`` (the last two defined on
    ``Link`` itself). Owns and initializes its own delivery-stats
    attributes via ``_init_adaptation``.
    """

    def _init_adaptation(self):
        self._acked_chunks = 0
        self._mode_delivery_stats = {}
        self._consecutive_tx_failures = 0
        self._last_adaptive_mode_change_at = float("-inf")
        self._last_mode_change_direction = 0

    def _maybe_adapt(self, attempts, allow_change=True):
        """Record an ACK and choose the neighboring mode with best evidence."""
        self._acked_chunks += 1
        self._record_mode_attempt(self.tx_profile, True)
        self._consecutive_tx_failures = 0
        if not allow_change:
            return
        scripted = self._mode_step_script.get(self._acked_chunks)
        if scripted is not None:
            logger.warning("[%s] taking scripted mode step %+d after chunk %d -- "
                           "WHALE_MODE_STEP_SCRIPT", self.mycall, scripted,
                           self._acked_chunks)
            self._step_tx_mode(scripted)
            return
        self._choose_statistical_mode()

    def _decayed_mode_stats(self, profile, now=None):
        """Return this mode's exponentially decayed successes and failures."""
        now = time.monotonic() if now is None else now
        stats = self._mode_delivery_stats.get(profile.mode_id)
        if stats is None:
            return 0.0, 0.0
        age = max(0.0, now - stats["updated_at"])
        half_life = self.policy.adaptation_half_life_seconds
        weight = 0.0 if half_life <= 0.0 else 2.0 ** (-age / half_life)
        return stats["successes"] * weight, stats["failures"] * weight

    def _record_mode_attempt(self, profile, succeeded, now=None):
        now = time.monotonic() if now is None else now
        successes, failures = self._decayed_mode_stats(profile, now)
        if succeeded:
            successes += 1.0
        else:
            failures += 1.0
        self._mode_delivery_stats[profile.mode_id] = {
            "successes": successes, "failures": failures, "updated_at": now}

    def _mode_reliability(self, profile, bound="mean", now=None):
        successes, failures = self._decayed_mode_stats(profile, now)
        alpha = self.policy.adaptation_prior_successes + successes
        beta = self.policy.adaptation_prior_failures + failures
        total = alpha + beta
        mean = alpha / total
        if bound == "mean":
            return mean
        sigma = math.sqrt(alpha * beta / (total * total * (total + 1.0)))
        if bound == "optimistic":
            return min(1.0, mean + sigma)
        if bound == "conservative":
            return max(0.0, mean - sigma)
        raise ValueError(f"unknown reliability bound {bound!r}")

    def _mode_expected_goodput(self, profile, bound="mean", now=None):
        """Estimate delivered DATA payload bits/s for one ARQ attempt."""
        probability = self._mode_reliability(profile, bound, now)
        data_airtime = profile.airtime(protocol.AIR_HEADER_LEN + profile.chunk_size)
        ack_airtime = self.modes.control.airtime(protocol.AIR_HEADER_LEN + 1)
        turnaround = 2 * self._channel("tx_turnaround_delay")
        timeout = (data_airtime + ack_airtime + turnaround
                   + self.policy.ack_timeout_slack)
        expected_seconds = (data_airtime
                            + probability * (ack_airtime + turnaround)
                            + (1.0 - probability) * timeout)
        return profile.chunk_size * 8.0 * probability / expected_seconds

    def _choose_statistical_mode(self):
        now = time.monotonic()
        faster = self.modes.step(self.tx_profile, +1)
        if faster is not None and faster.mode_id in self.peer_supported_modes:
            successes, failures = self._decayed_mode_stats(faster, now)
            untested = successes + failures < 0.5
            cooling_down = (now - self._last_adaptive_mode_change_at
                            < self.policy.adaptation_cooldown_seconds)
            current = self._mode_expected_goodput(self.tx_profile, "mean", now)
            candidate = self._mode_expected_goodput(faster, "optimistic", now)
            probe_allowed = not (cooling_down and self._last_mode_change_direction < 0)
            if (untested and probe_allowed) or (
                    not cooling_down and
                    candidate > current * (1.0 + self.policy.adaptation_step_up_margin)):
                self._adaptive_step(+1, "untested probe" if untested
                                    else "expected goodput")
                return

        if (now - self._last_adaptive_mode_change_at
                < self.policy.adaptation_cooldown_seconds):
            return

        slower = self.modes.step(self.tx_profile, -1)
        if slower is not None and slower.mode_id in self.peer_supported_modes:
            current = self._mode_expected_goodput(
                self.tx_profile, "conservative", now)
            candidate = self._mode_expected_goodput(slower, "mean", now)
            if candidate > current:
                self._adaptive_step(-1, "expected goodput")

    def _adaptive_step(self, direction, reason):
        before = self.tx_profile
        self._step_tx_mode(direction)
        if self.tx_profile is not before:
            self._consecutive_tx_failures = 0
            logger.info("[%s] adaptive %s to %s (%s)", self.mycall,
                        "increase" if direction > 0 else "decrease",
                        self.tx_profile.name, reason)

    def _step_tx_mode(self, direction):
        """Change DATA mode without changing the control-plane waveform."""
        candidate = self.modes.step(self.tx_profile, direction)
        if candidate is None:
            return
        if candidate.mode_id not in self.peer_supported_modes:
            return
        self._apply_tx_profile(candidate)
        self._last_adaptive_mode_change_at = time.monotonic()
        self._last_mode_change_direction = direction
        logger.info("[%s] switched tx profile to %s; awaiting DATA_ACK confirmation",
                    self.mycall, self.tx_profile.name)
