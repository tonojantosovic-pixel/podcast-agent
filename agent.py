#!/usr/bin/env python3
"""Jadro logiky pre prepis podcastov cez Gemini File API."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import parse_qs, urlparse

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

DOWNLOAD_DIR = Path("downloads")
OUTPUT_DIR = Path("vystup")
REQUEST_TIMEOUT_MS = 3_600_000
MAX_RETRIES = 5
RETRY_DELAY_SEC = 20
LARGE_FILE_MB = 28
FILE_POLL_INTERVAL_SEC = 5
FILE_PROCESSING_BASE_WAIT_SEC = 600
FILE_PROCESSING_PER_MB_SEC = 15
AUDIO_MIME_TYPE = "audio/mpeg"
TEMPERATURE = 0.2

StatusCallback = Callable[[str], None]

SYSTEM_INSTRUCTION = """Si asistent na prepis podcastového audia. Striktne dodržiavaj:

- Nikdy neopakuj rovnaké frázy, vety, odseky ani časové značky.
- Necykluj sa and nevypĺňaj výstup opakovaným alebo „halucinovaným“ textom.
- Ak reč v audiu skončí alebo nastane dlhšia odmlčka/ticho, okamžite ukonči prepis.
  Po skončení audia už nič nepíš, neopakuj posledné vety and nevymýšľaj pokračovanie.
- Piš presne, vecne and bez zbytočnej kreativity.
- Obsah prelož do slovenčiny (okrem názvu jazyka v sekcii [JAZYK]).
"""

PROMPT = """Analyzuj priložené audio podcastu.

Vráť výstup VÝLUČNE v tomto formáte (všetko v slovenčine, okrem názvu jazyka v sekcii [JAZYK]):

[JAZYK]
<identifikovaný jazyk pôvodného audia>

[ZHRNUTIE]
<stručné zhrnutie obsahu v slovenčine, 5–15 viet>

[PREPIS]
<úplný prepis audia preložený do slovenčiny; ukonči presne tam, kde audio končí>
"""

PROMPT_META = """Analyzuj priložené audio podcastu.

Vráť výstup VÝLUČNE v tomto formáte (v slovenčine):

[JAZYK]
<identifikovaný jazyk pôvodného audia>

[ZHRNUTIE]
<stručné zhrnutie obsahu v slovenčine, 5–15 viet>
"""

PROMPT_TRANSCRIPT = """Vytvor úplný a detailný prepis priloženého podcastového audia od úplného začiatku (čas 00:00:00) až do konca.

Pravidlá:
- Prepis píš výhradne v slovenčine (obsah prelož, ak sa hovorí cudzím jazykom).
- Na začiatok KAŽDÉHO nového odseku vlož reálnu časovú značku vo formáte [HH:MM:SS], kedy daná myšlienka v audiu skutočne zaznie.
- Časové značky musia rásť a posúvať sa vpred (napr. [00:00:05], [00:01:20], [00:02:45]...). Je prísne zakázané opakovať rovnaký čas na viacerých riadkoch.
- Nevynechaj žiadnu konverzáciu.
- Ak v audiu nastane ticho alebo reč skončí, prepis okamžite ukonči.

Vráť výstup VÝLUČNE v tomto formáte:

