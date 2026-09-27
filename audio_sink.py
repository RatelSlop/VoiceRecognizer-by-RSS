"""
audio_sink.py - High-Performance AudioSink voor Discord spraakherkenning en trefwoorddetectie.
Ondersteunt Whisper (state-of-the-art ASR), Google Speech en Vosk.
Met Automatic Gain Control (AGC) en Voice Activity Slicing voor maximale accuratesse.
"""

import os
import re
import json
import time
import queue
import logging
import threading
import audioop
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Callable, Optional, Tuple

import discord
from discord.ext import voice_recv
import numpy as np

# ASR Engines
try:
    import faster_whisper
    HAS_WHISPER = True
except ImportError:
    HAS_WHISPER = False

try:
    import speech_recognition as sr
    HAS_SR = True
except ImportError:
    HAS_SR = False

try:
    import vosk
    HAS_VOSK = True
except ImportError:
    HAS_VOSK = False

logger = logging.getLogger("KeywordBot.AudioSink")


def apply_agc(pcm_bytes: bytes) -> bytes:
    """Verhoogt dynamisch het volume van zachte microfoons (tot 6x boost)."""
    if not pcm_bytes:
        return pcm_bytes
    try:
        max_amp = audioop.max(pcm_bytes, 2)
        if max_amp < 150:
            return pcm_bytes
        target_amp = 22000.0
        factor = min(6.0, max(1.0, target_amp / max_amp))
        if factor > 1.05:
            return audioop.mul(pcm_bytes, 2, factor)
    except Exception:
        pass
    return pcm_bytes


class DummySpeaker:
    """Fallback gebruiker als Discord geen Member object doorgeeft."""
    def __init__(self, speaker_id: int, name: str = "Onbekende Spreker"):
        self.id = speaker_id
        self.name = name
        self.display_name = name
        self.bot = False

    def __str__(self):
        return f"{self.display_name} (ID: {self.id})"


