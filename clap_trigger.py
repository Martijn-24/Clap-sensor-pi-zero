"""
Clap detector for the bedroompi USB microphone.

Pipeline:
    USB mic -> reader thread -> chunk queue -> ring buffer
            -> RMS gate (hysteresis + refractory)
            -> spectral classifier (FFT centroid + low-band ratio)
            -> sequence state machine (1/2/3 clap grouping)
            -> webhook dispatcher (POST to n8n)

The classifier replaces the third-party `clapDetector` library, which used
post-bandpass peak detection. That approach can't reliably reject loud
broadband impulses (door slams, floor stomps) because impulses by definition
contain energy at all frequencies — even an aggressive bandpass leaks the
onset click. We instead take a 4096-sample (~93 ms) window around each gate
event, FFT it, and classify on steady-state spectral content.

Empirical thresholds (from sessions on this exact mic and room):

    Real claps:        centroid 816-2258 Hz, <500 Hz energy 1.9-29.3%
    Slams/stomps:      centroid 30-475 Hz,   <500 Hz energy 91-100%

A simple AND-rule (`centroid >= 600 AND low_pct <= 60`) gives perfect
separation on the recorded dataset with ~2x safety margin on both axes.

The script supports `--replay <file.wav>` to feed a recorded WAV through the
exact same pipeline as the live mic, with the webhook stubbed out. Use it to
validate classifier changes against `clap-test-1.wav`, `clap-test-2.wav`,
and `clap-test-3.wav` before restarting the live service.

Logging is via plain `print(..., flush=True)` to stdout, captured by journald
through systemd. Every gate event, classification, sequence emit, and
webhook call gets one machine-parseable line.
"""

import argparse
import sys
import time
import wave
from datetime import datetime
from collections import deque
from queue import Queue, Empty, Full
from threading import Thread, Event

import numpy as np
import pyaudio
import requests


# ── Webhook ──────────────────────────────────────────────────────────────────

WEBHOOK_URL       = "http://192.168.50.76:5678/webhook/clap"
WEBHOOK_TIMEOUT_S = 3.0


# ── Audio ────────────────────────────────────────────────────────────────────

SAMPLE_RATE         = 44100
CHUNK_SIZE          = 1024     # ~23 ms per read; small enough for tight onset timing
RING_BUFFER_SIZE    = 65536    # ~1.49 s of float32 audio (pre-allocated)
AUDIO_QUEUE_SIZE    = 16       # ~370 ms of buffering before drop-oldest
MIC_NAME_SUBSTRING  = "USB"


# ── Gate ─────────────────────────────────────────────────────────────────────
#
# RMS computed per chunk in the [-1, 1] float domain. Slam/clap onsets in the
# session data hit chunk RMS of 0.1-0.5; quiet room baseline is ~0.005-0.01.
# Hysteresis avoids re-triggering on a single event's decay envelope.

# Fixed-floor thresholds: the gate can never go below these, even in a
# very quiet room. They preserve the original quiet-room behavior.
GATE_FLOOR_OPEN      = 0.04    # quiet-room minimum open threshold
GATE_FLOOR_CLOSE     = 0.02    # quiet-room minimum close threshold

# Adaptive thresholds: the gate runs a rolling-median estimate of recent
# chunk RMS values. The dynamic open threshold is max(median * MULT_OPEN,
# GATE_FLOOR_OPEN). This means in a quiet room the floor dominates and
# behavior is identical to the old fixed-threshold gate; in a loud room
# (e.g. music playing) the gate self-raises so it only fires on events
# that genuinely stand out above the noise floor.
GATE_ADAPT_HISTORY_S = 3.0     # how many seconds of recent RMS to track for the median
GATE_ADAPT_MULT_OPEN = 4.0     # event must be N× above recent median to fire the gate
GATE_ADAPT_MULT_CLOSE = 2.0    # 2:1 hysteresis preserved, applied to dynamic threshold

