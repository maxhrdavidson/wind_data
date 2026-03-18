#!/usr/bin/env python3
"""
Email-to-Podcast: Fetches Heatmap daily briefing from Gmail,
converts it to audio via OpenAI TTS, and sends it via Telegram.
"""

import imaplib
import email
import os
import re
import sys
import logging
from datetime import date
from email.header import decode_header
from pathlib import Path

import openai
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config (all values come from environment variables / GitHub Secrets)
# ---------------------------------------------------------------------------

GMAIL_USER = os.environ["GMAIL_USER"]          # your.email@gmail.com
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]  # 16-char Google app password

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Sender filter – adjust if Heatmap uses a different from address
HEATMAP_SENDER = os.getenv("HEATMAP_SENDER", "heatmap")

# TTS settings
TTS_MODEL = os.getenv("TTS_MODEL", "tts-1-hd")   # tts-1 (faster/cheaper) or tts-1-hd
TTS_VOICE = os.getenv("TTS_VOICE", "alloy")       # alloy, echo, fable, onyx, nova, shimmer

# Max characters sent to TTS (OpenAI limit is 4096 per request; we chunk if needed)
TTS_CHUNK_SIZE = 4000


# ---------------------------------------------------------------------------
# Step 1: Fetch email
# ---------------------------------------------------------------------------

def fetch_heatmap_email() -> tuple[str, str]:
    """Return (subject, plain-text body) of today's Heatmap briefing."""
    log.info("Connecting to Gmail IMAP…")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select("inbox")

    today = date.today().strftime("%d-%b-%Y")  # e.g. 18-Mar-2026
    search_query = f'(FROM "{HEATMAP_SENDER}" SINCE {today})'
    log.info("Searching: %s", search_query)

    _, message_ids = mail.search(None, search_query)
    ids = message_ids[0].split()

    if not ids:
        raise RuntimeError(
            f"No email from '{HEATMAP_SENDER}' found today ({today}). "
            "Check HEATMAP_SENDER env var or run again later."
        )

    # Take the most recent match
    _, msg_data = mail.fetch(ids[-1], "(RFC822)")
    mail.logout()

    raw = msg_data[0][1]
    msg = email.message_from_bytes(raw)

    subject = _decode_header(msg["Subject"])
    body = _extract_body(msg)
    log.info("Fetched: %s (%d chars)", subject, len(body))
    return subject, body


def _decode_header(value: str) -> str:
    parts = decode_header(value or "")
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _extract_body(msg) -> str:
    """Extract readable plain text from a (possibly multipart) email."""
    text_parts = []
    html_parts = []

    for part in msg.walk():
        ct = part.get_content_type()
        disp = str(part.get("Content-Disposition", ""))
        if "attachment" in disp:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")

        if ct == "text/plain":
            text_parts.append(text)
        elif ct == "text/html":
            html_parts.append(text)

    if text_parts:
        return _clean_text("\n".join(text_parts))

    # Fall back to stripping HTML
    combined_html = "\n".join(html_parts)
    soup = BeautifulSoup(combined_html, "html.parser")
    return _clean_text(soup.get_text(separator="\n"))


def _clean_text(text: str) -> str:
    """Remove excessive whitespace and unsubscribe footers."""
    # Strip lines that look like unsubscribe / legal boilerplate
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in ["unsubscribe", "manage preferences", "view in browser"]):
            break
        cleaned.append(line)

    text = "\n".join(cleaned)
    # Collapse 3+ blank lines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Step 2: Text → Audio
# ---------------------------------------------------------------------------

def text_to_audio(text: str, output_path: Path) -> Path:
    """Convert text to an MP3 file using OpenAI TTS, chunking if needed."""
    client = openai.OpenAI(api_key=OPENAI_API_KEY)

    chunks = _split_text(text, TTS_CHUNK_SIZE)
    log.info("Converting %d chunk(s) to audio with model=%s voice=%s…", len(chunks), TTS_MODEL, TTS_VOICE)

    audio_segments = []
    for i, chunk in enumerate(chunks):
        log.info("  Chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
        response = client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=chunk,
            response_format="mp3",
        )
        audio_segments.append(response.content)

    # Concatenate MP3 bytes (valid for concatenating MPEG frames)
    with open(output_path, "wb") as f:
        for segment in audio_segments:
            f.write(segment)

    size_kb = output_path.stat().st_size // 1024
    log.info("Audio saved: %s (%d KB)", output_path, size_kb)
    return output_path


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks at sentence boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks, current = [], []
    current_len = 0

    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if current_len + len(sentence) > max_chars and current:
            chunks.append(" ".join(current))
            current, current_len = [], 0
        current.append(sentence)
        current_len += len(sentence) + 1

    if current:
        chunks.append(" ".join(current))
    return chunks


# ---------------------------------------------------------------------------
# Step 3: Deliver via Telegram
# ---------------------------------------------------------------------------

def send_telegram_audio(audio_path: Path, caption: str) -> None:
    """Send audio file to Telegram chat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendAudio"
    log.info("Sending to Telegram chat %s…", TELEGRAM_CHAT_ID)

    with open(audio_path, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
            files={"audio": (audio_path.name, f, "audio/mpeg")},
            timeout=120,
        )

    if not resp.ok:
        raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text}")
    log.info("Delivered successfully.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    subject, body = fetch_heatmap_email()

    output_dir = Path("/tmp")
    audio_file = output_dir / f"heatmap_{date.today().isoformat()}.mp3"

    text_to_audio(body, audio_file)

    caption = f"Heatmap Daily Briefing – {date.today().strftime('%B %d, %Y')}\n{subject}"
    send_telegram_audio(audio_file, caption)

    log.info("Done!")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error("Failed: %s", e)
        sys.exit(1)
