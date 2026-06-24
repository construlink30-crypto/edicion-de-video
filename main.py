import os
import re
import subprocess
import uuid
from pathlib import Path

import imageio_ffmpeg
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
os.environ["PATH"] = str(Path(FFMPEG).parent) + os.pathsep + os.environ.get("PATH", "")

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI()
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")


# ── helpers ──────────────────────────────────────────────────────────────────

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


# ── 1. noise reduction (audio only, video copied) ────────────────────────────

def reduce_noise(src: str, dst: str) -> tuple[bool, str]:
    ok, err = run_cmd([
        FFMPEG, "-y", "-i", src,
        "-af", "afftdn=nf=-25",
        "-c:v", "copy",
        dst,
    ])
    return ok, err


# ── 2. silence removal (cuts video+audio in sync) ────────────────────────────

def detect_speech_segments(path: str, noise_db: int = -35, min_silence: float = 0.4) -> list:
    """Returns list of (start, end) tuples of speech (non-silent) segments."""
    _, stderr = run_cmd([
        FFMPEG, "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ])
    starts = [float(x) for x in re.findall(r"silence_start:\s*(\d+\.?\d*)", stderr)]
    ends   = [float(x) for x in re.findall(r"silence_end:\s*(\d+\.?\d*)", stderr)]

    duration = get_duration(path)
    segments = []
    prev = 0.0
    for s, e in zip(starts, ends):
        if s - prev > 0.25:
            segments.append((round(prev, 3), round(s, 3)))
        prev = e
    if duration - prev > 0.25:
        segments.append((round(prev, 3), round(duration, 3)))
    return segments


def remove_silence(src: str, dst: str) -> tuple[bool, str]:
    segments = detect_speech_segments(src)
    if not segments:
        # nothing to cut — just copy
        ok, err = run_cmd([FFMPEG, "-y", "-i", src, "-c", "copy", dst])
        return ok, err

    n = len(segments)
    parts = []
    for i, (s, e) in enumerate(segments):
        parts.append(f"[0:v]trim={s}:{e},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim={s}:{e},asetpts=PTS-STARTPTS[a{i}]")

    v_inputs = "".join(f"[v{i}]" for i in range(n))
    a_inputs = "".join(f"[a{i}]" for i in range(n))
    parts.append(f"{v_inputs}concat=n={n}:v=1:a=0[vout]")
    parts.append(f"{a_inputs}concat=n={n}:v=0:a=1[aout]")

    ok, err = run_cmd([
        FFMPEG, "-y", "-i", src,
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "aac",
        dst,
    ])
    return ok, err


# ── 3. subtitles via whisper + drawtext (no libass needed) ───────────────────

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

    drawtext_list = []
    for seg in segments:
        text = (seg["text"].strip()
                .replace("\\", "")
                .replace("'", "’")   # curly apostrophe to avoid ffmpeg escape issues
                .replace(":", " "))
        if not text:
            continue
        s = round(seg["start"], 2)
        e = round(seg["end"], 2)
        drawtext_list.append(
            f"drawtext=text='{text}'"
            f":enable='between(t,{s},{e})'"
            f":fontcolor=white:fontsize=22"
            f":borderw=2:bordercolor=black"
            f":x=(w-text_w)/2:y=h-th-50"
        )

    if not drawtext_list:
        ok, err = run_cmd([FFMPEG, "-y", "-i", src, "-c", "copy", dst])
        return ok, err

    ok, err = run_cmd([
        FFMPEG, "-y", "-i", src,
        "-vf", ",".join(drawtext_list),
        "-c:a", "copy",
        dst,
    ])
    return ok, err


# ── 4. background music (optional file, loops to fit video) ──────────────────

def add_music(src: str, music: str, dst: str, vol: float = 0.12) -> tuple[bool, str]:
    ok, err = run_cmd([
        FFMPEG, "-y",
        "-i", src,
        "-stream_loop", "-1", "-i", music,
        "-filter_complex",
        f"[0:a]volume=1.0[v];[1:a]volume={vol}[m];[v][m]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-shortest", "-c:v", "copy", "-c:a", "aac",
        dst,
    ])
    return ok, err


# ── prompt parser ─────────────────────────────────────────────────────────────

def parse_prompt(prompt: str) -> dict:
    p = prompt.lower()
    return {
        "ruido":      any(w in p for w in ["ruido", "ruidos", "fondo", "noise", "externo", "limpi"]),
        "silencio":   any(w in p for w in ["silencio", "pausa", "vacio", "vacío", "traba", "silence", "espacio"]),
        "subtitulos": any(w in p for w in ["subtitulo", "subtítulo", "subtitle", "transcri", "caption", "texto"]),
        "musica":     any(w in p for w in ["musica", "música", "cancion", "canción", "song", "fondo musical"]),
    }


# ── FastAPI endpoints ─────────────────────────────────────────────────────────

@app.get("/diagnostico")
async def diagnostico():
    results = {}

    # ffmpeg path
    results["ffmpeg"] = FFMPEG

    # codecs disponibles
    _, stderr = run_cmd([FFMPEG, "-codecs"])
    results["libx264"] = "libx264" in stderr
    results["aac"]     = "aac" in stderr

    # whisper
    try:
        import whisper
        results["whisper"] = True
    except Exception as e:
        results["whisper"] = str(e)

    # test simple ffmpeg (genera 3s de video negro)
    test_in  = str(UPLOAD_DIR / "test_in.mp4")
    test_out = str(UPLOAD_DIR / "test_out.mp4")
    ok, err = run_cmd([FFMPEG, "-y", "-f", "lavfi", "-i", "color=black:s=320x240:d=3",
                       "-f", "lavfi", "-i", "sine=frequency=440:d=3",
                       "-c:v", "libx264", "-c:a", "aac", test_in])
    results["ffmpeg_generate_test"] = ok
    if not ok:
        # try without libx264
        ok2, err2 = run_cmd([FFMPEG, "-y", "-f", "lavfi", "-i", "color=black:s=320x240:d=3",
                             "-f", "lavfi", "-i", "sine=frequency=440:d=3",
                             test_in])
        results["ffmpeg_generate_fallback"] = ok2
        results["ffmpeg_error"] = err[-500:]

    if Path(test_in).exists():
        # test silence detection
        segs = detect_speech_segments(test_in)
        results["silence_detection"] = segs

        # test noise reduction
        ok3, err3 = reduce_noise(test_in, test_out)
        results["noise_reduction"] = ok3
        if not ok3:
            results["noise_error"] = err3[-500:]

    return JSONResponse(results)


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
    # Merge prompt-detected options with checkbox options
    detected = parse_prompt(prompt) if prompt.strip() else {}
    do_ruido      = reducir_ruido.lower() == "true"      or detected.get("ruido", False)
    do_silencio   = eliminar_silencios.lower() == "true" or detected.get("silencio", False)
    do_subtitulos = agregar_subtitulos.lower() == "true" or detected.get("subtitulos", False)
    do_musica     = agregar_musica.lower() == "true"     or detected.get("musica", False)

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
        nonlocal step
        step += 1
        return str(UPLOAD_DIR / f"{uid}_s{step}_{label}.mp4")

    # 1. noise reduction
    if do_ruido:
        out = nxt("noise")
        ok, err = reduce_noise(current, out)
        if ok:
            current = out
            logs.append("✓ Ruido de fondo reducido")
        else:
            logs.append(f"✗ Error reduciendo ruido: {err[:300]}")

    # 2. silence removal (cuts both a/v in sync)
    if do_silencio:
        out = nxt("silence")
        ok, err = remove_silence(current, out)
        if ok:
            current = out
            logs.append("✓ Silencios eliminados (video y audio sincronizados)")
        else:
            logs.append(f"✗ Error eliminando silencios: {err[:300]}")

    # 3. subtitles
    sub_segments = []
    if do_subtitulos:
        logs.append("⏳ Transcribiendo audio con Whisper...")
        ok, sub_segments, err = transcribe(current)
        if ok and sub_segments:
            out = nxt("subs")
            ok2, err2 = burn_subtitles(current, sub_segments, out)
            if ok2:
                current = out
                logs.append(f"✓ Subtítulos generados ({len(sub_segments)} segmentos)")
            else:
                logs.append(f"✗ Error quemando subtítulos: {err2[:300]}")
        else:
            logs.append(f"✗ Error en transcripción: {err[:300]}")

    # 4. background music
    if do_musica:
        if music_path:
            out = nxt("music")
            ok, err = add_music(current, music_path, out)
            if ok:
                current = out
                logs.append("✓ Música de fondo agregada")
            else:
                logs.append(f"✗ Error con música: {err[:300]}")
        else:
            logs.append("⚠ Música omitida — no se subió ningún archivo de audio")

    # final copy to output
    final = str(OUTPUT_DIR / f"{uid}_final.mp4")
    ok, err = run_cmd([FFMPEG, "-y", "-i", current, "-c", "copy", final])
    if not ok:
        ok, err = run_cmd([FFMPEG, "-y", "-i", current, final])
    if not ok or not Path(final).exists():
        return JSONResponse({"error": err, "logs": logs})

    return JSONResponse({"url": f"/outputs/{Path(final).name}", "logs": logs})