GATE_REFRACTORY_S    = 0.04    # ignore further triggers for 40 ms after close
GATE_CLOSE_HOLD      = 2       # need this many consecutive sub-close chunks to confirm close
STARTUP_GRACE_S      = 0.5     # ignore gate triggers for 0.5 s after stream start


# ── Classifier ───────────────────────────────────────────────────────────────
#
# Empirical separation, refined across three calibration datasets:
#
# Dataset A: clap-test-1.wav (12 point-blank claps, mic ~30cm)
#   centroid 735-1295 Hz   low_pct 7-24%   mid_pct 61-88%
#
# Dataset B: clap-test-2.wav + clap-test-3.wav (25 slams/stomps/drawer slams)
#   centroid 8-1149 Hz     low_pct 47-100% mid_pct 0-43%
#
# Dataset C: triple-clap-test-2.wav (45 claps, varying room positions)
#   centroid 1450-2039 Hz  low_pct 0.6-12% mid_pct 33.9-73.7%
#
# Key observation: real-world claps and point-blank claps occupy DIFFERENT
# regions in feature space. Real-world claps (C) have high centroid but
# variable mid_pct. Point-blank claps (A) have moderate centroid but
# consistently high mid_pct. There is no single 1D threshold on either axis
# that admits all claps and rejects all slams.
#
# Decision rule (2-clause disjunction):
#
#   is_clap = (centroid >= 1300 Hz)
#             OR (centroid >= 600 Hz AND mid_pct >= 60%)
#
#   First clause catches dataset C (real-world): centroid min 1450 Hz.
#   Second clause catches dataset A (point-blank): mid_pct min 61%, centroid
#       min 735 Hz.
#   Both clauses reject dataset B (slams): max centroid 1149 Hz fails the
#       first clause, max mid_pct 43% fails the second's mid_pct check.
#
# low_pct backstop kept as a sanity guard against pathological events.

CLASSIFIER_WINDOW_SIZE          = 4096    # ~93 ms @ 44.1 kHz; power of two for fast rfft
CLASSIFIER_CENTROID_HIGH_MIN_HZ = 1300.0  # first clause: high-centroid claps (real-world)
CLASSIFIER_CENTROID_LOW_MIN_HZ  = 600.0   # second clause: lower bound for mid-heavy claps
CLASSIFIER_MID_PCT_HIGH_MIN     = 60.0    # second clause: mid-band ratio for point-blank claps
CLASSIFIER_LOW_PCT_MAX          = 60.0    # backstop against very-low-frequency events
CLASSIFIER_LOW_BAND_HZ          = 500.0
CLASSIFIER_MID_BAND_HI_HZ       = 4000.0  # captures both point-blank and snappy claps


# ── Sequence detection (1/2/3 clap counting) ────────────────────────────────

CLAP_INTERVAL_S    = 0.7    # max gap between consecutive claps in one sequence.
                             # Bumped from 0.5 after observing that natural 3-clap
                             # sequences can have 500-600 ms gaps between adjacent claps,
                             # causing the third clap to start a new singleton sequence
                             # instead of joining the existing one.
SEQUENCE_TIMEOUT_S = 0.8    # finalize a sequence after this much silence after the last
                             # clap. Must be > CLAP_INTERVAL_S so the interval check
                             # gets a chance to fire before the timeout does.
VALID_COUNTS       = (1, 2, 3)


# ── Cooldown ─────────────────────────────────────────────────────────────────

POST_FIRE_COOLDOWN_S = 1.0   # gate disabled for 1 s after a successful webhook


# ── Logging ──────────────────────────────────────────────────────────────────
#
# QUIET   - only SEQUENCE_EMIT, WEBHOOK_*, errors
# NORMAL  - + GATE_OPEN, CLASSIFY_ACCEPT, CLASSIFY_REJECT, DROPPED
# VERBOSE - + GATE_CLOSE, OVERRUN, GRACE_SKIP, COOLDOWN_SKIP

LOG_LEVEL = "NORMAL"

