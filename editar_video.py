"""
Script de edición de video:
1. Elimina silencios, vacíos de sonido y ruidos externos
2. Agrega subtítulos (SRT) generados con Whisper
3. Agrega música de fondo
"""

import os
import sys
import subprocess
import json
import tempfile
import shutil
import numpy as np

# Use static ffmpeg binary
import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
FFPROBE = FFMPEG.replace("ffmpeg", "ffprobe")
if not os.path.exists(FFPROBE):
    # fallback: ffprobe sibling
    FFPROBE = os.path.join(os.path.dirname(FFMPEG), "ffprobe-linux-x86_64-v7.0.2")
    if not os.path.exists(FFPROBE):
        FFPROBE = "ffprobe"

def run(cmd, check=True):
    cmd[0] = FFMPEG if cmd[0] == "ffmpeg" else cmd[0]
    cmd[0] = FFPROBE if cmd[0] == "ffprobe" else cmd[0]
    print("  $", " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print("STDERR:", result.stderr[-500:])
        raise RuntimeError(f"Command failed: {cmd[0]}")
    return result

def extract_audio(input_video, output_wav):
    run(["ffmpeg", "-y", "-i", input_video, "-ac", "1", "-ar", "16000",
         "-vn", "-f", "wav", output_wav])

def detect_silences(wav_file, silence_thresh_db=-35, min_silence_ms=400):
    """Detect silence intervals using ffmpeg silencedetect filter."""
    result = subprocess.run(
        [FFMPEG, "-i", wav_file,
         "-af", f"silencedetect=noise={silence_thresh_db}dB:d={min_silence_ms/1000:.2f}",
         "-f", "null", "-"],
        capture_output=True, text=True
    )
    output = result.stderr
    silences = []
    start = None
    for line in output.splitlines():
        if "silence_start" in line:
            start = float(line.split("silence_start:")[1].strip().split()[0])
        elif "silence_end" in line and start is not None:
            end = float(line.split("silence_end:")[1].strip().split("|")[0].strip())
            silences.append((start, end))
            start = None
    return silences

def get_duration(input_file):
    result = subprocess.run(
        [FFMPEG, "-i", input_file, "-f", "null", "-"],
        capture_output=True, text=True
    )
    for line in result.stderr.splitlines():
        if "Duration" in line:
            t = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = t.split(":")
            return float(h)*3600 + float(m)*60 + float(s)
    return 0

def build_keep_segments(duration, silences, padding=0.05):
    """Invert silence list to get speech segments."""
    segments = []
    cursor = 0.0
    for s_start, s_end in silences:
        seg_end = s_start + padding
        if s_start - padding > cursor:
            segments.append((cursor, s_start + padding))
        cursor = max(cursor, s_end - padding)
    if cursor < duration - 0.1:
        segments.append((cursor, duration))
    return segments

def cut_video(input_video, segments, output_video):
    """Concatenate only the speech segments."""
    tmp_dir = tempfile.mkdtemp()
    parts = []
    for i, (start, end) in enumerate(segments):
        part = os.path.join(tmp_dir, f"part_{i:04d}.mp4")
        run(["ffmpeg", "-y", "-ss", str(start), "-to", str(end),
             "-i", input_video, "-c", "copy", part])
        parts.append(part)

    list_file = os.path.join(tmp_dir, "list.txt")
    with open(list_file, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")

    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
         "-c", "copy", output_video])
    shutil.rmtree(tmp_dir)

