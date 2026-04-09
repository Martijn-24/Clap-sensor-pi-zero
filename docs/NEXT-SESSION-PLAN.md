# Next-session plan: improving clap detection over background music

This is a working-state handoff document. Read it first when you pick this
up next time. It captures (a) where we are now, (b) what we tried, (c) what
we ruled out, and (d) what concrete improvements are worth pursuing, in
priority order.

## Current state (frozen good)

The detector lives at `clap_trigger.py` and runs as `clap-trigger.service`
on `bedroompi` (`martijn@192.168.50.192`). It uses a custom 2-clause
spectral classifier on 4096-sample FFT windows around adaptive-gate events.
**Last commit on `main`:** see `git log --oneline -5` — final tuned
configuration was deployed in commit `591144c` (adaptive gate).

### Empirical performance, validated by offline replay against 7 reference WAVs on the Pi

| Reference WAV | What it contains | Accepts | Status |
|---|---|---|---|
| `clap-test-1.wav` | 12 point-blank claps | 12 / 12 | ✅ |
| `clap-test-2.wav` | 16 slams/stomps/drawer slams | 0 / 16 | ✅ |
| `clap-test-3.wav` | 9 slams + stomps | 0 / 9 | ✅ |
| `triple-clap-test-2.wav` | 15 living-room triple-claps from varying positions | 45 / 46 | ✅ |
| `bedroom-claps.wav` | 13 bedroom triple-claps from varying positions | 39 / 39 | ✅ |
| `bedroom-negatives.wav` | bedroom slams/stomps | 0 / 14 | ✅ |
| `bedroom-music-claps.wav` | claps over Radio Paradise at typical volume | 6 / ~16 (4 sequences) | ⚠️ ~50% catch rate |

The system **works perfectly when there's no music in the room**. The only
remaining issue is detection accuracy when music is playing through the
WiiM in the same room as the Pi mic — currently around 50-65% catch rate
on offline replay, slightly better in live testing because the music isn't
constantly at full level.

### Why parameter tuning is exhausted

Two grid searches (~80 configurations) over (`gate_open_mult`,
`gate_close_mult`, `mid_pct_floor`, `gate_history_seconds`) confirmed the
current deployed configuration is at the global maximum on the existing
dataset:

```
GATE_FLOOR_OPEN              = 0.04
GATE_FLOOR_CLOSE             = 0.02
GATE_ADAPT_HISTORY_S         = 3.0
GATE_ADAPT_MULT_OPEN         = 4.0
GATE_ADAPT_MULT_CLOSE        = 2.0
CLASSIFIER_CENTROID_HIGH_MIN_HZ = 1300.0
CLASSIFIER_CENTROID_LOW_MIN_HZ  = 600.0
CLASSIFIER_MID_PCT_HIGH_MIN  = 60.0
CLASSIFIER_LOW_PCT_MAX       = 60.0
```

**No combination of these constants does better on `bedroom-music-claps.wav`
without breaking one of the other 6 datasets.** The next improvement has to
come from architecture or inputs, not constants.

### What was already considered and ruled out for last session

- **Lower mid_pct floor to 50** — admits a slam at 52.3% mid_pct from
  `clap-test-2.wav`. 55 is the lowest safe value, and the grid search
  proved 55 vs 60 makes no difference on test 7.
- **Lower open multiplier to 2.5-3** — produces more gate triggers on bass
  thumps, which puts the gate in refractory more often, *reducing* total
  music+clap accepts.
- **Raise open multiplier to 5+** — breaks `triple-clap-test-2.wav`
  (regression from 45 → 44 accepts).
- **Shorter gate history** (1-2s) — reduces test 7 score.
- **Longer history** (>3s) — no further improvement.

## What to try next, in priority order

The remaining improvements fall into three tiers. **Tier 1** items are
small code changes with measurable expected gain. **Tier 2** is significant
engineering with bigger payoff. **Tier 3** is hardware.

I recommend doing them in order, evaluating after each, and stopping when
the user is satisfied. Each item below has its own validation strategy
using the existing reference WAVs as the regression suite.

---

### Tier 1A: Try moving the mic before changing any code (~5 minutes, free, possibly huge gain)

Before any software change, the user should physically experiment with
**mic placement** in the bedroom. The goal is to maximize the SNR between
the user's claps and the WiiM's audio at the mic position. Inverse-square
law: doubling the distance from a sound source quarters the energy.

- Try positioning the mic 2-3× farther from the WiiM speaker than its
  current location, while still within the user's clapping range.
- Re-record `bedroom-music-claps.wav` from the new position.
- Run `python3 ~/clap_trigger.py --replay ~/bedroom-music-claps.wav` and
  count accepts. If the score jumps to 10+ accepts / 6+ sequences without
  any code changes, **you're done**, take the win.
- If yes, save the new recording as the new `bedroom-music-claps.wav`
  reference and commit a note about the new mic position to `README.md`.

### Tier 1B: Detect "music is on" and switch classifier profiles (~1-2 hours)