_LOG_KIND_LEVELS = {
    "STARTUP":         "QUIET",
    "SHUTDOWN":        "QUIET",
    "SEQUENCE_EMIT":   "QUIET",
    "WEBHOOK_OK":      "QUIET",
    "WEBHOOK_FAIL":    "QUIET",
    "WEBHOOK_DRYRUN":  "QUIET",
    "DROPPED":         "QUIET",
    "ERROR":           "QUIET",
    "GATE_OPEN":       "NORMAL",
    "CLASSIFY_ACCEPT": "NORMAL",
    "CLASSIFY_REJECT": "NORMAL",
    "REPLAY_SUMMARY":  "NORMAL",
    "GATE_CLOSE":      "VERBOSE",
    "OVERRUN":         "VERBOSE",
    "GRACE_SKIP":      "VERBOSE",
    "COOLDOWN_SKIP":   "VERBOSE",
}
_LEVEL_RANK = {"QUIET": 0, "NORMAL": 1, "VERBOSE": 2}

_start_monotonic = time.monotonic()
_clock_now = time.monotonic   # replaced in --replay mode by a fake clock


def ts():
    """Wallclock with millisecond precision for log line prefixes."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log_event(kind, **fields):
    """Single-line structured log. Format: '[wallclock] KIND key=value ...'"""
    required_level = _LOG_KIND_LEVELS.get(kind, "NORMAL")
    if _LEVEL_RANK[required_level] > _LEVEL_RANK[LOG_LEVEL]:
        return
    parts = [f"[{ts()}] {kind:<15}"]
    parts.append(f"t=+{_clock_now() - _start_monotonic:8.3f}")
    for key, value in fields.items():
        if isinstance(value, float):
            parts.append(f"{key}={value:.3f}")
        else:
            parts.append(f"{key}={value}")
    print(" ".join(parts), flush=True)


# ── Ring buffer ──────────────────────────────────────────────────────────────

class RingBuffer:
    """Pre-allocated float32 ring of audio samples.

    Writes wrap modulo `size`. The absolute sample index of every write is
    tracked monotonically so the gate can pass an absolute index to the
    classifier and the classifier can copy out the right window.
    """

    def __init__(self, size):
        self.size = size
        self.buf = np.zeros(size, dtype=np.float32)
        self.write_index = 0   # absolute, monotonically increasing

    def write(self, samples):
        """Append a 1-D float32 chunk. May wrap around the end."""
        n = len(samples)
        if n > self.size:
            samples = samples[-self.size:]
            n = self.size
        start = self.write_index % self.size
        end = start + n
        if end <= self.size:
            self.buf[start:end] = samples
        else:
            first = self.size - start
            self.buf[start:] = samples[:first]
            self.buf[:end - self.size] = samples[first:]
        self.write_index += n

    def read_window(self, abs_start_index, length):
        """Copy `length` samples starting at the given absolute index.

        Returns None if the requested window is outside what the buffer
        currently holds (either too far in the past or not yet written).
        """
        end_index = abs_start_index + length
        if end_index > self.write_index:
            return None
        oldest_available = self.write_index - self.size
        if abs_start_index < oldest_available:
            return None
        out = np.empty(length, dtype=np.float32)
        start = abs_start_index % self.size
        end = start + length
        if end <= self.size:
            out[:] = self.buf[start:end]
        else:
            first = self.size - start
            out[:first] = self.buf[start:]
            out[first:] = self.buf[:end - self.size]
        return out

    def newest_index(self):
        """Absolute index of the next sample to be written."""
        return self.write_index


# ── Gate ─────────────────────────────────────────────────────────────────────

class Gate:
    """Adaptive RMS envelope gate with hysteresis and a refractory period.

    Maintains a rolling-median estimate of recent chunk RMS values. The
    dynamic open/close thresholds are derived from the median scaled by
    GATE_ADAPT_MULT_*, with a fixed floor that preserves quiet-room
    behavior. This lets the gate ignore steady background sound (e.g.
    music) and only fire on events that genuinely stand out above the
    current noise floor.

    Call `process_chunk(rms, abs_start_index, now)` once per audio chunk.
    Returns the absolute sample index of the chunk that opened the gate, or
    None if no event happened.
    """

    QUIET = "QUIET"
    OPEN = "OPEN"
    REFRACTORY = "REFRACTORY"

    def __init__(self):
        self.state = self.QUIET
        self.close_hold_count = 0
        self.refractory_until = 0.0
        self.startup_until = _clock_now() + STARTUP_GRACE_S
        self.cooldown_until = 0.0

        # Rolling RMS history for adaptive thresholding. Sized to hold
        # ~GATE_ADAPT_HISTORY_S worth of chunks.
        history_len = max(8, int(GATE_ADAPT_HISTORY_S * SAMPLE_RATE / CHUNK_SIZE))
        self.rms_history = deque(maxlen=history_len)

    def set_cooldown(self, until_monotonic):
        self.cooldown_until = until_monotonic

    def _dynamic_thresholds(self):
        """Compute current open/close thresholds from the rolling RMS median.
        Returns (open_thr, close_thr, median_used).
        """
        if len(self.rms_history) < 4:
            return GATE_FLOOR_OPEN, GATE_FLOOR_CLOSE, 0.0
        # Sorted-list median is fine for our small history (~130 entries)
        sorted_h = sorted(self.rms_history)
        median = sorted_h[len(sorted_h) // 2]
        open_thr = max(median * GATE_ADAPT_MULT_OPEN, GATE_FLOOR_OPEN)
        close_thr = max(median * GATE_ADAPT_MULT_CLOSE, GATE_FLOOR_CLOSE)
        return open_thr, close_thr, median

    def process_chunk(self, rms, abs_start_index, now):
        # Always update the rolling history, regardless of state. Even
        # during refractory we want to know what the room sounds like.
        self.rms_history.append(rms)

        # Startup grace
        if now < self.startup_until:
            return None

        # Post-fire cooldown disables the gate entirely
        if now < self.cooldown_until:
            return None

        open_thr, close_thr, _median = self._dynamic_thresholds()

        if self.state == self.QUIET:
            if rms >= open_thr:
                self.state = self.OPEN
                self.close_hold_count = 0
                log_event(
                    "GATE_OPEN",
                    rms=rms,
                    sample_idx=abs_start_index,
                    thr_open=open_thr,
                )
                return abs_start_index
            return None

        if self.state == self.OPEN:
            if rms < close_thr:
                self.close_hold_count += 1
                if self.close_hold_count >= GATE_CLOSE_HOLD:
                    self.state = self.REFRACTORY
                    self.refractory_until = now + GATE_REFRACTORY_S
                    log_event("GATE_CLOSE", rms=rms, thr_close=close_thr)
            else:
                self.close_hold_count = 0
            return None

        if self.state == self.REFRACTORY:
            if now >= self.refractory_until:
                self.state = self.QUIET
            return None

        return None


# ── Classifier ───────────────────────────────────────────────────────────────

def classify_window(window, sample_rate):
    """Spectral classifier. Returns (is_clap: bool, features: dict).

    Features dict always includes: centroid, low_pct, mid_pct, peak_hz, total.
    Empty/zero windows return is_clap=False with total=0.
    """
    if window is None or len(window) == 0:
        return False, {"reason": "empty_window", "total": 0.0}

    seg = window.astype(np.float64) * np.hanning(len(window))
    spec = np.abs(np.fft.rfft(seg))
    freqs = np.fft.rfftfreq(len(seg), d=1.0 / sample_rate)
    energy = spec ** 2
    total = float(energy.sum())
    if total <= 0.0:
        return False, {"reason": "zero_energy", "total": 0.0}

    centroid = float((freqs * energy).sum() / total)
    low_mask = freqs < CLASSIFIER_LOW_BAND_HZ
    mid_mask = (freqs >= CLASSIFIER_LOW_BAND_HZ) & (freqs < CLASSIFIER_MID_BAND_HI_HZ)
    low_pct = 100.0 * float(energy[low_mask].sum()) / total
    mid_pct = 100.0 * float(energy[mid_mask].sum()) / total
    peak_hz = float(freqs[int(np.argmax(spec))])

    # 2-clause clap rule:
    #   clause A: centroid >= 1300 Hz (real-world claps from any position)
    #   clause B: centroid >= 600 AND mid_pct >= 60 (point-blank claps with
    #             moderate centroid but strong mid-band)
    # Plus a low_pct backstop against very-low-frequency events.
    clause_a = centroid >= CLASSIFIER_CENTROID_HIGH_MIN_HZ
    clause_b = (
        centroid >= CLASSIFIER_CENTROID_LOW_MIN_HZ
        and mid_pct >= CLASSIFIER_MID_PCT_HIGH_MIN
    )
    low_ok = low_pct <= CLASSIFIER_LOW_PCT_MAX
    is_clap = (clause_a or clause_b) and low_ok

    if is_clap:
        reason = "ok"
    else:
        if not low_ok:
            reason = "low_band_strong"
        elif centroid < CLASSIFIER_CENTROID_LOW_MIN_HZ:
            reason = "centroid_too_low"
        elif centroid < CLASSIFIER_CENTROID_HIGH_MIN_HZ:
            # In the "moderate centroid" zone, must have strong mid-band
            reason = f"moderate_centroid_{centroid:.0f}_needs_mid_pct_60_got_{mid_pct:.0f}"
        else:
            reason = "unknown"

    return is_clap, {
        "centroid": centroid,
        "low_pct": low_pct,
        "mid_pct": mid_pct,
        "peak_hz": peak_hz,
        "reason": reason,
    }


# ── Sequence tracker ─────────────────────────────────────────────────────────

class SequenceTracker:
    """Groups classified-clap events into 1/2/3-clap sequences.

    Call `add_clap(now)` for each accepted clap and `tick(now)` once per main
    loop iteration to expire pending sequences. Both may return a finalized
    count (or None).
    """

    def __init__(self):
        self.count = 0
        self.last_clap_t = 0.0
        self.collecting = False

    def add_clap(self, now):
        emitted = None
        if self.collecting:
            if now - self.last_clap_t <= CLAP_INTERVAL_S:
                self.count += 1
                self.last_clap_t = now
                return None
            # Gap too large -> finalize the old sequence and start a new one.
            emitted = self.count
            self.count = 1
            self.last_clap_t = now
            return emitted
        self.collecting = True
        self.count = 1
        self.last_clap_t = now
        return None

    def tick(self, now):
        if not self.collecting:
            return None
        if now - self.last_clap_t > SEQUENCE_TIMEOUT_S:
            emitted = self.count
            self.collecting = False
            self.count = 0
            return emitted
        return None


# ── Webhook dispatcher ───────────────────────────────────────────────────────

class WebhookDispatcher:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.session = requests.Session() if not dry_run else None

    def fire(self, count):
        if self.dry_run:
            log_event("WEBHOOK_DRYRUN", count=count, url=WEBHOOK_URL)
            return True
        start = time.monotonic()
        try:
            resp = self.session.post(
                WEBHOOK_URL, json={"claps": count}, timeout=WEBHOOK_TIMEOUT_S
            )
            dur_ms = int((time.monotonic() - start) * 1000)
            log_event(
                "WEBHOOK_OK", count=count, status=resp.status_code, dur_ms=dur_ms
            )
            return True
        except requests.exceptions.RequestException as e:
            dur_ms = int((time.monotonic() - start) * 1000)
            log_event(
                "WEBHOOK_FAIL", count=count, error=str(e)[:80], dur_ms=dur_ms
            )
            return False


# ── Audio reader ─────────────────────────────────────────────────────────────

class AudioReaderThread(Thread):
    """Background thread that reads from PyAudio and pushes chunks into a queue.

    On overrun (queue full) it drops the oldest chunk and increments a counter
    that's logged once per second by the main loop.
    """

    def __init__(self, stream, chunk_size, queue, stop_event):
        super().__init__(daemon=True)
        self.stream = stream
        self.chunk_size = chunk_size
        self.queue = queue
        self.stop_event = stop_event
        self.overruns = 0
        self.read_errors = 0

    def run(self):
        while not self.stop_event.is_set():
            try:
                raw = self.stream.read(self.chunk_size, exception_on_overflow=False)
            except Exception as e:
                self.read_errors += 1
                log_event("ERROR", source="reader", error=str(e)[:80])
                time.sleep(0.1)
                continue
            chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            try:
                self.queue.put_nowait(chunk)
            except Full:
                # Drop the oldest chunk to make room.
                try:
                    self.queue.get_nowait()
                except Empty:
                    pass
                try:
                    self.queue.put_nowait(chunk)
                except Full:
                    pass
                self.overruns += 1


def find_usb_mic(p):
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0 and MIC_NAME_SUBSTRING in info["name"]:
            log_event(
                "STARTUP",
                event="usb_mic_found",
                index=i,
                name=info["name"][:40],
            )
            return i
    return None


def open_audio_stream():
    """Open a blocking PyAudio input stream on the USB mic. Retries on failure."""
    for attempt in range(10):
        try:
            p = pyaudio.PyAudio()
            device_index = find_usb_mic(p)
            if device_index is None:
                p.terminate()
                raise OSError("No USB microphone found")
            stream = p.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
                input_device_index=device_index,
            )
            log_event("STARTUP", event="audio_open_ok", attempt=attempt + 1)
            return p, stream
        except Exception as e:
            log_event(
                "ERROR", source="audio_init", attempt=attempt + 1, error=str(e)[:80]
            )
            time.sleep(5)
    log_event("ERROR", source="audio_init", error="exhausted_retries")
    sys.exit(1)


# ── Main pipeline (shared between live and replay modes) ────────────────────

class Pipeline:
    # Window layout: when the gate fires on chunk N (whose samples occupy
    # absolute indices [gate_idx, gate_idx + CHUNK_SIZE)), we want the
    # classifier window to start ~25% before the trigger so the actual onset
    # sits inside the window after Hann tapering.
    WINDOW_PRE_SAMPLES = 1024
    # Maximum chunks we will defer a pending classification before giving up.
    # Need ceil((CLASSIFIER_WINDOW_SIZE - WINDOW_PRE_SAMPLES) / CHUNK_SIZE)
    # chunks of forward audio to be available; with window=4096, pre=1024,
    # chunk=1024 that's 3 chunks after the trigger, so 4 deferrals max gives
    # one chunk of slack.
    MAX_PENDING_DEFERRALS = 4

    def __init__(self, dispatcher):
        self.ring = RingBuffer(RING_BUFFER_SIZE)
        self.gate = Gate()
        self.tracker = SequenceTracker()
        self.dispatcher = dispatcher
        # Pending classifications waiting for enough forward audio:
        # list of dicts {gate_idx, t_emitted, deferrals}
        self.pending_classifications = []
        # Stats for replay summary
        self.gate_triggers = 0
        self.classify_accepts = 0
        self.classify_rejects = 0
        self.classify_reject_reasons = {}
        self.sequences_emitted = []   # list of (t, count)

    def process_chunk(self, chunk, now):
        chunk_start_index = self.ring.newest_index()
        self.ring.write(chunk)
        rms = float(np.sqrt(np.mean(chunk * chunk)))

        # Try to fulfill any pending classifications now that another chunk
        # has been written into the ring. Do this BEFORE the new gate event
        # so old events get classified in time-order.
        self._process_pending_classifications(now)

        # Tick the sequence tracker (may emit on timeout)
        emitted = self.tracker.tick(now)
        if emitted is not None:
            self._emit_sequence(emitted, now)

        # Run the gate
        gate_idx = self.gate.process_chunk(rms, chunk_start_index, now)
        if gate_idx is None:
            return
        self.gate_triggers += 1

        # Try to classify immediately. If the forward window isn't available
        # yet (it usually isn't because the gate just fired on the freshest
        # chunk), defer until enough subsequent chunks have been written.
        if not self._try_classify(gate_idx, now):
            self.pending_classifications.append(
                {"gate_idx": gate_idx, "t_emitted": now, "deferrals": 0}
            )

    def _process_pending_classifications(self, now):
        if not self.pending_classifications:
            return
        still_pending = []
        for entry in self.pending_classifications:
            if self._try_classify(entry["gate_idx"], entry["t_emitted"]):
                continue
            entry["deferrals"] += 1
            if entry["deferrals"] >= self.MAX_PENDING_DEFERRALS:
                # Give up on this one — should never happen in practice.
                self.classify_rejects += 1
                self.classify_reject_reasons["pending_expired"] = (
                    self.classify_reject_reasons.get("pending_expired", 0) + 1
                )
                log_event(
                    "CLASSIFY_REJECT",
                    reason="pending_expired",
                    gate_idx=entry["gate_idx"],
                )
                continue
            still_pending.append(entry)
        self.pending_classifications = still_pending

    def _try_classify(self, gate_idx, now_for_sequence):
        """Attempt classification at gate_idx. Returns True if completed
        (accepted, rejected, or unrecoverably failed); False if the window
        isn't available yet and the caller should defer.
        """
        window_start = max(0, gate_idx - self.WINDOW_PRE_SAMPLES)
        window = self.ring.read_window(window_start, CLASSIFIER_WINDOW_SIZE)
        if window is None:
            return False

        is_clap, features = classify_window(window, SAMPLE_RATE)
        if is_clap:
            self.classify_accepts += 1
            log_event(
                "CLASSIFY_ACCEPT",
                centroid=features["centroid"],
                low_pct=features["low_pct"],
                mid_pct=features["mid_pct"],
                peak_hz=features["peak_hz"],
            )
            emitted = self.tracker.add_clap(now_for_sequence)
            if emitted is not None:
                self._emit_sequence(emitted, now_for_sequence)
        else:
            self.classify_rejects += 1
            reason = features.get("reason", "unknown")
            self.classify_reject_reasons[reason] = (
                self.classify_reject_reasons.get(reason, 0) + 1
            )
            log_event(
                "CLASSIFY_REJECT",
                centroid=features.get("centroid", 0.0),
                low_pct=features.get("low_pct", 0.0),
                mid_pct=features.get("mid_pct", 0.0),
                peak_hz=features.get("peak_hz", 0.0),
                reason=reason,
            )
        return True

    def _emit_sequence(self, count, now):
        log_event("SEQUENCE_EMIT", count=count)
        self.sequences_emitted.append((now - _start_monotonic, count))
        if count in VALID_COUNTS:
            ok = self.dispatcher.fire(count)
            if ok:
                self.gate.set_cooldown(now + POST_FIRE_COOLDOWN_S)
        else:
            log_event("DROPPED", count=count, reason="out_of_range")

    def flush_pending(self, now):
        """Drain pending classifications and force the sequence tracker to
        expire any in-progress sequence. Used at end of replay so trailing
        claps don't get lost."""
        self._process_pending_classifications(now)
        # Anything still pending after the drain is unrecoverable; mark as
        # expired so the summary stats reflect it.
        for entry in self.pending_classifications:
            self.classify_rejects += 1
            self.classify_reject_reasons["pending_expired_at_eof"] = (
                self.classify_reject_reasons.get("pending_expired_at_eof", 0) + 1
            )
            log_event(
                "CLASSIFY_REJECT",
                reason="pending_expired_at_eof",
                gate_idx=entry["gate_idx"],
            )
        self.pending_classifications = []
        emitted = self.tracker.tick(now + SEQUENCE_TIMEOUT_S + 1.0)
        if emitted is not None:
            self._emit_sequence(emitted, now)