[PREPIS]
<úplný prepis s postupujúcim časom>
"""

SECTION_MARKERS = ("JAZYK", "ZHRNUTIE", "PREPIS")


def log(message: str, status: StatusCallback | None = None) -> None:
    if status:
        status(message)
    else:
        print(message)


def load_api_key() -> str:
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Chýba API kľúč. Nastav GEMINI_API_KEY v súbore .env"
        )
    return api_key


def resolve_url(url: str | None, file_path: str | None) -> str:
    if url:
        return url.strip()
    if file_path:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Súbor neexistuje: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
        raise ValueError(f"V súbore {path} sa nenašla URL adresa.")
    raise ValueError("URL adresa nebola zadaná.")


def filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    episode_match = re.search(r"/episodes/([a-f0-9-]+)", parsed.path, re.I)
    if episode_match:
        return f"{episode_match.group(1)}.mp3"
    query = parse_qs(parsed.query)
    episode_id = query.get("awEpisodeId", [None])[0]
    if episode_id:
        return f"{episode_id}.mp3"
    name = Path(parsed.path).name
    if name and "." in name and name != "default.mp3":
        return name
    return "podcast.mp3"


def create_client(api_key: str) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )


def download_audio(
    url: str,
    dest_dir: Path,
    *,
    reuse: bool = False,
    status: StatusCallback | None = None,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename_from_url(url)

    if reuse and dest_path.is_file() and dest_path.stat().st_size > 0:
        size_mb = dest_path.stat().st_size / (1024 * 1024)
        log(f"Používam existujúci súbor: {dest_path} ({size_mb:.1f} MB)", status)
        return dest_path

    log(f"Sťahujem audio…", status)
    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with dest_path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)

    size_mb = dest_path.stat().st_size / (1024 * 1024)
    log(f"Audio uložené ({size_mb:.1f} MB)", status)
    return dest_path


def file_state_name(uploaded_file) -> str:
    state = getattr(uploaded_file, "state", None)
    if state is None:
        return types.FileState.ACTIVE.value
    return getattr(state, "name", str(state))


def processing_timeout_sec(audio_path: Path) -> int:
    size_mb = audio_path.stat().st_size / (1024 * 1024)
    return int(FILE_PROCESSING_BASE_WAIT_SEC + size_mb * FILE_PROCESSING_PER_MB_SEC)


def upload_file(
    client: genai.Client,
    audio_path: Path,
    *,
    status: StatusCallback | None = None,
):
    log("Nahrávam do Gemini File API…", status)
    uploaded = client.files.upload(
        file=str(audio_path),
        config=types.UploadFileConfig(
            display_name=audio_path.name,
            mime_type=AUDIO_MIME_TYPE,
        ),
    )
    if not uploaded.name:
        raise RuntimeError("File API nevrátilo identifikátor súboru.")
    log(f"Súbor nahraný: {uploaded.name}", status)
    return uploaded


def wait_for_file_active(
    client: genai.Client,
    uploaded_file,
    *,
    audio_path: Path,
    status: StatusCallback | None = None,
) -> object:
    name = uploaded_file.name
    deadline = time.monotonic() + processing_timeout_sec(audio_path)
    last_state = ""

    while time.monotonic() < deadline:
        uploaded_file = client.files.get(name=name)
        state_name = file_state_name(uploaded_file)

        if state_name == types.FileState.ACTIVE.value:
            log("Súbor je pripravený na spracovanie.", status)
            return uploaded_file
        if state_name == types.FileState.FAILED.value:
            error = getattr(uploaded_file, "error", None)
            detail = getattr(error, "message", error) if error else "neznáma chyba"
            raise RuntimeError(
                f"Spracovanie súboru na strane Google zlyhalo: {detail}"
            )

        if state_name != last_state:
            log(f"Čakám na spracovanie súboru ({state_name})…", status)
            last_state = state_name
        time.sleep(FILE_POLL_INTERVAL_SEC)

    raise TimeoutError(
        f"Časový limit spracovania súboru vypršal ({processing_timeout_sec(audio_path)} s)."
    )


def audio_part_from_file(uploaded_file) -> types.Part:
    uri = uploaded_file.uri
    if not uri:
        raise RuntimeError("Nahraný súbor nemá URI pre generate_content.")
    mime_type = uploaded_file.mime_type or AUDIO_MIME_TYPE
    return types.Part.from_uri(file_uri=uri, mime_type=mime_type)


def delete_uploaded_file(
    client: genai.Client,
    uploaded_file,
    *,
    status: StatusCallback | None = None,
) -> None:
    name = getattr(uploaded_file, "name", None)
    if not name:
        return
    for attempt in range(1, 3):
        try:
            client.files.delete(name=name)
            log("Súbor vymazaný z Google cloudu.", status)
            return
        except Exception as exc:
            if attempt == 2:
                log(f"Varovanie: súbor {name} sa nepodarilo vymazať: {exc}", status)
            else:
                time.sleep(2)


@dataclass
class GeminiAudioFile:
    client: genai.Client
    audio_path: Path
    status: StatusCallback | None = None
    file: object | None = None
    part: types.Part | None = None

    def upload_and_wait(self) -> types.Part:
        self.file = upload_file(self.client, self.audio_path, status=self.status)
        self.file = wait_for_file_active(
            self.client,
            self.file,
            audio_path=self.audio_path,
            status=self.status,
        )
        self.part = audio_part_from_file(self.file)
        return self.part

    def cleanup(self) -> None:
        if self.file is not None:
            delete_uploaded_file(self.client, self.file, status=self.status)
            self.file = None
            self.part = None


@contextmanager
def gemini_audio_file(
    client: genai.Client,
    audio_path: Path,
    *,
    status: StatusCallback | None = None,
) -> Iterator[types.Part]:
    handle = GeminiAudioFile(
        client=client, audio_path=audio_path, status=status
    )
    try:
        yield handle.upload_and_wait()
    finally:
        handle.cleanup()


def generation_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        temperature=TEMPERATURE,
        system_instruction=SYSTEM_INSTRUCTION,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )


def generate_text(
    client: genai.Client,
    audio_part: types.Part,
    prompt: str,
    *,
    label: str,
    model_name: str,
    stream: bool = False,
    status: StatusCallback | None = None,
) -> str:
    config = generation_config()
    contents = [audio_part, prompt]
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(f"{label}…", status)
            if stream:
                parts: list[str] = []
                for chunk in client.models.generate_content_stream(
                    model=model_name,
                    contents=contents,
                    config=config,
                ):
                    if chunk.text:
                        parts.append(chunk.text)
                text = "".join(parts)
            else:
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )
                text = response.text or ""

            if not text.strip():
                raise RuntimeError("Model nevrátil žiadny text.")
            return text.strip()
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY_SEC * attempt
                log(
                    f"{label}: pokus {attempt}/{MAX_RETRIES} zlyhal, opakujem o {wait} s…",
                    status,
                )
                time.sleep(wait)
            else:
                raise last_error from exc

    raise RuntimeError(f"{label} sa nepodaril.")


def transcribe(
    client: genai.Client,
    audio_part: types.Part,
    audio_path: Path,
    *,
    model_name: str,
    status: StatusCallback | None = None,
) -> dict[str, str]:
    size_mb = audio_path.stat().st_size / (1024 * 1024)
    log(f"Generujem prepis ({model_name}, {size_mb:.1f} MB)…", status)

    if size_mb >= LARGE_FILE_MB:
        log("Dlhá epizóda – spracovanie v dvoch krokoch.", status)
        meta_text = generate_text(
            client,
            audio_part,
            PROMPT_META,
            label="Jazyk a zhrnutie",
            model_name=model_name,
            status=status,
        )
        transcript_text = generate_text(
            client,
            audio_part,
            PROMPT_TRANSCRIPT,
            label="Plný prepis",
            model_name=model_name,
            stream=True,
            status=status,
        )
        
        return {
            "JAZYK": extract_section(meta_text, "JAZYK"),
            "ZHRNUTIE": extract_section(meta_text, "ZHRNUTIE"),
            "PREPIS": extract_section(transcript_text, "PREPIS") or transcript_text,
            "RAW_RESPONSE": transcript_text
        }

    raw = generate_text(
        client,
        audio_part,
        PROMPT,
        label="Kompletný výstup",
        model_name=model_name,  # TU BOLA CHYBA - opravené odovzdanie premennej
        stream=True,
        status=status,
    )
    return parse_sections(raw)


def extract_section(text: str, marker: str) -> str:
    pattern = re.compile(rf"\[{marker}\]\s*\n?", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return ""
    start = match.end()
    next_marker = re.compile(r"\[(JAZYK|ZHRNUTIE|PREPIS)\]", re.IGNORECASE)
    next_match = next_marker.search(text, start)
    end = next_match.start() if next_match else len(text)
    return text[start:end].strip()


def parse_sections(text: str) -> dict[str, str]:
    pattern = re.compile(
        r"\[(JAZYK|ZHRNUTIE|PREPIS)\]\s*\n?",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    
    sections: dict[str, str] = {"RAW_RESPONSE": text}
    if not matches:
        sections["JAZYK"] = "Neznámy"
        sections["ZHRNUTIE"] = "Nepodarilo sa oddeliť sekcie."
        sections["PREPIS"] = text
        return sections

    for i, match in enumerate(matches):
        key = match.group(1).upper()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[key] = text[start:end].strip()

    for m in SECTION_MARKERS:
        if m not in sections:
            sections[m] = text if m == "PREPIS" else "Chýba"

    return sections


def save_outputs(sections: dict[str, str], base_name: str) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(base_name).stem

    summary_path = OUTPUT_DIR / f"{stem}_zhrnutie.txt"
    transcript_path = OUTPUT_DIR / f"{stem}_prepis.txt"

    summary_body = f"Jazyk: {sections.get('JAZYK', 'Neznámy')}\n\n{sections.get('ZHRNUTIE', '')}\n"
    transcript_body = sections.get("PREPIS", sections.get("RAW_RESPONSE", "")) + "\n"

    summary_path.write_text(summary_body, encoding="utf-8")
    transcript_path.write_text(transcript_body, encoding="utf-8")

    return summary_path, transcript_path


def process_podcast(
    url: str,
    *,
    model_name: str = "gemini-2.5-flash",
    reuse_download: bool = False,
    status: StatusCallback | None = None,
) -> dict[str, str]:
    """Stiahne audio, prepíše ho cez Gemini File API a vráti sekcie."""
    url = url.strip()
    if not url:
        raise ValueError("URL adresa je prázdna.")

    api_key = load_api_key()
    client = create_client(api_key)
    audio_path = download_audio(
        url, DOWNLOAD_DIR, reuse=reuse_download, status=status
    )

    with gemini_audio_file(client, audio_path, status=status) as audio_part:
        sections = transcribe(client, audio_part, audio_path, model_name=model_name, status=status)

    save_outputs(sections, audio_path.name)
    return sections


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AI asistent na prepis podcastového audia (Gemini).",
    )
    parser.add_argument("--url", help="URL adresa mp3 podcastu")
    parser.add_argument(
        "--file",
        "-f",
        dest="url_file",
        metavar="PATH",
        help="Textový súbor s URL adresou",
    )
    parser.add_argument(
        "--reuse-download",
        action="store_true",
        help="Preskočí sťahovanie, ak mp3 už existuje",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.url and args.url_file:
        print("Použi buď --url, alebo --file, nie oboje naraz.", file=sys.stderr)
        sys.exit(1)

    try:
        url = resolve_url(args.url, args.url_file)
        sections = process_podcast(url, model_name="gemini-2.5-flash", reuse_download=args.reuse_download)
    except Exception as exc:
        print(f"Chyba: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\nHotovo.")
    print(f"  Jazyk:   {sections.get('JAZYK', 'Neznámy')}")
    print(f"  Výstupy: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