class KeywordAudioSink(voice_recv.AudioSink):
    """
    Ontvangt audio streams van alle gebruikers in het spraakkanaal,
    past AGC toe en transcribeert spraaksegmenten met Whisper / Google Speech.
    """

    def __init__(
        self,
        bot: discord.Client,
        config: Dict[str, Any],
        on_keyword_detected: Callable[[Any, str, str], None],
        get_config_func: Optional[Callable[[], Dict[str, Any]]] = None
    ):
        super().__init__()
        self.bot = bot
        self.config = config
        self.get_config = get_config_func or (lambda: self.config)
        self.on_keyword_detected = on_keyword_detected

        # Whisper Model initialisatie (indien beschikbaar)
        self.whisper_model = None
        if HAS_WHISPER:
            try:
                logger.info("Whisper model laden (base, int8)...")
                self.whisper_model = faster_whisper.WhisperModel("base", device="cpu", compute_type="int8")
                logger.info("✅ Whisper model succesvol geladen!")
            except Exception as e:
                logger.warning(f"Kon Whisper model niet laden: {e}")

        # Vosk Model initialisatie (fallback)
        self.vosk_model = None
        if HAS_VOSK:
            try:
                self.vosk_model = vosk.Model(lang="nl")
            except Exception:
                pass

        # Threadpool voor asynchrone spraakherkenning
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ASR-Worker")

        # Worker threads en wachtrijen per spreker
        self.user_queues: Dict[int, queue.Queue] = {}
        self.user_threads: Dict[int, threading.Thread] = {}
        self.user_ratecv_state: Dict[int, Any] = {}
        self.user_objects: Dict[int, Any] = {}
        self.running = True
        self.lock = threading.Lock()

        # Cooldown per guild / bot om audio overlap te voorkomen
        self.last_trigger_time = 0.0

        # Diagnostische statistieken
        self.total_packets_received = 0
        self.last_packet_time = 0.0
        self.last_transcription = ""
        self.last_detected_keyword = ""

    def wants_opus(self) -> bool:
        return False

    def write(self, user: Optional[discord.User], data: voice_recv.VoiceData) -> None:
        if not self.running:
            return

        self.total_packets_received += 1
        self.last_packet_time = time.time()

        ssrc = data.packet.ssrc if hasattr(data, "packet") and data.packet else 0
        speaker_id = None
        speaker_obj = user

        if speaker_obj and getattr(speaker_obj, "bot", False):
            return

        if speaker_obj:
            speaker_id = speaker_obj.id
        elif self.voice_client:
            resolved_id = self.voice_client._get_id_from_ssrc(ssrc) if hasattr(self.voice_client, "_get_id_from_ssrc") else None
            if resolved_id:
                speaker_id = resolved_id
                if self.voice_client.channel:
                    for m in self.voice_client.channel.members:
                        if m.id == resolved_id:
                            speaker_obj = m
                            break
            else:
                speaker_id = ssrc

        if not speaker_id:
            speaker_id = ssrc or 999999

        if not speaker_obj:
            speaker_obj = DummySpeaker(speaker_id, f"Spreker-{speaker_id}")

        cfg = self.get_config()
        targets = cfg.get("target_user_ids", [])
        if targets and speaker_id not in targets:
            return

        with self.lock:
            self.user_objects[speaker_id] = speaker_obj
            if speaker_id not in self.user_queues:
                q = queue.Queue(maxsize=300)
                self.user_queues[speaker_id] = q
                self.user_ratecv_state[speaker_id] = None

                thread = threading.Thread(
                    target=self._audio_capture_worker,
                    args=(speaker_id, q),
                    daemon=True,
                    name=f"CaptureWorker-{speaker_id}"
                )
                self.user_threads[speaker_id] = thread
                thread.start()

            user_q = self.user_queues[speaker_id]

        if not user_q.full():
            user_q.put_nowait(data.pcm)

    def _check_text_for_keywords(self, text: str) -> Optional[Tuple[str, str]]:
        if not text:
            return None

        clean_text = text.lower().strip()
        # Verwijder leestekens van begin en eind van woorden
        raw_words = clean_text.split()
        words = [re.sub(r'^[^\w]+|[^\w]+$', '', w) for w in raw_words if w]
        compact_text = "".join(words)

        cfg = self.get_config()
        keywords_dict: Dict[str, str] = cfg.get("keywords", {})
        default_sound = cfg.get("default_sound", "buzzer.wav")

        for kw, sound in keywords_dict.items():
            kw_clean = kw.strip().lower()
            if not kw_clean:
                continue

            sound_to_play = sound if sound else default_sound

            # Regel 1: Exacte woordmatch (bijv. "bro" in ["yo", "bro"] of "kanker" in ["echt", "kanker"])
            if kw_clean in words:
                return (kw_clean, sound_to_play)

            # Regel 2: Voor woorden met 4+ letters (zoals "kanker", "kaas"),
            # sta samenstellingen of voorvoegsels toe (bijv. "kankerzooi", "kankerhond", "kaasbroodje")
            if len(kw_clean) >= 4:
                for w in words:
                    if w.startswith(kw_clean) or (kw_clean in w and len(w) <= len(kw_clean) + 8):
                        return (kw_clean, sound_to_play)
                if kw_clean in compact_text:
                    return (kw_clean, sound_to_play)

            # Regel 3: Bekende ASR fonetische varianten (bijv. Whisper die "konker" of "canker" hoort voor "kanker")
            if kw_clean == "kanker":
                for w in words:
                    if any(var in w for var in ["konker", "canker", "kankur", "konk"]):
                        return (kw_clean, sound_to_play)

        return None

    def _audio_capture_worker(self, speaker_id: int, q: queue.Queue) -> None:
        """
        Leest 48kHz audio van de wachtrij, resampleert naar 16kHz mono,
        verzamelt spraak met dynamische VAD en stuurt segmenten direct naar de ASR threadpool.
        """
        speaker_obj = self.user_objects.get(speaker_id) or DummySpeaker(speaker_id)
        speaker_name = getattr(speaker_obj, "display_name", str(speaker_id))
        logger.info(f"🎙️ Luisteren gestart voor: {speaker_name}")

        state = None
        buffer = bytearray()
        silence_packets = 0

        # Parameters
        min_speech_bytes = int(16000 * 2 * 0.30)   # minstens 0.30s spraak
        max_speech_bytes = int(16000 * 2 * 2.8)    # max 2.8s per segment
        silence_threshold = 70                     # RMS drempelwaarde (gevoelig voor zachte klanken)
        silence_timeout_packets = 14               # ~0.42s stilte na spraak

        while self.running:
            try:
                # 30ms timeout om pauzes soepel te registreren
                pcm_data = q.get(timeout=0.03)
            except queue.Empty:
                pcm_data = None

            if pcm_data is None:
                if not self.running:
                    break
                if len(buffer) > 0:
                    silence_packets += 1
            else:
                try:
                    mono = audioop.tomono(pcm_data, 2, 1, 1)
                    resampled, state = audioop.ratecv(mono, 2, 1, 48000, 16000, state)
                    rms = audioop.rms(resampled, 2)

                    if rms > silence_threshold:
                        buffer.extend(resampled)
                        silence_packets = 0
                    else:
                        if len(buffer) > 0:
                            buffer.extend(resampled)
                            silence_packets += 1
                except Exception as e:
                    logger.debug(f"Conversiefout: {e}")

            # Trigger ASR segment:
            # 1. Pauze gedetecteerd (minstens 0.42s stilte na minstens 0.30s spraak)
            # 2. Of buffer bereikt maximale lengte van 2.8s (met 0.6s overlap om woorden niet te splitsen!)
            if len(buffer) >= max_speech_bytes:
                chunk_bytes = bytes(buffer)
                overlap = int(16000 * 2 * 0.6)  # 0.6s overlap voor vloeiende overgangen
                buffer = bytearray(buffer[-overlap:])
                silence_packets = 0
                boosted_chunk = apply_agc(chunk_bytes)
                self.executor.submit(self._process_asr, speaker_obj, boosted_chunk)

            elif len(buffer) >= min_speech_bytes and silence_packets >= silence_timeout_packets:
                chunk_bytes = bytes(buffer)
                buffer.clear()
                silence_packets = 0
                boosted_chunk = apply_agc(chunk_bytes)
                self.executor.submit(self._process_asr, speaker_obj, boosted_chunk)

            elif len(buffer) > 16000 * 2 * 8:
                buffer.clear()
                silence_packets = 0

        logger.info(f"🛑 Luisteren gestopt voor: {speaker_name}")

    def _process_asr(self, speaker_obj: Any, audio_bytes: bytes) -> None:
        """Transcribeert een audio segment met de geconfigureerde engine en automatische fallback."""
        speaker_name = getattr(speaker_obj, "display_name", "Iemand")
        cfg = self.get_config()
        engine = cfg.get("stt_engine", "google").lower()

        text = ""

        # Engine 1: Google Speech Recognition (Snelst ~0.4s & perfect Nederlands)
        if engine == "google" and HAS_SR:
            try:
                recognizer = sr.Recognizer()
                recognizer.energy_threshold = 80
                audio_data = sr.AudioData(audio_bytes, 16000, 2)
                text = recognizer.recognize_google(audio_data, language="nl-NL")
            except sr.UnknownValueError:
                text = ""
            except Exception as e:
                logger.warning(f"Google Speech fout ({e}), val terug op Whisper...")
                if self.whisper_model:
                    try:
                        audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                        segments, _ = self.whisper_model.transcribe(audio_np, language="nl", beam_size=1)
                        text = " ".join([s.text for s in segments]).strip()
                    except Exception as we:
                        logger.error(f"Whisper fallback fout: {we}")

        # Engine 2: OpenAI Whisper (Lokaal, Base model)
        elif engine == "whisper" and self.whisper_model:
            try:
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                segments, _ = self.whisper_model.transcribe(audio_np, language="nl", beam_size=1)
                text = " ".join([s.text for s in segments]).strip()
            except Exception as e:
                logger.error(f"Whisper fout: {e}")

        # Engine 3: Vosk (Fallback)
        elif engine == "vosk" and self.vosk_model:
            try:
                kws = [k.strip().lower() for k in cfg.get("keywords", {}).keys() if k.strip()]
                grammar = json.dumps(kws + ["[unk]"]) if kws else None
                rec = vosk.KaldiRecognizer(self.vosk_model, 16000, grammar) if grammar else vosk.KaldiRecognizer(self.vosk_model, 16000)
                rec.AcceptWaveform(audio_bytes)
                res = json.loads(rec.FinalResult())
                text = res.get("text", "")
            except Exception as e:
                logger.error(f"Vosk fout: {e}")

        # Als er tekst gevonden is
        if text:
            clean_display = text.replace("\n", " ").strip()
            self.last_transcription = f"[{speaker_name}]: {clean_display}"
            logger.info(f"🗣️ [{engine.upper()} gehoord van {speaker_name}]: '{clean_display}'")

            matched = self._check_text_for_keywords(clean_display)
            if matched:
                kw, sound = matched
                now = time.time()
                cooldown = cfg.get("cooldown_seconds", 1.5)

                vc = self.voice_client
                is_busy = vc and vc.is_playing()

                if not is_busy and (now - self.last_trigger_time >= cooldown):
                    self.last_trigger_time = now
                    self.last_detected_keyword = kw
                    logger.info(f"🚨 TREFWOORD GEDETECTEERD: '{kw}' door {speaker_name}! Speel: {sound}")
                    self.on_keyword_detected(speaker_obj, kw, sound)

    @voice_recv.AudioSink.listener()
    def on_voice_member_disconnect(self, member: discord.Member, ssrc: Optional[int]) -> None:
        if member:
            self._cleanup_user(member.id)
        if ssrc:
            self._cleanup_user(ssrc)

    def _cleanup_user(self, speaker_id: int) -> None:
        with self.lock:
            q = self.user_queues.pop(speaker_id, None)
            if q:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self.user_threads.pop(speaker_id, None)
            self.user_ratecv_state.pop(speaker_id, None)
            self.user_objects.pop(speaker_id, None)

    def cleanup(self) -> None:
        self.running = False
        with self.lock:
            for sid, q in list(self.user_queues.items()):
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self.user_queues.clear()
            self.user_threads.clear()
            self.user_ratecv_state.clear()
            self.user_objects.clear()
        self.executor.shutdown(wait=False)