def transcribe_google(wav_file, output_srt, chunk_duration=30):
    """Transcribe audio with Google Speech Recognition in chunks and save SRT."""
    import speech_recognition as sr
    import wave, math

    recognizer = sr.Recognizer()

    # Get total duration
    with wave.open(wav_file, 'r') as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        total_dur = frames / float(rate)

    n_chunks = math.ceil(total_dur / chunk_duration)
    print(f"  Transcribiendo {n_chunks} fragmentos de {chunk_duration}s...")

    segments = []
    for i in range(n_chunks):
        start = i * chunk_duration
        end = min((i + 1) * chunk_duration, total_dur)
        tmp_chunk = f"/tmp/chunk_{i:04d}.wav"

        subprocess.run(
            [FFMPEG, "-y", "-ss", str(start), "-to", str(end), "-i", wav_file, tmp_chunk],
            capture_output=True
        )

        try:
            with sr.AudioFile(tmp_chunk) as source:
                audio = recognizer.record(source)
            text = recognizer.recognize_google(audio, language="es-ES")
            segments.append((start, end, text))
            print(f"  [{start:.0f}s-{end:.0f}s]: {text[:60]}...")
        except sr.UnknownValueError:
            print(f"  [{start:.0f}s-{end:.0f}s]: (sin voz detectada)")
        except Exception as e:
            print(f"  [{start:.0f}s-{end:.0f}s]: Error: {e}")
        finally:
            if os.path.exists(tmp_chunk):
                os.remove(tmp_chunk)

    def fmt(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(output_srt, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(segments, 1):
            f.write(f"{i}\n{fmt(start)} --> {fmt(end)}\n{text}\n\n")

    return " ".join(t for _, _, t in segments)

def generate_background_music(duration_sec, output_wav):
    """Generate a simple pleasant background music using sine waves."""
    sample_rate = 44100
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)

    # Chord progression: C major feel (C, Am, F, G)
    freqs_chords = [
        [261.63, 329.63, 392.00],  # C major
        [220.00, 261.63, 329.63],  # A minor
        [174.61, 220.00, 261.63],  # F major
        [196.00, 246.94, 293.66],  # G major
    ]

    chord_dur = duration_sec / len(freqs_chords)
    audio = np.zeros_like(t)

    for i, freqs in enumerate(freqs_chords):
        start = int(i * chord_dur * sample_rate)
        end = int((i + 1) * chord_dur * sample_rate)
        chunk = np.zeros(end - start)
        chunk_t = np.linspace(i * chord_dur, (i + 1) * chord_dur, end - start, endpoint=False)
        for freq in freqs:
            chunk += 0.15 * np.sin(2 * np.pi * freq * chunk_t)
            chunk += 0.05 * np.sin(2 * np.pi * freq * 2 * chunk_t)  # harmonic
        # Fade in/out per chord
        fade = min(int(0.3 * sample_rate), len(chunk) // 4)
        chunk[:fade] *= np.linspace(0, 1, fade)
        chunk[-fade:] *= np.linspace(1, 0, fade)
        audio[start:end] += chunk

    # Normalize
    audio = audio / (np.max(np.abs(audio)) + 1e-9) * 0.3

    # Save as WAV
    import wave, struct
    audio_int = (audio * 32767).astype(np.int16)
    with wave.open(output_wav, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int.tobytes())

def add_subtitles_and_music(input_video, srt_file, music_wav, output_video):
    """Burn subtitles and mix background music."""
    # First add subtitles (burned in)
    tmp_sub = output_video.replace(".mp4", "_sub.mp4")

    run(["ffmpeg", "-y", "-i", input_video,
         "-vf", f"subtitles={srt_file}:force_style='FontSize=20,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=2,Alignment=2'",
         "-c:a", "copy", tmp_sub])

    # Then mix background music
    run(["ffmpeg", "-y",
         "-i", tmp_sub,
         "-i", music_wav,
         "-filter_complex",
         "[0:a]volume=1.0[voice];[1:a]volume=0.25,aloop=loop=-1:size=2e+09[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
         "-map", "0:v",
         "-map", "[aout]",
         "-c:v", "copy",
         "-c:a", "aac",
         "-shortest",
         output_video])

    os.remove(tmp_sub)

def main(input_video):
    base = os.path.splitext(os.path.basename(input_video))[0]
    out_dir = "/home/user/edicion-de-video"
    output_final = os.path.join(out_dir, f"{base}_editado.mp4")

    tmp = tempfile.mkdtemp(prefix="video_edit_")
    print(f"\n=== Editando: {input_video} ===\n")

    try:
        # 1. Extract audio
        print("[1/5] Extrayendo audio...")
        wav_original = os.path.join(tmp, "original.wav")
        extract_audio(input_video, wav_original)

        # 2. Detect silences
        print("[2/5] Detectando silencios y ruidos...")
        duration = get_duration(input_video)
        print(f"  Duración original: {duration:.1f}s")
        silences = detect_silences(wav_original, silence_thresh_db=-25, min_silence_ms=300)
        print(f"  Silencios detectados: {len(silences)}")
        for s, e in silences:
            print(f"    [{s:.2f}s - {e:.2f}s]")

        # 3. Cut video removing silences
        print("[3/5] Eliminando silencios del video...")
        segments = build_keep_segments(duration, silences)
        print(f"  Segmentos a conservar: {len(segments)}")
        cut_video_path = os.path.join(tmp, "cut.mp4")
        if len(segments) > 0 and len(silences) > 0:
            cut_video(input_video, segments, cut_video_path)
        else:
            shutil.copy(input_video, cut_video_path)
            print("  No se encontraron silencios significativos, conservando video original.")

        # 4. Transcribe and generate subtitles
        print("[4/5] Generando subtítulos...")
        cut_wav = os.path.join(tmp, "cut.wav")
        extract_audio(cut_video_path, cut_wav)
        srt_file = os.path.join(out_dir, f"{base}_subtitulos.srt")
        has_subtitles = False
        try:
            text = transcribe_google(cut_wav, srt_file)
            if text.strip():
                has_subtitles = True
                print(f"  Subtítulos guardados: {srt_file}")
            else:
                print("  Advertencia: no se pudo transcribir (APIs externas no disponibles).")
        except Exception as e:
            print(f"  Advertencia: transcripción fallida ({e}). El video se exportará sin subtítulos.")

        # 5. Generate background music and mix
        print("[5/5] Generando música de fondo y exportando...")
        cut_duration = get_duration(cut_video_path)
        music_wav = os.path.join(tmp, "music.wav")
        generate_background_music(cut_duration + 5, music_wav)

        if has_subtitles:
            add_subtitles_and_music(cut_video_path, srt_file, music_wav, output_final)
        else:
            # Mix music only, no subtitles
            run(["ffmpeg", "-y",
                 "-i", cut_video_path,
                 "-i", music_wav,
                 "-filter_complex",
                 "[0:a]volume=1.0[voice];[1:a]volume=0.25,aloop=loop=-1:size=2e+09[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                 "-map", "0:v",
                 "-map", "[aout]",
                 "-c:v", "copy",
                 "-c:a", "aac",
                 "-shortest",
                 output_final])

        print(f"\n✓ Video editado guardado en: {output_final}")
        if has_subtitles:
            print(f"✓ Subtítulos SRT guardados en: {srt_file}")
        else:
            print("! Subtítulos: no disponibles (requiere acceso a internet para descargar modelo de IA)")
        return output_final

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    video = sys.argv[1] if len(sys.argv) > 1 else "/root/.claude/uploads/a714719c-737b-5e88-833a-3941880c953f/d2aca575-whatsappvideo20260624at120947_Yo6CcxpF.mp4"
    main(video)