# ── Live mode ────────────────────────────────────────────────────────────────

def run_live():
    log_event("STARTUP", mode="live", sample_rate=SAMPLE_RATE, chunk=CHUNK_SIZE)
    p, stream = open_audio_stream()
    chunk_queue = Queue(maxsize=AUDIO_QUEUE_SIZE)
    stop_event = Event()
    reader = AudioReaderThread(stream, CHUNK_SIZE, chunk_queue, stop_event)
    reader.start()

    pipeline = Pipeline(WebhookDispatcher(dry_run=False))
    log_event("STARTUP", event="listening")

    last_overrun_log_t = time.monotonic()
    last_overruns_seen = 0

    try:
        while True:
            try:
                chunk = chunk_queue.get(timeout=0.5)
            except Empty:
                # Still tick the sequence tracker so trailing claps expire.
                now = time.monotonic()
                emitted = pipeline.tracker.tick(now)
                if emitted is not None:
                    pipeline._emit_sequence(emitted, now)
                continue

            now = time.monotonic()
            pipeline.process_chunk(chunk, now)

            # Log overrun counter once per second when nonzero.
            if now - last_overrun_log_t >= 1.0:
                if reader.overruns != last_overruns_seen:
                    log_event(
                        "OVERRUN",
                        total=reader.overruns,
                        delta=reader.overruns - last_overruns_seen,
                    )
                    last_overruns_seen = reader.overruns
                last_overrun_log_t = now

            if not reader.is_alive():
                log_event("ERROR", source="reader", error="thread_died")
                break
    except KeyboardInterrupt:
        log_event("SHUTDOWN", reason="keyboard_interrupt")
    finally:
        stop_event.set()
        try:
            stream.stop_stream()
            stream.close()
            p.terminate()
        except Exception:
            pass


