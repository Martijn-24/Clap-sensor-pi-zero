# Clap sensor (Pi Zero)

A Raspberry Pi Zero with a USB microphone that listens for hand claps and
fires a webhook to n8n when it hears 1, 2, or 3 claps in sequence. n8n then
toggles a Shelly Plug S Gen3 (a lamp) and a WiiM Pro Plus (audio playback).

## What's in this repo

| File | What it is |
|---|---|
| [`clap_trigger.py`](clap_trigger.py) | The detector. Reads the USB mic, classifies events, dispatches the webhook. |
| [`systemd/clap-trigger.service`](systemd/clap-trigger.service) | The systemd unit that runs the script as user `martijn` with auto-restart. |
| `.gitignore` | Excludes secrets and editor state. |

The script lives on the Pi at `/home/martijn/clap_trigger.py` and the unit at
`/etc/systemd/system/clap-trigger.service`. The systemd unit invokes the
script with `python3 -u`.

## How it works

The detection pipeline is:

```
USB mic
   ↓ (PyAudio @ 44.1 kHz mono int16, 1024-sample chunks)
Reader thread
   ↓ (bounded queue, drop-oldest on overflow)
Ring buffer (~1.5 s of float32 audio)
   ↓
RMS gate (hysteresis + refractory)            ← something loud happened?
   ↓
Pending classification queue                  ← wait until enough forward audio is buffered
   ↓
Spectral classifier (FFT)                     ← was that thing actually a clap?
   ↓
Sequence state machine                        ← group consecutive claps into a count
   ↓
Webhook dispatcher                            ← POST {"claps": N} to n8n
```

Each stage is one class or function in `clap_trigger.py` and they're stacked
in that order in the file.

### Why this design

The first version of this script used the third-party `clapDetector` PyPI
library, which detects claps by bandpass-filtering the audio and then peak-
detecting in the filtered signal. That approach has a fundamental limitation
we hit hard in testing: **broadband impulses (door slams, floor stomps,
drawer slams) can't be reliably rejected by frequency filtering alone**,
because impulses by definition contain energy at all frequencies. Even at
LOWCUT=1500 Hz, slam onsets still produced post-filter peaks well above any
useful threshold.

The fix is to switch from "filter then peak-detect" to "windowed spectral
classification". When the gate fires on a loud event, we grab a 4096-sample
(~93 ms) window of audio around it, FFT it, and look at the steady-state
spectral content. Real claps and slams have measurably different spectra
when measured this way — but the separation depends on **distance and room
acoustics**, so we use a 2-clause rule that handles two distinct clap regimes:

| Feature | Point-blank claps (~30cm) | Real-world claps (varying distance) | Door slams / stomps |
|---|---|---|---|
| Spectral centroid | 735–1295 Hz | **1450–2039 Hz** | 8–1149 Hz |
| Energy below 500 Hz | 7–24% | 0.6–12% | 47–100% |
| Energy in 500–4000 Hz | **61–88%** | 33.9–73.7% | 0–43% |

(Numbers from offline analysis of four reference recordings on the actual
mic and in the actual room — see "Validation history" below.)

The classifier accepts an event as a clap if **EITHER** clause holds:

- **Clause A** (real-world claps): `centroid >= 1300 Hz`
- **Clause B** (point-blank claps): `centroid >= 600 Hz AND mid_pct >= 60%`

Plus a `low_pct <= 60%` backstop to reject very-low-frequency events that
slipped through both clauses.

Why 2 clauses? Real-world claps from across a room push their spectral
energy higher (centroid > 1450 Hz) but spread it across more bands
(mid_pct varies 34-74%). Close-mic claps have moderate centroid (735-1295
Hz) but a tightly concentrated mid-band (mid_pct 61-88%). No single 1D
threshold separates both clap regimes from slams. The disjunctive rule
handles both.

Slams fail both clauses: their max centroid (1149 Hz) is below clause A's
1300 Hz floor, and their max mid_pct (43%) is below clause B's 60% floor.
There is comfortable separation on both axes.

### Sequence detection (1, 2, 3 claps)

Once an individual clap is classified, the sequence tracker groups
consecutive claps into a count:

- New clap → start collecting (`count = 1`).
- Another clap within 500 ms → `count += 1`.
- 600 ms of silence after the last clap → emit the count.
- Counts of 1, 2, or 3 fire the webhook. Anything else is dropped with a
  log line.

After a successful webhook, the gate is disabled for 1 second to avoid
self-retriggering.

## Configuration

All tunable values live at the top of `clap_trigger.py` in clearly-labeled
constant blocks. The most important ones:

| Constant | What it controls |
|---|---|
| `WEBHOOK_URL` | Where the POST goes |
| `GATE_OPEN_THRESHOLD` | RMS above which the gate fires (currently 0.04) |
| `GATE_CLOSE_THRESHOLD` | RMS below which the gate re-arms (currently 0.02) |
| `CLASSIFIER_CENTROID_HIGH_MIN_HZ` | Clause A centroid floor (currently 1300) |
| `CLASSIFIER_CENTROID_LOW_MIN_HZ` | Clause B centroid floor (currently 600) |
| `CLASSIFIER_MID_PCT_HIGH_MIN` | Clause B mid-band ratio floor (currently 60%) |
| `CLASSIFIER_LOW_PCT_MAX` | Backstop max below-500 Hz energy ratio (currently 60%) |
| `CLAP_INTERVAL_S` | Max gap between consecutive claps in one sequence (0.7s) |
| `SEQUENCE_TIMEOUT_S` | Silence after last clap before sequence finalizes (0.8s) |
| `POST_FIRE_COOLDOWN_S` | Gate-disabled window after a successful webhook |
| `LOG_LEVEL` | `QUIET` / `NORMAL` / `VERBOSE` |

If you tune any of these, **always re-run the offline replay** (see below)
against the test WAVs before restarting the live service.

### Calibration is room-specific

The classifier thresholds were tuned to a specific room with specific
acoustics, mic placement, and typical clap distances. **Moving the sensor
to a different room may break detection**, because:

- Smaller rooms have stronger low-frequency standing waves that can push
  `low_pct` upward
- Soft furnishings (bedding, curtains, carpet) absorb high frequencies and
  lower `centroid`
- Different background noise floors can cause the gate to over- or
  under-fire
- Different typical mic-to-clapper distances shift the entire feature
  distribution

If you move the sensor and detection starts misbehaving, the recovery
playbook is:

1. Record a fresh ~120-second WAV of you doing 10-15 triple-claps from
   varying positions in the new room (use the `arecord` invocation in the
   `--replay` validation section below for the right device/format).
2. Run `analyze_claps.py` over it to see the new spectral envelope.
3. Adjust the classifier thresholds to admit the new envelope while still
   rejecting the existing slam reference WAVs.
4. Validate the new thresholds against ALL existing reference WAVs (the
   old room's claps, the slams, and the new room's claps) to confirm no
   regressions.
5. Atomic-swap deploy.

Keep all reference WAVs forever — they're the regression suite.

## Offline replay mode (validation gate)

The script supports `--replay <file.wav>` to feed a recorded WAV through
the **same pipeline as live mode**, with the webhook stubbed to a dry-run
log line. This is the cheap, safe way to validate classifier changes:

```bash
python3 clap_trigger.py --replay /home/martijn/clap-test-1.wav
python3 clap_trigger.py --replay /home/martijn/clap-test-2.wav
python3 clap_trigger.py --replay /home/martijn/clap-test-3.wav
```

There are three reference recordings on the Pi at `/home/martijn/clap-test-N.wav`:

- **clap-test-1.wav** — only real claps, from various positions/intensities. Should produce **only accepts**, no rejects.
- **clap-test-2.wav** — door slams, floor stomps, drawer slams, etc. Should produce **only rejects**, no accepts.
- **clap-test-3.wav** — more slams + stomps, cross-validation. Should also produce **only rejects**.

If a classifier change breaks any of these expectations, do not deploy until
the change is fixed or the test WAVs are updated to reflect a new ground
truth.

The replay mode uses a fake monotonic clock that advances by chunk
duration, so sequence timing is deterministic regardless of host CPU speed.
You can run replay mode on any machine that has Python + numpy + pyaudio
(pyaudio is needed only for the live mode imports; the replay path doesn't
actually open a stream).

## Logging

Every gate event, classification (accept and reject), sequence emit, and
webhook call produces one machine-parseable log line. Format:

```
[HH:MM:SS.mmm] KIND            t=+seconds   key=value key=value ...
```

Examples (from a real test session):

```
[21:58:27.971] GATE_OPEN       t=+82.074  rms=0.191 sample_idx=2000896
[21:58:28.116] CLASSIFY_ACCEPT t=+82.220  centroid=2213.440 low_pct=2.937 mid_pct=95.638 peak_hz=1431.958
[21:58:28.610] SEQUENCE_EMIT   t=+82.714  count=1
[21:58:28.946] WEBHOOK_OK      t=+83.049  count=1 status=200 dur_ms=334

[21:58:33.300] GATE_OPEN       t=+87.404  rms=0.147 sample_idx=2135040
[21:58:33.351] CLASSIFY_REJECT t=+87.454  centroid=480.229 low_pct=69.849 mid_pct=29.785 peak_hz=64.600 reason=centroid_low+low_band_strong+mid_band_weak
```

