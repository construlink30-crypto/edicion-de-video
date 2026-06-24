import os
import re
import subprocess
import uuid
from pathlib import Path

import imageio_ffmpeg
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
os.environ["PATH"] = str(Path(FFMPEG).parent) + os.pathsep + os.environ.get("PATH", "")

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI()
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")


# ── helpers ───────────────────────────────────────────────────────────────────

def run_cmd(cmd: list) -> tuple[bool, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, r.stderr[-3000:]


def get_duration(path: str) -> float:
    _, stderr = run_cmd([FFMPEG, "-i", path])
    m = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.?\d*)", stderr)
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0.0


def safe_path(p: str) -> str:
    """Forward slashes for ffmpeg on all platforms."""
    return p.replace("\\", "/")


# ── 1. noise reduction ────────────────────────────────────────────────────────

def reduce_noise(src: str, dst: str) -> tuple[bool, str]:
    ok, err = run_cmd([
        FFMPEG, "-y", "-i", src,
        "-af", "afftdn=nf=-25",
        "-c:v", "copy", dst,
    ])
    return ok, err


# ── 2. silence removal via concat demuxer (no re-encode, Windows-safe) ───────

def detect_speech_segments(path: str) -> tuple[list, list]:
    """
    Returns (speech_segments, silence_segments).
    Uses two passes with different thresholds to catch both
    dead silence and near-silence (trabas, ruido bajo).
    """
    duration = get_duration(path)
    all_silences = []

    # Pass 1: strict — catches dead silence and gaps
    for noise_db, min_dur in [("-30dB", 0.3), ("-20dB", 0.5)]:
        _, stderr = run_cmd([
            FFMPEG, "-i", path,
            "-af", f"silencedetect=noise={noise_db}:d={min_dur}",
            "-f", "null", "-",
        ])
        starts = [float(x) for x in re.findall(r"silence_start:\s*(\d+\.?\d*)", stderr)]
        ends   = [float(x) for x in re.findall(r"silence_end:\s*(\d+\.?\d*)", stderr)]
        for s, e in zip(starts, ends):
            all_silences.append((round(s, 3), round(e, 3)))

    if not all_silences:
        return [], []

    # Merge overlapping silence ranges
    all_silences.sort()
    merged = [all_silences[0]]
    for s, e in all_silences[1:]:
        if s <= merged[-1][1] + 0.1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    # Build speech segments (gaps between silences)
    speech = []
    prev = 0.0
    for s, e in merged:
        if s - prev > 0.2:
            speech.append((round(prev, 3), round(s, 3)))
        prev = e
    if duration - prev > 0.2:
        speech.append((round(prev, 3), round(duration, 3)))

    return speech, merged


def remove_silence(src: str, dst: str) -> tuple[bool, str, int]:
    speech_segs, silence_segs = detect_speech_segments(src)

    if not speech_segs or not silence_segs:
        ok, err = run_cmd([FFMPEG, "-y", "-i", src, "-c", "copy", dst])
        return ok, "No se detectaron silencios en el video", 0

    # Write concat list file — no re-encoding needed
    concat_file = src + "_segs.txt"
    src_abs = safe_path(str(Path(src).resolve()))
    with open(concat_file, "w", encoding="utf-8") as f:
        for start, end in speech_segs:
            f.write(f"file '{src_abs}'\n")
            f.write(f"inpoint {start}\n")
            f.write(f"outpoint {end}\n")

    ok, err = run_cmd([
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        dst,
    ])
    return ok, err, len(silence_segs)


# ── 3. subtitles: whisper transcription + drawtext burn ───────────────────────

def transcribe(path: str) -> tuple[bool, list, str]:
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(path, word_timestamps=False)
        return True, result.get("segments", []), ""
    except Exception as e:
        return False, [], str(e)


def burn_subtitles(src: str, segments: list, dst: str) -> tuple[bool, str]:
    if not segments:
        ok, err = run_cmd([FFMPEG, "-y", "-i", src, "-c", "copy", dst])
        return ok, err

    filters = []
    for seg in segments:
        text = (seg["text"].strip()
                .replace("'", "’")
                .replace("\"", "")
                .replace("\\", "")
                .replace(":", " "))
        if not text:
            continue
        s = round(seg["start"], 2)
        e = round(seg["end"], 2)
        # Escape special chars for ffmpeg drawtext
        text_esc = text.replace("%", "\\%").replace("[", "\\[").replace("]", "\\]")
        filters.append(
            f"drawtext=text='{text_esc}'"
            f":enable='between(t,{s},{e})'"
            f":fontcolor=white:fontsize=22"
            f":borderw=2:bordercolor=black"
            f":x=(w-text_w)/2:y=h-th-50"
        )

    if not filters:
        ok, err = run_cmd([FFMPEG, "-y", "-i", src, "-c", "copy", dst])
        return ok, err

    ok, err = run_cmd([
        FFMPEG, "-y", "-i", src,
        "-vf", ",".join(filters),
        "-c:a", "copy", dst,
    ])
    return ok, err


# ── 4. background music ───────────────────────────────────────────────────────

def add_music(src: str, music: str, dst: str, vol: float = 0.12) -> tuple[bool, str]:
    ok, err = run_cmd([
        FFMPEG, "-y",
        "-i", src,
        "-stream_loop", "-1", "-i", music,
        "-filter_complex",
        f"[0:a]volume=1.0[v];[1:a]volume={vol}[m];"
        f"[v][m]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-shortest", "-c:v", "copy", "-c:a", "aac", dst,
    ])
    return ok, err


# ── prompt parser ─────────────────────────────────────────────────────────────

