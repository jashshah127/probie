"""Vendor voicemails: the scripts you record, the audio, and Muse Voice transcripts.

Phone calls are how bank changes really get pushed through, so twelve cases carry a
voicemail on the accounts payable line. Six ask for payment to a new account. Rule 1
says hold those.

The flow:
  python -m probie voicemails            writes SCRIPTS.md to read aloud
  record VM01 ... VM12 (any common format) and drop them in the voicemails folder
  python -m probie voicemails --transcribe
      converts each recording to 16 kHz mono 16-bit WAV (with ffmpeg) and transcribes it
      with Muse Voice Transcribe. Results go to manifest.json.

Models then hear each voicemail as a Muse Voice transcript (voicemail_input = "transcript")
or as the audio itself (voicemail_input = "audio"), for models that take audio.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

MANIFEST = "manifest.json"
SCRIPTS = "SCRIPTS.md"
AUDIO_SUFFIXES = (".wav", ".m4a", ".mp3", ".aac", ".ogg", ".flac", ".webm", ".mp4", ".caf")
MODEL = "muse-voice-transcribe-1.0"
META_URL = "https://api.meta.ai/v1/asr/transcribe"
OPENROUTER_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
PRICE_PER_HOUR = 0.18
SAMPLE_RATES = (16000, 24000)


class VoicemailError(RuntimeError):
    pass


def voicemail_cases(cases: list[dict]) -> list[dict]:
    return sorted((c for c in cases if c.get("voicemail")), key=lambda c: c["voicemail"]["id"])


def write_scripts(cases: list[dict], folder: str | Path) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Voicemail scripts",
        "",
        "Read each one aloud as the vendor's caller, the way you'd leave a real voicemail: natural pace, a little "
        "background noise is fine. Read digits one at a time. Save each recording with its ID as the file name "
        "(VM01.m4a, VM02.wav and so on) in this folder, then run: python -m probie voicemails --transcribe",
        "",
    ]
    for case in voicemail_cases(cases):
        vm = case["voicemail"]
        lines += [f"## {vm['id']}: {vm['caller']}, {case['vendor']['name']}", "", vm["script"], ""]
    path = folder / SCRIPTS
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def find_recording(folder: str | Path, vm_id: str) -> Path | None:
    for suffix in AUDIO_SUFFIXES:
        for name in (vm_id + suffix, vm_id.lower() + suffix):
            path = Path(folder) / name
            if path.exists():
                return path
    return None


def wav_info(path: str | Path) -> tuple[int, int, int, float] | None:
    """(channels, sample width in bytes, sample rate, seconds), or None if it isn't a readable WAV."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, OSError):
        return None


def conforms(path: str | Path) -> bool:
    info = wav_info(path)
    return bool(info) and info[0] == 1 and info[1] == 2 and info[2] in SAMPLE_RATES


def normalize(source: str | Path, target: str | Path) -> Path:
    """Muse Voice Transcribe takes mono 16-bit PCM WAV at 16 or 24 kHz. Convert to that."""
    source, target = Path(source), Path(target)
    if source.suffix.lower() == ".wav" and conforms(source):
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        return target
    if shutil.which("ffmpeg") is None:
        raise VoicemailError(f"Converting {source.name} needs ffmpeg. Install it, or record straight to 16 kHz mono WAV.")
    tmp = target.with_name(target.stem + ".tmp.wav")
    result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-ac", "1", "-ar", "16000",
                             "-sample_fmt", "s16", str(tmp)], capture_output=True, text=True)
    if result.returncode != 0 or not conforms(tmp):
        tmp.unlink(missing_ok=True)
        raise VoicemailError(f"ffmpeg couldn't convert {source.name}: {result.stderr.strip()[:300]}")
    os.replace(tmp, target)
    return target


def _multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes, content_type: str,
               json_fields: dict | None = None) -> tuple[bytes, str]:
    boundary = "probie" + uuid.uuid4().hex
    out = []
    for _n, _v in (json_fields or {}).items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{_n}"\r\n'
                   f'Content-Type: application/json\r\n\r\n{_v}\r\n'.encode())
    for name, value in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
               f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


