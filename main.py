import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import imageio_ffmpeg
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# Make ffmpeg available on PATH for whisper
os.environ["PATH"] = str(Path(FFMPEG).parent) + os.pathsep + os.environ.get("PATH", "")

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI()
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")


def get_duration(path: str) -> float:
    result = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.?\d*)", result.stderr)
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0.0


def run_ffmpeg(cmd: list, label: str = "") -> tuple[bool, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr[-2000:]
    return True, ""


def remove_silence(input_path: str, output_path: str) -> tuple[bool, str]:
    cmd = [
        FFMPEG, "-y", "-i", input_path,
        "-af", "silenceremove=start_periods=1:start_duration=0.3:start_threshold=-35dB:stop_periods=-1:stop_duration=0.5:stop_threshold=-35dB",
        "-c:v", "copy",
        output_path,
    ]
    return run_ffmpeg(cmd, "silence removal")


def reduce_noise(input_path: str, output_path: str) -> tuple[bool, str]:
    cmd = [
        FFMPEG, "-y", "-i", input_path,
        "-af", "afftdn=nf=-25",
        "-c:v", "copy",
        output_path,
    ]
    return run_ffmpeg(cmd, "noise reduction")


def generate_subtitles(input_path: str, srt_path: str) -> tuple[bool, str]:
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(input_path, word_timestamps=False)
        segments = result.get("segments", [])

        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, 1):
                start = format_srt_time(seg["start"])
                end = format_srt_time(seg["end"])
                text = seg["text"].strip()
                f.write(f"{i}\n{start} --> {end}\n{text}\n\n")

        return True, ""
    except ImportError:
        return False, "whisper no instalado. Corre: pip install openai-whisper"
    except Exception as e:
        return False, str(e)


def format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def burn_subtitles(input_path: str, srt_path: str, output_path: str) -> tuple[bool, str]:
    # Escape path for ffmpeg subtitles filter (Windows backslashes)
    safe_srt = srt_path.replace("\\", "/").replace(":", "\\:")
    cmd = [
        FFMPEG, "-y", "-i", input_path,
        "-vf", f"subtitles='{safe_srt}':force_style='FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Alignment=2'",
        "-c:a", "copy",
        output_path,
    ]
    return run_ffmpeg(cmd, "burn subtitles")


def add_background_music(input_path: str, music_path: str, output_path: str, music_volume: float = 0.15) -> tuple[bool, str]:
    cmd = [
        FFMPEG, "-y",
        "-i", input_path,
        "-stream_loop", "-1", "-i", music_path,
        "-filter_complex",
        f"[0:a]volume=1.0[voice];[1:a]volume={music_volume}[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-shortest",
        "-c:v", "copy", "-c:a", "aac",
        output_path,
    ]
    return run_ffmpeg(cmd, "background music")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(open("index.html", encoding="utf-8").read())


@app.post("/procesar")
async def procesar(
    video: UploadFile = File(...),
    music: UploadFile = File(None),
    eliminar_silencios: str = Form("false"),
    reducir_ruido: str = Form("false"),
    agregar_subtitulos: str = Form("false"),
    agregar_musica: str = Form("false"),
):
    uid = uuid.uuid4().hex
    ext = Path(video.filename).suffix or ".mp4"

    input_path = str(UPLOAD_DIR / f"{uid}_input{ext}")
    with open(input_path, "wb") as f:
        f.write(await video.read())

    music_path = None
    if music and music.filename:
        mext = Path(music.filename).suffix or ".mp3"
        music_path = str(UPLOAD_DIR / f"{uid}_music{mext}")
        with open(music_path, "wb") as f:
            f.write(await music.read())

    current = input_path
    step = 0
    logs = []

    def next_path(label):
        nonlocal step
        step += 1
        return str(UPLOAD_DIR / f"{uid}_step{step}_{label}.mp4")

    # 1. Reduce noise
    if reducir_ruido.lower() == "true":
        out = next_path("noise")
        ok, err = reduce_noise(current, out)
        if ok:
            current = out
            logs.append("Ruido reducido")
        else:
            logs.append(f"Error reduciendo ruido: {err[:200]}")

    # 2. Remove silence
    if eliminar_silencios.lower() == "true":
        out = next_path("silence")
        ok, err = remove_silence(current, out)
        if ok:
            current = out
            logs.append("Silencios eliminados")
        else:
            logs.append(f"Error eliminando silencios: {err[:200]}")

    # 3. Generate and burn subtitles
    if agregar_subtitulos.lower() == "true":
        srt_path = str(UPLOAD_DIR / f"{uid}.srt")
        ok, err = generate_subtitles(current, srt_path)
        if ok:
            out = next_path("subtitles")
            ok2, err2 = burn_subtitles(current, srt_path, out)
            if ok2:
                current = out
                logs.append("Subtitulos generados y quemados")
            else:
                logs.append(f"Error quemando subtitulos: {err2[:200]}")
        else:
            logs.append(f"Error generando subtitulos: {err[:200]}")

    # 4. Add background music
    if agregar_musica.lower() == "true" and music_path:
        out = next_path("music")
        ok, err = add_background_music(current, music_path, out)
        if ok:
            current = out
            logs.append("Musica de fondo agregada")
        else:
            logs.append(f"Error agregando musica: {err[:200]}")

    # Copy final result to output dir
    final_output = str(OUTPUT_DIR / f"{uid}_final.mp4")
    cmd = [FFMPEG, "-y", "-i", current, "-c", "copy", final_output]
    ok, err = run_ffmpeg(cmd)
    if not ok:
        # try re-encode as fallback
        cmd2 = [FFMPEG, "-y", "-i", current, final_output]
        ok, err = run_ffmpeg(cmd2)

    if not ok or not Path(final_output).exists():
        return JSONResponse({"error": err, "logs": logs})

    return JSONResponse({
        "url": f"/outputs/{Path(final_output).name}",
        "logs": logs,
    })
