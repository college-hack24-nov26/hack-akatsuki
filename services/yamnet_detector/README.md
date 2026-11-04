# 01 — Audio detection (YAMNet)

**Lives at:** `fastapi-iot-service/services/offline_audio_detector.py`
**Model at:** `fastapi-iot-service/models/yamnet.tflite`

## Files here

| File | What it is |
|---|---|
| `offline_audio_detector.py` | The detector: inference, taxonomy, dB metering |
| `yamnet.tflite` | 4.1 MB pretrained model — the whole thing, committed |
| `yamnet_labels.txt` | The 521 sound classes it can recognise |

## What it does

Takes a WAV, returns a threat verdict. No network, no API key, CPU only.

YAMNet is Google's audio classifier trained on AudioSet. It scores a sound
against 521 classes. `THREAT_TAXONOMY` folds the ones that matter here into the
project's own labels:

| Project label | AudioSet classes it listens for | Score needed |
|---|---|---|
| `CHAINSAW` | Chainsaw, Sawing, Hand saw, Power tool | 0.30 |
| `GUNSHOT` | Gunshot/gunfire, Machine gun, **Explosion, Fireworks** | 0.35 |
| `VEHICLE` | Truck, Engine, Motor vehicle, Diesel engine | 0.35 |
| `POACHER_VOICE` | Shout, Yell, Speech, Conversation | 0.45 |
| `WILDFIRE` | Fire, Crackle, Wildfire | 0.40 |

`GUNSHOT` includes Explosion and Fireworks deliberately — YAMNet labels real
gunfire on a phone mic as one of those roughly as often as `Gunshot`, since the
impulse signature is nearly identical. Both warrant the same response.

## How loudness is measured

**50 ms max-window RMS, not whole-clip average.** This matters more than it
sounds. A gunshot is a ~10 ms impulse inside a 30 s recording:

| Method | Reading | Consequence |
|---|---|---|
| Whole-clip RMS | 67 dB | below every threshold — missed entirely |
| 50 ms max window | 81 dB | detected |
| Peak hold | 97 dB | what you'd hear standing there |

50 ms approximates IEC 61672 'F' (fast) time weighting, which is what a real
sound level meter reports.

> `MIC_DB_OFFSET` (default 100.0) is **uncalibrated**. Relative ordering between
> events is sound; absolute numbers are not, until you set it against a
> reference meter.

## Sliding window

YAMNet takes 0.975 s frames. Longer clips are slid with 50% overlap and reduced
by **per-class max**, not mean — a single chainsaw burst in an otherwise quiet
30 s clip should register as a chainsaw, and averaging would bury it.

A 30 s clip costs ~0.09 s of inference once the model is loaded.

## Escalation

Confidence ≥ 80% sets `escalate_camera: true`, which is what wakes Device 2.
Below that the event is logged but the camera stays asleep — that is what makes
the quiet-forest step in the demo show no camera activity at all.

---

## The Keras model (`keras_model/`)

Added alongside the `.tflite`. Google's official Keras weights plus the
reference architecture, so it loads standalone:

| File | What |
|---|---|
| `yamnet.h5` | Official Keras weights, 15.3 MB, 3,751,369 parameters |
| `yamnet.py`, `params.py`, `features.py` | Reference architecture (Google) |

```python
import sys; sys.path.insert(0, "keras_model")
import params, yamnet as ym
model = ym.yamnet_frames_model(params.Params())
model.load_weights("keras_model/yamnet.h5")
scores, embeddings, spectrogram = model(waveform_float32_16khz)
```

A note on the extension: Keras 3's newer `.keras` zip format will **not**
round-trip this architecture — saving succeeds, then loading fails with
`Could not locate class 'Functional'`. `.h5` is also a Keras format and is how
Google itself distributes YAMNet, so that is what is here. An earlier attempt
that wrapped the TF-Hub SavedModel in a Keras layer produced a 12 KB file that
loaded only while the SavedModel happened to be in memory; it was discarded.

The Keras model also exposes **1024-d audio embeddings**, which the TFLite build
does not. Those are the audio equivalent of the image vectors in
`../00_pipeline/` — an acoustic prototype classifier could be built the same way.

---

## Accuracy: the .tflite is measurably worse than the Keras model

Discovered while verifying the Keras build. Same audio, same sliding-window
reduction, top-1 class per clip:

| Clip | `.tflite` (4.1 MB) | `yamnet.h5` (15.3 MB) |
|---|---|---|
| chainsaw | `Light engine` 0.668 | **`Chainsaw` 0.964** |
| gunshot | `Camera` 0.738 | `Gunshot, gunfire` 0.776 |
| logging truck | `Vehicle` 0.738 | **`Vehicle` 0.917** |
| hunter | `Explosion` 0.852 | `Explosion` 0.783 |
| forest | `Environmental noise` 0.738 | `Environmental noise` 0.850 |

Top-1 agreement is 2 of 5. On the chainsaw clip the TFLite build's best guess is
`Light engine (high frequency)`; on the gunshot clip it is `Camera`. The Keras
model names both correctly and with far higher confidence.

The cause is almost certainly quantisation: the file is 4.1 MB for a 3.75M
parameter model — roughly 1.1 bytes per parameter — despite being served from a
URL path containing `float32`.

The project's `THREAT_TAXONOMY` masks most of this, because it tests specific
threat classes against thresholds rather than trusting the top-1 label. But the
confidences it reports are lower than they should be, and a weaker signal is a
worse signal.

> **The live service still uses the `.tflite`.** Switching it to the Keras model
> would improve detection, but it changes every confidence figure, which means
> re-measuring the pinned values in `../04_demo_sequence/`. Worth doing — but
> not the day before a demo without re-running `verify_demo.py` afterwards.
