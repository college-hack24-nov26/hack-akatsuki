"""
Offline acoustic threat detector — YAMNet (AudioSet, 521 classes).

Runs entirely on-device: a 4 MB TFLite graph in models/yamnet.tflite, CPU only,
no network and no API key. This is the primary detector for forest gateways,
which routinely have no uplink; the cloud agent in ai_audio_agent.py is reduced
to optional enrichment layered on top of whatever this returns.

Two pieces of signal come out of here:

  1. Classification — YAMNet emits scores over the 521-class AudioSet ontology.
     THREAT_TAXONOMY folds the handful of classes that matter for a sanctuary
     into the project's own threat labels.

  2. Sound pressure — see measure_spl(). Deliberately NOT a whole-clip RMS.

Why peak-hold rather than clip RMS: a gunshot is a ~10 ms impulse inside a 5 s
recording. Averaging over the whole clip buries it — the demo gunshot measures
67 dB as a clip average but 97 dB at the peak, and 67 dB would sit below every
threshold in the system. Window RMS at 50 ms approximates IEC 61672 'F' (fast)
time weighting, which is what an actual SPL meter reports, so that is what the
thresholds are tuned against.
"""

import logging
import math
import os
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config import MIC_DB_OFFSET

logger = logging.getLogger("fastapi_iot.offline_audio")

BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = BASE_DIR / "models" / "yamnet.tflite"
LABELS_PATH = BASE_DIR / "models" / "yamnet_labels.txt"

# YAMNet consumes a fixed 15600-sample mono frame at 16 kHz (0.975 s).
YAMNET_SAMPLE_RATE = 16000
YAMNET_FRAME = 15600

# ── AudioSet class → DeepGreen threat taxonomy ────────────────────────────────
# Ordered by priority: the first family with a qualifying score wins, so a
# chainsaw heard over a running truck still reports as CHAINSAW.
#
# GUNSHOT folds in Explosion/Fireworks on purpose. YAMNet consistently labels
# real gunfire recorded on phone mics as "Fireworks" or "Explosion" — the
# acoustic signature (sharp broadband impulse, fast decay) is near-identical and
# AudioSet's training data reflects that confusion. For a poaching alert both
# warrant the same response, so collapsing them costs nothing and recovers a
# detection that would otherwise be missed entirely.
THREAT_TAXONOMY: List[Tuple[str, Tuple[str, ...], float]] = [
    ("CHAINSAW", (
        "Chainsaw", "Sawing", "Hand saw", "Wood", "Chopping (food)",
        "Power tool", "Drill",
    ), 0.30),
    ("GUNSHOT", (
        "Gunshot, gunfire", "Machine gun", "Fusillade", "Artillery fire",
        "Cap gun", "Explosion", "Fireworks", "Firecracker", "Boom", "Bang",
        "Eruption",
    ), 0.35),
    ("VEHICLE", (
        "Truck", "Medium engine (mid frequency)", "Heavy engine (low frequency)",
        "Motor vehicle (road)", "Vehicle", "Engine", "Diesel engine",
        "Motorcycle", "Car", "Idling", "Accelerating, revving, vroom",
        "Light engine (high frequency)",
    ), 0.35),
    ("POACHER_VOICE", (
        "Shout", "Yell", "Screaming", "Children shouting", "Whoop",
        "Speech", "Conversation", "Narration, monologue",
    ), 0.45),
    ("WILDFIRE", (
        "Fire", "Crackle", "Wildfire",
    ), 0.40),
]

# Classes that affirmatively indicate an undisturbed forest. Used only to
# describe a non-detection; they never suppress a threat family above.
AMBIENT_CLASSES = (
    "Environmental noise", "Silence", "Wind", "Wind noise (microphone)",
    "Rustling leaves", "Bird", "Bird vocalization, bird call, bird song",
    "Chirp, tweet", "Insect", "Cricket", "Rain", "Stream", "Water",
    "Outside, rural or natural", "Wild animals", "Animal", "Noise",
)