# ── Replay mode ──────────────────────────────────────────────────────────────

def read_wav_mono(path):
    """Read a mono int16 WAV. Returns (samples_float32, sample_rate)."""
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    if sample_width != 2:
        raise ValueError(f"expected 16-bit PCM, got {sample_width * 8}-bit")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    samples /= 32768.0
    return samples, sample_rate


def run_replay(path):
    """Replay a WAV through the pipeline. Webhook is stubbed (DRYRUN).

    Uses a fake monotonic clock that advances by CHUNK_SIZE/SAMPLE_RATE per
    chunk so sequence timing is deterministic regardless of host CPU speed.
    """
    global _clock_now, _start_monotonic
    samples, file_rate = read_wav_mono(path)
    if file_rate != SAMPLE_RATE:
        raise ValueError(
            f"WAV sample rate {file_rate} != expected {SAMPLE_RATE}"
        )

    # Install a fake clock so sequence timing is deterministic.
    fake_t = [0.0]

    def fake_clock():
        return fake_t[0]

    _clock_now = fake_clock
    _start_monotonic = 0.0

    log_event("STARTUP", mode="replay", file=path, samples=len(samples))
    pipeline = Pipeline(WebhookDispatcher(dry_run=True))

    chunk_dt = CHUNK_SIZE / SAMPLE_RATE
    n_chunks = len(samples) // CHUNK_SIZE
    for i in range(n_chunks):
        chunk = samples[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
        fake_t[0] = (i + 1) * chunk_dt
        pipeline.process_chunk(chunk, fake_t[0])

    # Drain any in-progress sequence.
    pipeline.flush_pending(fake_t[0])

    # Summary
    log_event(
        "REPLAY_SUMMARY",
        gate_triggers=pipeline.gate_triggers,
        classify_accepts=pipeline.classify_accepts,
        classify_rejects=pipeline.classify_rejects,
        sequences=len(pipeline.sequences_emitted),
    )
    if pipeline.classify_reject_reasons:
        for reason, count in sorted(
            pipeline.classify_reject_reasons.items(), key=lambda kv: -kv[1]
        ):
            log_event("REPLAY_SUMMARY", reject_reason=reason, count=count)
    for t, count in pipeline.sequences_emitted:
        log_event("REPLAY_SUMMARY", emitted_t=t, count=count)


# ── Entrypoint ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Clap detector for bedroompi")
    parser.add_argument(
        "--replay",
        metavar="WAV",
        help="Replay a recorded WAV through the pipeline (no webhook).",
    )
    args = parser.parse_args()

    if args.replay:
        run_replay(args.replay)
    else:
        run_live()


if __name__ == "__main__":
    main()