The reject lines always include all three feature values **and** a `reason`
string listing which checks failed. So when the detector misbehaves later,
debugging is just `journalctl -u clap-trigger.service | grep CLASSIFY_REJECT`
followed by reading the centroid/band values to see exactly why.

`LOG_LEVEL = "NORMAL"` is the default and includes everything you typically
want. `VERBOSE` adds GATE_CLOSE, OVERRUN counters, and cooldown skips for
deeper debugging. `QUIET` cuts back to just sequences and webhook results.

All logging is via `print(..., flush=True)` so it goes to journald via
systemd's stdout capture. View with:

```bash
sudo journalctl -u clap-trigger.service -f
```

## Hardware & environment

- **Pi**: Raspberry Pi Zero W running Debian bookworm aarch64 (kernel 6.12).
- **Mic**: Generic USB mic identified as `USB PnP Sound Device` at ALSA
  card 1, device 0. The script auto-discovers it by substring match on
  `"USB"` in the device name.
- **Audio**: 44.1 kHz mono int16, 1024-sample chunks (~23 ms each).

## Dependencies

- `numpy` — FFT and array math (already on Pi).
- `pyaudio` — USB mic capture (already on Pi).
- `requests` — webhook POST (already on Pi).
- `wave`, `argparse`, `threading`, `queue`, `datetime` — stdlib.

`scipy` and `clapDetector` are no longer used at runtime. They can stay
installed; the script no longer imports them.

## Deployment

Atomic-swap pattern via the ssh helper at
`../.tools/ssh-helper/run.js` in the parent project:

```bash
# 1. Edit clap_trigger.py locally.
# 2. Upload as .new file (base64 over ssh, stdin pipe):
node -e "process.stdout.write(require('fs').readFileSync('clap_trigger.py').toString('base64'));" \
  | node ../.tools/ssh-helper/run.js 192.168.50.192 martijn '<password>' \
    'base64 -d > ~/clap_trigger.py.new && python3 -m py_compile ~/clap_trigger.py.new'

# 3. Run offline replay validation against the test WAVs (see above).
#    If any test fails, iterate before going live.

# 4. Swap atomically:
node ../.tools/ssh-helper/run.js 192.168.50.192 martijn '<password>' \
  'sudo systemctl stop clap-trigger.service \
   && cp ~/clap_trigger.py ~/clap_trigger.py.bak.$(date +%Y%m%d-%H%M%S) \
   && mv ~/clap_trigger.py.new ~/clap_trigger.py \
   && sudo systemctl start clap-trigger.service'

# 5. Verify startup banner appears in the journal.
```

**Rollback**: `cp ~/clap_trigger.py.bak.<timestamp> ~/clap_trigger.py && sudo systemctl restart clap-trigger.service`. Under 10 seconds.

## Validation history

The detector was developed iteratively with empirical recordings on the
real hardware. Final validation results against four reference WAVs (all
on the Pi at `/home/martijn/clap-test-{1,2,3}.wav` and
`/home/martijn/triple-clap-test-2.wav`):

| Recording | What's in it | Gate triggers | Classifier accepts | Sequences emitted |
|---|---|---|---|---|
| clap-test-1.wav | 12 point-blank claps | 12 | **12** | 9 |
| clap-test-2.wav | 16 slams/stomps/drawer slams | 17 | **0** | 0 |
| clap-test-3.wav | 9 slams + stomps | 11 | **0** | 0 |
| triple-clap-test-2.wav | 15 triple-clap sequences from varying positions | 46 | **45** | 15 (all `count=3`) |

The two clap-test files calibrate the "point-blank" regime (clause B in
the classifier). The triple-clap-test-2 file calibrates the "real-world"
regime (clause A). The slam files are the negative regression set.

The detector was redesigned several times during development. Each
redesign corrected a different failure mode:

1. **Original (`clapDetector` library)** — used post-bandpass peak
   detection. Could not reject broadband impulses (slams) because impulses
   contain energy at all frequencies even after aggressive filtering.
2. **Custom spectral classifier (3-condition AND rule)** — replaced peak
   detection with windowed FFT analysis. Worked perfectly on the original
   point-blank calibration but rejected most real-world claps because
   distance and reverb push the spectrum out of the narrow trained envelope.
3. **2-clause disjunctive classifier** (current) — handles both regimes
   with separate decision clauses, no overlap with slams on either axis.