The Pi already has network access and can poll the WiiM at
`https://192.168.50.165/httpapi.asp?command=getPlayerStatus`. Switch
between two classifier profiles based on `status` field:

- **Strict profile** (when WiiM is `none`/`stop`/`pause`): current values
- **Lax profile** (when WiiM is `play`): looser thresholds calibrated
  against `bedroom-music-claps.wav`

Implementation sketch:
1. Add a `WiimStatusPoller` background thread (separate from the audio
   reader) that polls `getPlayerStatus` every 5s and updates a shared
   `is_music_playing` boolean.
2. Hook the boolean into the `Gate` and `classify_window` stages so they
   can pick which constants to use.
3. The lax profile would have something like `GATE_ADAPT_MULT_OPEN = 3.0`
   (more permissive), `CLASSIFIER_MID_PCT_HIGH_MIN = 45.0`, possibly
   `CLASSIFIER_CENTROID_HIGH_MIN_HZ = 1200.0`.
4. Validate the lax profile against `bedroom-music-claps.wav` only (the
   other datasets use the strict profile).
5. **Critical**: validate the strict profile against ALL OTHER reference
   WAVs to confirm zero regression.

**Expected gain**: 70-80% catch rate on music+claps without compromising
quiet-room accuracy. This is the **best ratio of effort to payoff** of all
the options.

**Risks**:
- Network polling adds an external dependency. If the WiiM is offline, the
  poller errors and we need to default to strict profile. Easy.
- The poller must not block the audio loop. Background thread with a
  shared atomic boolean handles this cleanly.
- Lag: the Pi might not know music has started for a few seconds. This is
  acceptable — claps right at the start of music will use the strict
  profile and probably not be detected, but everything after the first
  poll cycle is fine.

### Tier 1C: Lower the WiiM's volume in the n8n workflow (~10 minutes)

When the workflow turns the WiiM ON, it could **also** issue a `setPlayerCmd:vol:N`
command to cap the volume at, say, 60% of max. This is purely degrading
user experience to make detection easier — but if the user is OK with
slightly quieter default music, it's a free win.

Implementation: add one line to the WiiM ON branch in `clap-triggers-w-radio`:
```
await wiimCmd.call(this, 'setPlayerCmd:vol:60');
```

The user can still raise the volume manually via the WiiM app afterward —
this just sets the default that follows a clap-triggered turn-on.

**Expected gain**: maybe 15-25% improvement. Cosmetic compromise.

---

### Tier 2A: Spectral subtraction (~3-4 hours, ~150 lines)

This is the most interesting engineering task and has the highest expected
payoff among software-only options.

**The idea**: maintain a rolling **average spectrum** of the last few
seconds of audio (representing the music background). When the gate fires,
compute the FFT of the clap window AND **subtract the average spectrum**
before extracting features. The classifier then sees something close to
"clap minus music" instead of "clap plus music", and the contaminated
mid_pct values jump back into clean territory.

Implementation sketch:
1. Add a `SpectralBackground` class that maintains a rolling average of
   FFT magnitudes (not phases) over a 2-3 second window. Updated on every
   chunk of audio that's NOT inside a gate event.
2. Modify `classify_window()` to take an optional `background_spectrum`
   argument. When provided, subtract it (in magnitude space, with a floor
   at 0) before computing centroid/band ratios.
3. The subtraction should be element-wise on the FFT magnitude array, with
   any negative values clipped to a small positive epsilon (to avoid
   divide-by-zero in centroid computation).
4. Test thoroughly against ALL reference WAVs. Spectral subtraction can
   over-subtract and break clean clap detection if not done carefully.

**Validation strategy**:
- All 7 reference WAVs must continue to pass (no regression on any
  silent-room dataset).
- `bedroom-music-claps.wav` should jump to 80-90% catch rate.
- Optionally, capture a fresh recording of "music with no claps" to
  validate that subtraction doesn't accidentally manufacture phantom claps.

**Subtle gotchas**:
- The background spectrum must NOT be updated while the gate is open or in
  refractory — otherwise the clap itself contaminates the background.
- Music isn't perfectly stationary; the background lags real changes by
  the averaging window. A song change will produce a few seconds of
  imperfect subtraction.
- Spectral subtraction is well-known in noise reduction (look up "noise
  gate vs spectral gate" and "Boll's spectral subtraction algorithm" for
  prior art). Keep it simple: magnitude subtraction with a floor.

**Expected gain**: 80-90% catch rate over music. This is the right
software solution if the user wants to invest the engineering time.

### Tier 2B: MFCC + small ML classifier (~1 day with training pipeline)

Replace the hand-tuned classifier with Mel-Frequency Cepstral Coefficients
(the standard speech-recognition feature set) plus a small classifier
(logistic regression, kNN, or random forest from scikit-learn).

Why it might help: MFCCs capture spectral envelope shape in a way that's
more robust to additive noise than raw band ratios. ML can learn
separability we can't articulate manually.

Why it might NOT help: training data is small (~100 events across the
reference WAVs). Models trained on small data don't generalize well. And
we lose the interpretable "why was this rejected?" log lines that have
been so valuable for debugging.