# Confidence floor below which an escalation to the camera is not worth the
# battery and bandwidth. Mirrors the documented ≥80% gate in plan.md.
CAMERA_ESCALATION_THRESHOLD = 80.0


class OfflineAudioDetector:
    """
    Lazily-loaded YAMNet wrapper. The interpreter is built on first use rather
    than at import so that a host without TensorFlow still starts the service —
    detection then degrades to the caller's heuristic instead of crashing boot.
    """

    def __init__(self) -> None:
        self._interpreter = None
        self._labels: List[str] = []
        self._load_failed = False

    # ── model loading ─────────────────────────────────────────────────────────
    def _ensure_loaded(self) -> bool:
        if self._interpreter is not None:
            return True
        if self._load_failed:
            return False

        try:
            if not MODEL_PATH.exists():
                raise FileNotFoundError(f"YAMNet model missing at {MODEL_PATH}")

            # Prefer the standalone LiteRT runtime when present (smaller, and
            # the supported path going forward); fall back to the copy shipped
            # inside TensorFlow. Note tf.lite.Interpreter is reachable only as
            # an attribute — `from tensorflow.lite import Interpreter` raises
            # ImportError on TF 2.21.
            try:
                from ai_edge_litert.interpreter import Interpreter  # type: ignore
            except ImportError:
                import tensorflow as tf  # type: ignore
                Interpreter = tf.lite.Interpreter

            interp = Interpreter(model_path=str(MODEL_PATH))
            interp.allocate_tensors()

            with open(LABELS_PATH, "r", encoding="utf-8") as fh:
                labels = [ln.strip() for ln in fh if ln.strip()]

            if len(labels) != 521:
                logger.warning(
                    f"[OfflineAudio] Expected 521 AudioSet labels, found {len(labels)}."
                )

            self._interpreter = interp
            self._labels = labels
            logger.info(
                f"🧠 [OfflineAudio] YAMNet ready ({MODEL_PATH.stat().st_size / 1e6:.1f} MB, "
                f"{len(labels)} classes) — fully offline, CPU only."
            )
            return True

        except Exception as exc:
            self._load_failed = True
            logger.error(
                f"❌ [OfflineAudio] Could not initialise YAMNet: {exc}. "
                "Acoustic classification will fall back to dB thresholds only."
            )
            return False

    @property
    def available(self) -> bool:
        return self._ensure_loaded()

    # ── audio IO ──────────────────────────────────────────────────────────────
    @staticmethod
    def _read_wav_mono16k(path: str) -> Optional[np.ndarray]:
        """
        Load a 16-bit PCM WAV as float32 in [-1, 1], downmixed to mono and
        resampled to 16 kHz. The ESP32 firmware already emits 16 kHz mono, so
        the resample path only runs for operator-supplied or test audio.
        """
        try:
            with wave.open(path, "rb") as wf:
                channels = wf.getnchannels()
                rate = wf.getframerate()
                width = wf.getsampwidth()
                raw = wf.readframes(wf.getnframes())

            if width != 2:
                logger.warning(f"[OfflineAudio] Unsupported sample width {width * 8}-bit.")
                return None
            if not raw:
                return None

            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

            if channels > 1:
                usable = (len(samples) // channels) * channels
                samples = samples[:usable].reshape(-1, channels).mean(axis=1)

            if rate != YAMNET_SAMPLE_RATE and rate > 0:
                target_len = int(round(len(samples) * YAMNET_SAMPLE_RATE / rate))
                if target_len > 1:
                    samples = np.interp(
                        np.linspace(0.0, len(samples) - 1, target_len),
                        np.arange(len(samples)),
                        samples,
                    ).astype(np.float32)

            return samples

        except Exception as exc:
            logger.warning(f"[OfflineAudio] Failed to read WAV {path}: {exc}")
            return None

    # ── sound pressure ────────────────────────────────────────────────────────
    @staticmethod
    def measure_spl(samples: np.ndarray) -> Dict[str, float]:
        """
        Report dB SPL three ways. `spl_db` (50 ms max-window RMS) is the value
        the rest of the system treats as "the" reading; the other two are kept
        for forensics and for anyone re-calibrating MIC_DB_OFFSET later.

        MIC_DB_OFFSET is an uncalibrated guess by default — see config.py. These
        numbers are only correct in absolute terms once it has been set against
        a reference meter; relative ordering between events is reliable either way.
        """
        if samples is None or len(samples) == 0:
            return {"spl_db": 40.0, "rms_db": 40.0, "peak_db": 40.0}

        def to_db(amplitude: float) -> float:
            return 20.0 * math.log10(max(float(amplitude), 1e-9)) + MIC_DB_OFFSET

        rms_db = to_db(float(np.sqrt(np.mean(np.square(samples)))))
        peak_db = to_db(float(np.max(np.abs(samples))))

        window = int(YAMNET_SAMPLE_RATE * 0.05)  # 50 ms ≈ IEC 61672 'F' weighting
        if len(samples) >= window:
            trimmed = samples[: (len(samples) // window) * window]
            frames = trimmed.reshape(-1, window)
            window_rms = np.sqrt(np.mean(np.square(frames), axis=1))
            spl_db = to_db(float(np.max(window_rms)))
        else:
            spl_db = rms_db

        clamp = lambda v: round(max(30.0, min(140.0, v)), 1)
        return {
            "spl_db": clamp(spl_db),
            "rms_db": clamp(rms_db),
            "peak_db": clamp(peak_db),
        }

    # ── inference ─────────────────────────────────────────────────────────────
    def _run_yamnet(self, samples: np.ndarray) -> Optional[np.ndarray]:
        """
        Slide YAMNet over the clip with 50% overlap and keep the per-class max.
        Max rather than mean because threats here are transient: a single
        chainsaw burst in an otherwise quiet 5 s clip should score as a chainsaw,
        and averaging would dilute it below threshold.
        """
        if not self._ensure_loaded():
            return None

        interp = self._interpreter
        inp = interp.get_input_details()[0]
        out = interp.get_output_details()[0]

        if len(samples) < YAMNET_FRAME:
            samples = np.pad(samples, (0, YAMNET_FRAME - len(samples)))

        hop = YAMNET_FRAME // 2
        collected: List[np.ndarray] = []

        for start in range(0, len(samples) - YAMNET_FRAME + 1, hop):
            frame = np.ascontiguousarray(
                samples[start:start + YAMNET_FRAME], dtype=np.float32
            )
            try:
                interp.set_tensor(inp["index"], frame)
                interp.invoke()
                collected.append(interp.get_tensor(out["index"])[0].copy())
            except Exception as exc:
                logger.warning(f"[OfflineAudio] YAMNet frame failed: {exc}")
                break

        if not collected:
            return None
        return np.max(np.stack(collected), axis=0)

    def _classify(self, scores: np.ndarray) -> Dict[str, Any]:
        """Fold raw AudioSet scores into the project's threat taxonomy."""
        label_score = {
            self._labels[i]: float(scores[i])
            for i in range(min(len(self._labels), len(scores)))
        }

        top_idx = np.argsort(scores)[::-1][:5]
        top_labels = [
            {"label": self._labels[i], "score": round(float(scores[i]), 4)}
            for i in top_idx
            if i < len(self._labels)
        ]

        for threat_type, members, threshold in THREAT_TAXONOMY:
            best_label, best_score = None, 0.0
            for member in members:
                s = label_score.get(member, 0.0)
                if s > best_score:
                    best_label, best_score = member, s

            if best_score >= threshold:
                # Rescale the model score into a reportable 0-100 confidence.
                # A YAMNet score at threshold maps to ~60%, saturating near 99%;
                # raw AudioSet scores rarely exceed 0.9 even when unambiguous,
                # so reporting them directly would understate every detection.
                span = max(1e-6, 1.0 - threshold)
                confidence = 60.0 + 39.0 * min(1.0, (best_score - threshold) / span)
                return {
                    "threat_detected": True,
                    "threat_type": threat_type,
                    "confidence_score": round(confidence, 1),
                    "matched_label": best_label,
                    "matched_score": round(best_score, 4),
                    "top_labels": top_labels,
                }

        ambient_best = max(
            ((lbl, label_score.get(lbl, 0.0)) for lbl in AMBIENT_CLASSES),
            key=lambda kv: kv[1],
            default=("Environmental noise", 0.0),
        )
        return {
            "threat_detected": False,
            "threat_type": "NORMAL_AMBIENT",
            "confidence_score": round(min(99.0, 70.0 + 29.0 * ambient_best[1]), 1),
            "matched_label": ambient_best[0],
            "matched_score": round(ambient_best[1], 4),
            "top_labels": top_labels,
        }

    # ── public entry point ────────────────────────────────────────────────────
    def analyze(self, wav_path: str, trigger_source: str = "VIBRATION_INTERRUPT") -> Optional[Dict[str, Any]]:
        """
        Classify one WAV. Returns a dict in the same shape AudioAIAgent emits,
        or None if the file is unreadable or the model is unavailable — the
        caller then decides how to degrade.
        """
        if not wav_path or not os.path.exists(wav_path):
            logger.warning(f"[OfflineAudio] WAV not found: {wav_path}")
            return None

        samples = self._read_wav_mono16k(wav_path)
        if samples is None or len(samples) == 0:
            return None

        spl = self.measure_spl(samples)
        scores = self._run_yamnet(samples)
        if scores is None:
            return None

        verdict = self._classify(scores)
        threat_detected = verdict["threat_detected"]
        confidence = verdict["confidence_score"]
        threat_type = verdict["threat_type"]

        # A loud impulse that YAMNet could not attribute is still worth a look.
        if not threat_detected and spl["spl_db"] >= 85.0:
            threat_detected = True
            threat_type = "ACOUSTIC_DISTURBANCE"
            confidence = 65.0
            verdict["matched_label"] = "(unclassified loud impulse)"

        escalate = threat_detected and confidence >= CAMERA_ESCALATION_THRESHOLD

        if threat_detected:
            severity = "ALERT" if confidence >= CAMERA_ESCALATION_THRESHOLD else "WARNING"
        else:
            severity = "INFO"

        reasoning = (
            f"YAMNet matched '{verdict['matched_label']}' at {verdict['matched_score']:.2f} "
            f"over the AudioSet ontology; peak sound pressure {spl['spl_db']} dB SPL "
            f"(impulse peak {spl['peak_db']} dB). Classified as {threat_type}."
        )

        result = {
            "success": True,
            "threat_detected": threat_detected,
            "threat_type": threat_type,
            "confidence_score": confidence,
            "decibel_level": spl["spl_db"],
            "severity": severity,
            "reasoning": reasoning,
            "transcription": "(offline acoustic classification — no speech transcription)",
            "escalate_camera": escalate,
            "model_used": "yamnet-audioset-521 (offline)",
            "offline": True,
            "acoustic_detail": {
                "matched_label": verdict["matched_label"],
                "matched_score": verdict["matched_score"],
                "top_labels": verdict["top_labels"],
                "spl_db": spl["spl_db"],
                "rms_db": spl["rms_db"],
                "peak_db": spl["peak_db"],
                "trigger_source": trigger_source,
            },
        }

        logger.info(
            f"🧠 [OfflineAudio] {threat_type} | conf={confidence}% | "
            f"{spl['spl_db']} dB SPL (peak {spl['peak_db']}) | "
            f"matched '{verdict['matched_label']}' @ {verdict['matched_score']:.2f} | "
            f"escalate_camera={escalate}"
        )
        return result


# Singleton — mirrors the module-level agent instances elsewhere in services/.
offline_audio_detector = OfflineAudioDetector()
