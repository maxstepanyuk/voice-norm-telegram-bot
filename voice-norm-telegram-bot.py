import asyncio
import json
import logging
import math
import os
import re
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

TARGET_LUFS = -16.0
CEILING_DB = -1.0          # limiter ceiling in dBFS
TOLERANCE_LU = 0.3         # stop iterating when within this of target
MAX_ITERATIONS = 4
MAX_GAIN_DB = 40.0         # safety cap for very quiet recordings

COMP_THRESHOLD = 0.05      # linear amplitude, ~ -26 dBFS
COMP_RATIO = 4
COMP_ATTACK_MS = 10
COMP_RELEASE_MS = 150

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voicenorm")


async def run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


async def measure_lufs(path: Path) -> float | None:
    """Integrated loudness via ffmpeg's loudnorm analysis pass."""
    code, out = await run(
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    )
    if code != 0:
        raise RuntimeError(f"measure failed:\n{out[-500:]}")
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out, re.S)
    if not match:
        return None
    value = float(json.loads(match.group(0))["input_i"])
    return value if math.isfinite(value) else None


def limiter_filter(gain_db: float) -> str:
    ceiling = 10 ** (CEILING_DB / 20)
    # level=disabled stops alimiter from re-normalizing the output level
    return (
        "highpass=f=80,"
        f"acompressor=threshold={COMP_THRESHOLD:.4f}:ratio={COMP_RATIO}"
        f":attack={COMP_ATTACK_MS}:release={COMP_RELEASE_MS}:knee=3:makeup=1,"
        f"volume={gain_db:.2f}dB,"
        f"alimiter=limit={ceiling:.4f}:attack=5:release=50:level=disabled"
    )


async def render(src: Path, dst: Path, gain_db: float) -> None:
    code, out = await run(
        "ffmpeg", "-hide_banner", "-nostats", "-y", "-i", str(src),
        "-af", limiter_filter(gain_db),
        "-ac", "1", "-ar", "48000",
        "-c:a", "libopus", "-b:a", "48k", "-application", "voip",
        str(dst),
    )
    if code != 0:
        raise RuntimeError(f"render failed:\n{out[-500:]}")


async def normalize(src: Path, dst: Path) -> str:
    original = await measure_lufs(src)
    if original is None:
        raise RuntimeError("silent audio, nothing to normalize")

    gain = TARGET_LUFS - original
    gain = max(min(gain, MAX_GAIN_DB), -MAX_GAIN_DB)
    result = original

    for i in range(MAX_ITERATIONS):
        await render(src, dst, gain)
        measured = await measure_lufs(dst)
        if measured is None:
            break
        result = measured
        error = TARGET_LUFS - result
        log.info("iter %d: gain %.2f dB -> %.2f LUFS", i + 1, gain, result)
        if abs(error) <= TOLERANCE_LU:
            break
        gain = max(min(gain + error, MAX_GAIN_DB), -MAX_GAIN_DB)

    return f"{original:.1f} -> {result:.1f} LUFS"


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.voice:
        return

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.ogg"
        dst = Path(tmp) / "out.ogg"
        try:
            tg_file = await msg.voice.get_file()   # bots can download up to 20 MB
            await tg_file.download_to_drive(src)
            summary = await normalize(src, dst)
            with dst.open("rb") as f:
                thread_id = msg.message_thread_id if msg.is_topic_message else None
                await msg.reply_voice(
                    voice=f,
                    caption=summary,
                    duration=msg.voice.duration,
                    message_thread_id=thread_id,
                    disable_notification=True,
                )
        except Exception:
            log.exception("failed to process voice message")


def main() -> None:
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()
    # Only brand-new messages
    app.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & filters.VOICE, handle_voice)
    )
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