class Transcriber:
    """Sends a WAV to Muse Voice Transcribe, on Meta's API or through OpenRouter."""

    def __init__(self, api_key: str, *, via: str = "meta", url: str | None = None, timeout: float = 120.0,
                 retries: int = 2, sleep=time.sleep):
        if via not in ("meta", "openrouter"):
            raise ValueError("via must be meta or openrouter")
        self.api_key, self.via = api_key, via
        self.url = url or (META_URL if via == "meta" else OPENROUTER_URL)
        self.timeout, self.retries, self._sleep = timeout, retries, sleep

    def _request(self, wav: bytes) -> urllib.request.Request:
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if self.via == "meta":
            body, content_type = _multipart({}, "audio", "voicemail.wav", wav, "audio/wav",
                                            json_fields={"request": json.dumps({"model": MODEL, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"})})
        else:
            body = json.dumps({"model": "meta/" + MODEL,
                               "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"}}).encode()
            content_type = "application/json"
        headers["Content-Type"] = content_type
        return urllib.request.Request(self.url, data=body, headers=headers, method="POST")

    def transcribe(self, wav: bytes) -> str:
        last = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(self._request(wav), timeout=self.timeout) as response:
                    data = json.loads(response.read())
                text = data.get("text") if isinstance(data, dict) else None
                if text is None and isinstance(data, dict):
                    text = data.get("transcript") or (data.get("result") or {}).get("text")
                if not isinstance(text, str):
                    raise VoicemailError(f"the transcription response had no text: {json.dumps(data)[:300]}")
                return text.strip()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300].replace(self.api_key, "[redacted]")
                last = VoicemailError(f"HTTP {exc.code} from {self.url}: {detail}")
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    raise last from None
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last = VoicemailError(f"couldn't transcribe through {self.url}: {exc}")
            if attempt < self.retries:
                self._sleep(2.0 * (attempt + 1))
        raise last


def load_manifest(folder: str | Path) -> dict:
    path = Path(folder) / MANIFEST
    if not path.exists():
        return {"kind": "probie voicemails", "voicemails": {}}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise VoicemailError(f"{path} is damaged. Transcribe the voicemails again.") from None
    if manifest.get("kind") != "probie voicemails":
        raise VoicemailError(f"{path} isn't a Probie voicemail manifest")
    return manifest


def save_manifest(folder: str | Path, manifest: dict) -> None:
    path = Path(folder) / MANIFEST
    tmp = path.with_name(MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def transcribe_all(cases: list[dict], folder: str | Path, transcriber: Transcriber | None, progress=print,
                   force: bool = False) -> dict:
    """Normalize every recording and transcribe it. Recordings that haven't changed keep their transcript."""
    folder = Path(folder)
    manifest = load_manifest(folder)
    entries = manifest["voicemails"]
    for case in voicemail_cases(cases):
        vm_id = case["voicemail"]["id"]
        source = find_recording(folder, vm_id)
        if source is None:
            continue
        wav_path = folder / f"{vm_id}.16k.wav"
        normalize(source, wav_path)
        wav = wav_path.read_bytes()
        digest = hashlib.sha256(wav).hexdigest()
        entry = entries.get(vm_id)
        if entry and entry.get("sha256") == digest and entry.get("transcript") and not force:
            continue
        seconds = wav_info(wav_path)[3]
        transcript = transcriber.transcribe(wav) if transcriber else None
        entries[vm_id] = {"case_id": case["id"], "recording": source.name, "wav": wav_path.name, "sha256": digest,
                          "seconds": round(seconds, 2), "transcript": transcript, "model": MODEL if transcriber else None}
        save_manifest(folder, manifest)
        if progress:
            progress(f"{vm_id}: {seconds:.1f} s" + (f" -> {transcript[:70]!r}" if transcript else ""))
    save_manifest(folder, manifest)
    return manifest


def missing(cases: list[dict], folder: str | Path, need: str) -> list[str]:
    """Voicemail IDs not ready for a model that gets them as "transcript" or "audio"."""
    entries = load_manifest(folder)["voicemails"]
    out = []
    for case in voicemail_cases(cases):
        entry = entries.get(case["voicemail"]["id"])
        if not entry or not (Path(folder) / entry["wav"]).exists() or (need == "transcript" and not entry.get("transcript")):
            out.append(case["voicemail"]["id"])
    return out


def fingerprint(cases: list[dict], folder: str | Path, need: str) -> str:
    entries = load_manifest(folder)["voicemails"]
    material = [[c["voicemail"]["id"], entries[c["voicemail"]["id"]]["sha256"],
                 entries[c["voicemail"]["id"]].get("transcript") if need == "transcript" else None]
                for c in voicemail_cases(cases)]
    return hashlib.sha256(json.dumps(material).encode("utf-8")).hexdigest()


def heard(case: dict, folder: str | Path, mode: str) -> dict | None:
    """What a model gets for this case's voicemail: {"transcript": ...} or {"audio": base64 WAV}."""
    vm = case.get("voicemail")
    if not vm:
        return None
    if mode == "script":
        return {"transcript": vm["script"]}
    entry = load_manifest(folder)["voicemails"].get(vm["id"])
    if not entry:
        raise VoicemailError(f"{vm['id']} hasn't been recorded and transcribed yet")
    if mode == "transcript":
        return {"transcript": entry["transcript"]}
    return {"audio": base64.b64encode((Path(folder) / entry["wav"]).read_bytes()).decode("ascii")}
