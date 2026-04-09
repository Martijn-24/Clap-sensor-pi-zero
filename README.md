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
spectral content. Real claps and slams have completely different spectra
when measured this way:

| Feature | Real claps | Door slams / stomps |
|---|---|---|
| Spectral centroid | 735–2651 Hz | 8–1149 Hz |
| Energy below 500 Hz | 3–32% | 47–100% |
| Energy in 500–4000 Hz | 67–94% | 0–43% |

(Numbers from offline analysis of three ~45 second test recordings on this
exact mic and room.)

The classifier requires **all three** of:

- `centroid >= 600 Hz`
- `low_pct <= 60%` (energy below 500 Hz)
- `mid_pct >= 60%` (energy in 500–4000 Hz)

Mid-band energy is the primary discriminator — there's a 24-point gap
between the worst real clap (67%) and the worst false-positive slam (43%).
The other two checks are belt-and-suspenders against edge cases the
recordings might not have captured.

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
| `CLASSIFIER_CENTROID_MIN_HZ` | Minimum spectral centroid for "this is a clap" |
| `CLASSIFIER_LOW_PCT_MAX` | Maximum below-500 Hz energy ratio |
| `CLASSIFIER_MID_PCT_MIN` | Minimum 500–4000 Hz energy ratio |
| `CLAP_INTERVAL_S` | Max gap between claps in one sequence |
| `SEQUENCE_TIMEOUT_S` | Silence after last clap before sequence finalizes |
| `POST_FIRE_COOLDOWN_S` | Gate-disabled window after a successful webhook |
| `LOG_LEVEL` | `QUIET` / `NORMAL` / `VERBOSE` |

If you tune any of these, **always re-run the offline replay** (see below)
against the test WAVs before restarting the live service.

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
real hardware. Final validation results before deploy:

| Recording | Gate triggers | Classified accepts | Classified rejects | Sequences emitted |
|---|---|---|---|---|
| clap-test-1.wav (claps only) | 12 | **12** | 0 | 11 |
| clap-test-2.wav (slams only) | 16 | 0 | **16** | 0 |
| clap-test-3.wav (slams only) | 9 | 0 | **9** | 0 |

And the live test session immediately after deploy: 5 real claps detected,
7 false-positive sounds rejected, 100% accuracy with no near-misses on the
clap side. The closest false-positive call was a sharp thud at mid_pct =
55.5% — still 4.5 points below the 60% threshold.