def parse_prompt(prompt: str) -> dict:
    p = prompt.lower()
    return {
        "ruido":      any(w in p for w in ["ruido", "ruidos", "fondo", "noise", "externo", "limpi", "estatica"]),
        "silencio":   any(w in p for w in ["silencio", "pausa", "vacio", "vacío", "traba", "silence", "espacio", "voz", "elimina"]),
        "subtitulos": any(w in p for w in ["subtitulo", "subtítulo", "subtitle", "transcri", "caption", "texto"]),
        "musica":     any(w in p for w in ["musica", "música", "cancion", "canción", "song", "musical"]),
    }


# ── diagnostico ───────────────────────────────────────────────────────────────

@app.get("/diagnostico")
async def diagnostico():
    info = {"ffmpeg": FFMPEG}
    _, stderr = run_cmd([FFMPEG, "-codecs"])
    info["libx264"] = "libx264" in stderr
    info["aac"] = "aac" in stderr
    try:
        import whisper; info["whisper"] = True
    except Exception as e:
        info["whisper"] = str(e)

    # generate test video
    test_in = str(UPLOAD_DIR / "diag_in.mp4")
    ok, err = run_cmd([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "color=black:s=320x240:d=5",
        "-f", "lavfi", "-i", "sine=frequency=440:d=5",
        "-c:a", "aac", test_in
    ])
    info["test_video_created"] = ok
    if not ok:
        info["test_error"] = err[-300:]

    if Path(test_in).exists():
        segs, sils = detect_speech_segments(test_in)
        info["speech_segments_found"] = len(segs)
        info["silence_segments_found"] = len(sils)

        ok2, err2 = run_cmd([FFMPEG, "-y", "-i", test_in,
                             "-af", "afftdn=nf=-25", "-c:v", "copy",
                             str(UPLOAD_DIR / "diag_noise.mp4")])
        info["noise_reduction_works"] = ok2
        if not ok2:
            info["noise_error"] = err2[-300:]

    return JSONResponse(info)


# ── main endpoint ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(open("index.html", encoding="utf-8").read())


@app.post("/procesar")
async def procesar(
    video: UploadFile = File(...),
    music: UploadFile = File(None),
    prompt: str = Form(""),
    eliminar_silencios: str = Form("false"),
    reducir_ruido: str = Form("false"),
    agregar_subtitulos: str = Form("false"),
    agregar_musica: str = Form("false"),
):
    detected = parse_prompt(prompt) if prompt.strip() else {}
    do_ruido      = reducir_ruido.lower()      == "true" or detected.get("ruido", False)
    do_silencio   = eliminar_silencios.lower() == "true" or detected.get("silencio", False)
    do_subtitulos = agregar_subtitulos.lower() == "true" or detected.get("subtitulos", False)
    do_musica     = agregar_musica.lower()     == "true" or detected.get("musica", False)

    uid = uuid.uuid4().hex
    ext = Path(video.filename).suffix or ".mp4"
    current = str(UPLOAD_DIR / f"{uid}_input{ext}")
    with open(current, "wb") as f:
        f.write(await video.read())

    music_path = None
    if music and music.filename:
        mext = Path(music.filename).suffix or ".mp3"
        music_path = str(UPLOAD_DIR / f"{uid}_music{mext}")
        with open(music_path, "wb") as f:
            f.write(await music.read())

    logs = []
    step = 0

    def nxt(label):
        nonlocal step; step += 1
        return str(UPLOAD_DIR / f"{uid}_s{step}_{label}.mp4")

    dur_before = get_duration(current)
    logs.append(f"📹 Video cargado — duración original: {dur_before:.1f}s")

    # 1. noise reduction
    if do_ruido:
        out = nxt("noise")
        ok, err = reduce_noise(current, out)
        if ok:
            current = out
            logs.append("✓ Ruido de fondo reducido (afftdn)")
        else:
            logs.append(f"✗ Error ruido: {err[:200]}")

    # 2. silence removal
    if do_silencio:
        out = nxt("silence")
        ok, err, n_sil = remove_silence(current, out)
        if ok:
            current = out
            dur_after = get_duration(current)
            removed = round(dur_before - dur_after, 1)
            logs.append(f"✓ {n_sil} silencios detectados — {removed}s eliminados — nueva duración: {dur_after:.1f}s")
        else:
            logs.append(f"✗ Error silencios: {err[:200]}")

    # 3. subtitles
    if do_subtitulos:
        logs.append("⏳ Transcribiendo con Whisper (puede tardar 1–3 min)…")
        ok, sub_segs, err = transcribe(current)
        if ok and sub_segs:
            out = nxt("subs")
            ok2, err2 = burn_subtitles(current, sub_segs, out)
            if ok2:
                current = out
                logs.append(f"✓ {len(sub_segs)} subtítulos generados y quemados")
            else:
                logs.append(f"✗ Error subtítulos: {err2[:200]}")
        else:
            logs.append(f"✗ Error transcripción: {err[:200]}")

    # 4. music
    if do_musica:
        if music_path:
            out = nxt("music")
            ok, err = add_music(current, music_path, out)
            if ok:
                current = out
                logs.append("✓ Música de fondo mezclada")
            else:
                logs.append(f"✗ Error música: {err[:200]}")
        else:
            logs.append("⚠ Música omitida — no se subió archivo de audio")

    # final output
    final = str(OUTPUT_DIR / f"{uid}_final.mp4")
    ok, err = run_cmd([FFMPEG, "-y", "-i", current, "-c", "copy", final])
    if not ok:
        ok, err = run_cmd([FFMPEG, "-y", "-i", current, final])
    if not ok or not Path(final).exists():
        return JSONResponse({"error": err, "logs": logs})

    dur_final = get_duration(final)
    logs.append(f"✅ Duración final: {dur_final:.1f}s")
    return JSONResponse({"url": f"/outputs/{Path(final).name}", "logs": logs})