**My honest opinion**: skip this unless Tier 2A doesn't work. Tier 2A is
mathematically principled and addresses the actual physical problem. Tier
2B is "throw ML at it and hope".

---

### Tier 3: Hardware (only if software is exhausted)

#### Tier 3A: Cardioid USB mic (~$30-50, ~30 min installation)

A directional mic physically attenuates sound from one direction. Aim it
**away from the WiiM speaker** and **toward where the user typically claps**.
Inverse square law plus the cardioid's polar pattern can give 15-20 dB
attenuation of the music while preserving claps.

**Recommended models** (not validated, search for current options):
- Samson Q2U
- Maono PD400X
- Any Blue Yeti in cardioid mode

**Risks**:
- Cardioid mics still pick up reflections from walls. A small bedroom may
  have so much reverb that the cardioid advantage is reduced.
- New mic = new spectral signature. The classifier may need re-calibration
  even without music playing.

**Expected gain**: 80-95% catch rate over music with just hardware, no
software changes.

#### Tier 3B: Acoustic Echo Cancellation (most work, biggest gain)

Smart speakers solve this exact problem with AEC. Feed the WiiM's audio
output as a reference signal into the mic processing pipeline, and
**adaptively subtract** the speaker's contribution using NLMS or FDAF.

**Requires**: a way to get the WiiM's output signal as a digital reference.
Options:
- Tap the WiiM's analog out into the Pi's USB sound card line-in (if
  available)
- Network-stream the WiiM's audio to the Pi (the Linkplay API may support
  this — needs investigation)
- Use a second mic positioned right next to the WiiM speaker to capture a
  "music only" reference

**Risks**:
- Pi Zero may not have CPU for real-time AEC (this is a hard real-time
  signal processing problem)
- Significant engineering effort
- Hardware complexity

**Expected gain**: 95%+ catch rate over music. This is what consumer smart
speakers do, and it works.

---

## Recommended path for next session

Step through these in order. Stop at the first one that satisfies the user.

1. **Tier 1A** (mic placement experiment) — 5 minutes, free, possibly
   solves the whole problem
2. **Tier 1C** (volume cap) — 10 minutes, easy win, mild user-experience
   degradation
3. **Tier 1B** (WiiM-aware profiles) — 1-2 hours, the highest-value
   software change
4. **Tier 2A** (spectral subtraction) — 3-4 hours, the most principled
   software solution
5. **Tier 3A** (cardioid mic) — $30-50 + 30 min, works around the problem
   physically
6. **Tier 3B** (AEC) — significant project, only if you really want >95%

Personally if it were my system I'd do **Tier 1A → Tier 1C → Tier 1B**
and call it done. Spectral subtraction is the "right" answer but in a
home automation context the marginal improvement isn't worth the
complexity unless you specifically enjoy the engineering.

## Reference data

The seven reference WAVs needed for validation all live on the Pi at
`/home/martijn/`:

- `clap-test-1.wav`
- `clap-test-2.wav`
- `clap-test-3.wav`
- `triple-clap-test-2.wav`
- `bedroom-claps.wav`
- `bedroom-negatives.wav`
- `bedroom-music-claps.wav`

These are NOT in git (they're large binary files and the analyzer + the
script are the reproducible artifacts). **Back them up before reflashing
the Pi**. They are the regression test suite for any future classifier
change.

The grid search scripts used in the previous session are at
`.tools/clap-analysis/grid_search.sh` and `grid_search_2.sh` in the parent
project. They're useful templates for any future parameter sweep.

The offline analyzer is at `.tools/clap-analysis/analyze_claps.py` and is
also uploaded to the Pi as `~/analyze_claps.py`. Use it to inspect any
new recording with full FFT spectral features per detected onset.

## How to deploy any change

The atomic-swap pattern is documented in the main `README.md`. Brief:

```bash
# 1. Edit clap_trigger.py locally
# 2. Upload to Pi as .new
node -e "process.stdout.write(require('fs').readFileSync('clap_trigger.py').toString('base64'));" \
  | node ../.tools/ssh-helper/run.js 192.168.50.192 martijn '<password>' \
    'base64 -d > ~/clap_trigger.py.new && python3 -m py_compile ~/clap_trigger.py.new'

# 3. Run offline replay validation against ALL 7 reference WAVs.
#    Confirm zero regression on tests 1-6, measurable improvement on test 7.

# 4. Atomic swap and restart:
node ../.tools/ssh-helper/run.js 192.168.50.192 martijn '<password>' \
  'sudo systemctl stop clap-trigger.service \
   && cp ~/clap_trigger.py ~/clap_trigger.py.bak.$(date +%Y%m%d-%H%M%S) \
   && mv ~/clap_trigger.py.new ~/clap_trigger.py \
   && sudo systemctl start clap-trigger.service'

# 5. Verify startup banner appears in journal.
# 6. Live test with music playing.
# 7. Commit and push to main.
```

Rollback is one `cp` away if anything goes wrong.
