#!/usr/bin/env python3
"""
SIRE Voice Agent — real-time voice assistant with Azure VoiceLive SDK.

Flow:
  1. User speaks into microphone                  (VoiceLive → STT)
  2. Model extracts intent + entity               (function calling / tools)
  3. Tool searches AI Search index                (search_user / search_group)
  4. Model confirms the result with the user      (TTS → speaker)

Requirements:
  - Python 3.11+
  - PyAudio  (Windows: pip install pyaudio,  macOS: brew install portaudio && pip install pyaudio)
  - Azure VoiceLive SDK   (pip install azure-ai-voicelive)
  - httpx, python-dotenv

Usage:
    python main.py                       # uses .env
    python main.py --use-token-credential  # Entra ID auth instead of API key
    python main.py --verbose              # debug logging
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import queue
import signal
import sys
from datetime import datetime
from typing import Any, Optional, Union, cast

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioNoiseReduction,
    AzureStandardVoice,
    FunctionCallOutputItem,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    ServerVad,
)
from dotenv import load_dotenv
import pyaudio

from config import AppConfig
from search_client import SIRESearchClient

# ---------------------------------------------------------------------------
# Change to script directory so .env is found
# ---------------------------------------------------------------------------
os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
os.makedirs("logs", exist_ok=True)
_ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

logging.basicConfig(
    filename=f"logs/{_ts}_sire.log",
    filemode="w",
    format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sire")

# Also log to console
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logger.addHandler(_console)

# ---------------------------------------------------------------------------
# Tool definitions for the Realtime session
# ---------------------------------------------------------------------------
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "search_group",
        "description": (
            "Search the group directory for a group by name. "
            "Call this when the user mentions a group, team, department, "
            "or organizational unit they want to interact with."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The group name or partial name to search for.",
                },
                "intent": {
                    "type": "string",
                    "description": "The user's intent/action verb, e.g. 'call', 'transfer to', 'connect to', 'look up', 'log in to', 'message'.",
                },
            },
            "required": ["query", "intent"],
        },
    },
    {
        "type": "function",
        "name": "search_user",
        "description": (
            "Search the user directory for a person by name. "
            "Call this when the user mentions a specific person's name "
            "they want to call, message, or interact with."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The person's name (first, last, or full) to search for.",
                },
                "intent": {
                    "type": "string",
                    "description": "The user's intent/action verb, e.g. 'call', 'transfer to', 'connect to', 'look up', 'log in to', 'message'.",
                },
            },
            "required": ["query", "intent"],
        },
    },
]

# ---------------------------------------------------------------------------
# System instructions
# ---------------------------------------------------------------------------
SYSTEM_INSTRUCTIONS = """\
You are SIRE, a voice-activated intelligent routing engine.

Your job:
1. Listen to the user's voice request.
2. Identify the **intent** — what the user wants to do (e.g. "call", "log in",
   "transfer to", "connect me to", "look up", etc.).
3. Identify the **entity** — the person or group the user is referring to.
4. Use the appropriate tool to look up the entity:
   - If the entity is a person/name → call `search_user`
   - If the entity is a group/team/department → call `search_group`
5. Check the `_confident` flag on the top result:
   - If `_confident` is true: The match is decisive. Go ahead and confirm it
     directly without listing alternatives:
     "I found John Smith, user ID u-jsmith, score 98. Shall I proceed?"
   - If `_confident` is false: Multiple close matches exist. Read the top 2-3
     results with their IDs and scores and ask which one the user meant.
6. If the user confirms, announce the confirmed entity, its ID, and intent clearly.
7. If the user says it's wrong, ask them to repeat or clarify.

Rules:
- Always search before confirming — never guess.
- Always include the GroupID or user id alongside the name in your response.
- Each search result includes a `_match_score` (0-100 confidence).
- Use the `_confident` flag to decide whether to auto-confirm or disambiguate.
- If the top match score is below 50, warn the user the match may not be accurate.
- Keep responses concise and conversational.
- If no results are found, ask the user to repeat or spell the name.
- State the detected intent back to the user for clarity.
"""

# ---------------------------------------------------------------------------
# Audio processor (adapted from ART Voice Agent Accelerator)
# ---------------------------------------------------------------------------

class AudioProcessor:
    """Handles real-time microphone capture and speaker playback via PyAudio."""

    loop: asyncio.AbstractEventLoop

    class _Packet:
        __slots__ = ("seq", "data")

        def __init__(self, seq: int, data: Optional[bytes]):
            self.seq = seq
            self.data = data

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.audio = pyaudio.PyAudio()

        # 24 kHz, 16-bit mono (VoiceLive standard)
        self.fmt = pyaudio.paInt16
        self.channels = 1
        self.rate = 24_000
        self.chunk = 1200  # 50 ms

        # Configurable device indices (None = system default)
        _in = os.getenv("AUDIO_INPUT_DEVICE_INDEX")
        _out = os.getenv("AUDIO_OUTPUT_DEVICE_INDEX")
        self.input_device_index: Optional[int] = int(_in) if _in else None
        self.output_device_index: Optional[int] = int(_out) if _out else None

        self.input_stream: Optional[pyaudio.Stream] = None
        self._pq: queue.Queue[AudioProcessor._Packet] = queue.Queue()
        self._pb_base = 0
        self._seq = 0
        self.output_stream: Optional[pyaudio.Stream] = None

        in_name = self.audio.get_device_info_by_index(self.input_device_index)["name"] if self.input_device_index is not None else "default"
        out_name = self.audio.get_device_info_by_index(self.output_device_index)["name"] if self.output_device_index is not None else "default"
        logger.info("AudioProcessor ready (24 kHz PCM16 mono) input=%s output=%s", in_name, out_name)

    # ── capture ──────────────────────────────────────────────────────────

    def start_capture(self) -> None:
        if self.input_stream:
            return

        self.loop = asyncio.get_event_loop()

        def _cb(in_data: bytes, _fc: int, _ti: Any, _sf: int):
            b64 = base64.b64encode(in_data).decode()
            asyncio.run_coroutine_threadsafe(
                self.connection.input_audio_buffer.append(audio=b64),
                self.loop,
            )
            return (None, pyaudio.paContinue)

        open_kwargs: dict[str, Any] = dict(
            format=self.fmt,
            channels=self.channels,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk,
            stream_callback=_cb,
        )
        if self.input_device_index is not None:
            open_kwargs["input_device_index"] = self.input_device_index
        self.input_stream = self.audio.open(**open_kwargs)
        logger.info("Microphone capture started (device %s)", self.input_device_index)

    # ── playback ─────────────────────────────────────────────────────────

    def start_playback(self) -> None:
        if self.output_stream:
            return

        remaining = bytes()

        def _pb(_in: Any, frame_count: int, _ti: Any, _sf: int):
            nonlocal remaining
            need = frame_count * pyaudio.get_sample_size(pyaudio.paInt16)
            out = remaining[:need]
            remaining = remaining[need:]

            while len(out) < need:
                try:
                    pkt = self._pq.get_nowait()
                except queue.Empty:
                    out += bytes(need - len(out))
                    continue

                if pkt is None or pkt.data is None:
                    break
                if pkt.seq < self._pb_base:
                    continue

                take = need - len(out)
                out += pkt.data[:take]
                remaining = pkt.data[take:]

            if len(out) >= need:
                return (out, pyaudio.paContinue)
            return (out, pyaudio.paComplete)

        out_kwargs: dict[str, Any] = dict(
            format=self.fmt,
            channels=self.channels,
            rate=self.rate,
            output=True,
            frames_per_buffer=self.chunk,
            stream_callback=_pb,
        )
        if self.output_device_index is not None:
            out_kwargs["output_device_index"] = self.output_device_index
        self.output_stream = self.audio.open(**out_kwargs)
        logger.info("Speaker playback started (device %s)", self.output_device_index)

    # ── helpers ──────────────────────────────────────────────────────────

    def _next_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    def enqueue(self, data: Optional[bytes]) -> None:
        self._pq.put(self._Packet(self._next_seq(), data))

    def skip(self) -> None:
        self._pb_base = self._next_seq()

    def shutdown(self) -> None:
        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
            self.input_stream = None
            logger.info("Stopped capture")

        if self.output_stream:
            self.skip()
            self.enqueue(None)
            self.output_stream.stop_stream()
            self.output_stream.close()
            self.output_stream = None
            logger.info("Stopped playback")

        self.audio.terminate()
        logger.info("Audio processor shut down")


# ---------------------------------------------------------------------------
# Voice agent
# ---------------------------------------------------------------------------

class SIREVoiceAgent:
    """
    Real-time voice agent that:
      - listens via VoiceLive SDK
      - uses function-calling tools for AI Search lookups
      - verifies results with the user verbally
    """

    def __init__(
        self,
        cfg: AppConfig,
        credential: Union[AzureKeyCredential, AsyncTokenCredential],
    ) -> None:
        self._cfg = cfg
        self._credential = credential
        self._search = SIRESearchClient(cfg.search)
        self._conn: Any = None
        self._ap: Optional[AudioProcessor] = None
        self._active_response = False
        self._response_done = False

        # Tracks in-progress function call argument deltas
        self._fn_call_id: Optional[str] = None
        self._fn_call_name: Optional[str] = None
        self._fn_call_args: str = ""

    async def run(self) -> None:
        """Connect to VoiceLive and start the event loop."""
        vc = self._cfg.voicelive
        logger.info("Connecting to VoiceLive (%s) model=%s", vc.endpoint, vc.model)

        try:
            async with connect(
                endpoint=vc.endpoint,
                credential=self._credential,
                model=vc.model,
            ) as conn:
                self._conn = conn
                self._ap = AudioProcessor(conn)

                await self._configure_session()
                self._ap.start_playback()

                print("\n" + "=" * 60, flush=True)
                print("  SIRE VOICE AGENT — READY", flush=True)
                print("  Speak naturally. Say a person's name or group.", flush=True)
                print("  Press Ctrl+C to exit.", flush=True)
                print("=" * 60 + "\n", flush=True)

                await self._event_loop()
        finally:
            if self._ap:
                self._ap.shutdown()

    # ── session setup ────────────────────────────────────────────────────

    async def _configure_session(self) -> None:
        vc = self._cfg.voicelive

        # Voice config
        if "-" in vc.voice:
            voice_cfg: Any = AzureStandardVoice(name=vc.voice)
        else:
            voice_cfg = vc.voice

        session = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions=SYSTEM_INSTRUCTIONS,
            voice=voice_cfg,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            turn_detection=ServerVad(
                threshold=0.5,
                prefix_padding_ms=300,
                silence_duration_ms=800,
            ),
            input_audio_echo_cancellation=AudioEchoCancellation(),
            input_audio_noise_reduction=AudioNoiseReduction(
                type="azure_deep_noise_suppression"
            ),
            tools=TOOLS,
        )

        await self._conn.session.update(session=session)
        logger.info("Session configured with %d tool(s)", len(TOOLS))

    # ── event loop ───────────────────────────────────────────────────────

    async def _event_loop(self) -> None:
        async for event in self._conn:
            await self._on_event(event)

    async def _on_event(self, event: Any) -> None:
        ap = self._ap
        conn = self._conn
        assert ap and conn

        etype = event.type  # ServerEventType enum or string

        # -- session ready ---------------------------------------------------
        if etype == ServerEventType.SESSION_UPDATED:
            logger.info("Session ready: %s", event.session.id)
            ap.start_capture()
            print("Listening...\n", flush=True)

        # -- user speech -----------------------------------------------------
        elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            logger.info("User speaking → barge-in")
            print("[You are speaking...]", flush=True)
            ap.skip()
            if self._active_response and not self._response_done:
                try:
                    await conn.response.cancel()
                except Exception:
                    pass

        elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            logger.info("User stopped speaking")
            print("[Processing...]", flush=True)

        # -- response lifecycle ----------------------------------------------
        elif etype == ServerEventType.RESPONSE_CREATED:
            self._active_response = True
            self._response_done = False

        elif etype == ServerEventType.RESPONSE_AUDIO_DELTA:
            ap.enqueue(event.delta)

        elif etype == ServerEventType.RESPONSE_AUDIO_DONE:
            logger.info("Assistant audio done")

        elif etype == ServerEventType.RESPONSE_DONE:
            self._active_response = False
            self._response_done = True
            logger.info("Response complete")

        # -- function call argument streaming --------------------------------
        elif str(etype) == "response.function_call_arguments.delta":
            self._fn_call_args += getattr(event, "delta", "")

        elif str(etype) == "response.function_call_arguments.done":
            # Full args received — execute the tool
            call_id = getattr(event, "call_id", self._fn_call_id)
            name = getattr(event, "name", self._fn_call_name)
            args_str = getattr(event, "arguments", self._fn_call_args)
            logger.info("Tool call: %s(%s)  call_id=%s", name, args_str, call_id)

            result = await self._execute_tool(name, args_str)

            # Send tool output back to the model
            await conn.conversation.item.create(
                item=FunctionCallOutputItem(
                    call_id=call_id,
                    output=json.dumps(result, default=str),
                )
            )
            # Ask the model to continue with a response
            await conn.response.create()

            # Reset
            self._fn_call_id = None
            self._fn_call_name = None
            self._fn_call_args = ""

        # -- output transcript (optional logging) ----------------------------
        elif str(etype) == "response.audio_transcript.delta":
            # Streamed assistant text — show live
            text = getattr(event, "delta", "")
            if text:
                print(text, end="", flush=True)

        elif str(etype) == "response.audio_transcript.done":
            print(flush=True)  # newline after full transcript

        # -- input transcription (user speech text) --------------------------
        elif str(etype) == "conversation.item.input_audio_transcription.completed":
            transcript = getattr(event, "transcript", "")
            if transcript:
                print(f"\n  YOU: {transcript}", flush=True)

        # -- conversation events -------------------------------------------
        elif etype == ServerEventType.CONVERSATION_ITEM_CREATED:
            # Capture function call metadata from the item
            item = getattr(event, "item", None)
            if item and getattr(item, "type", None) == "function_call":
                self._fn_call_id = getattr(item, "call_id", None)
                self._fn_call_name = getattr(item, "name", None)
                self._fn_call_args = ""
                logger.info(
                    "Function call item created: %s (call_id=%s)",
                    self._fn_call_name,
                    self._fn_call_id,
                )

        # -- errors ----------------------------------------------------------
        elif etype == ServerEventType.ERROR:
            msg = event.error.message
            if "no active response" in msg.lower():
                logger.debug("Benign cancel: %s", msg)
            else:
                logger.error("VoiceLive error: %s", msg)
                print(f"ERROR: {msg}", flush=True)

        else:
            logger.debug("Unhandled event: %s", etype)

    # ── tool execution ───────────────────────────────────────────────────

    async def _execute_tool(self, name: Optional[str], args_json: str) -> Any:
        """Dispatch a tool call to the appropriate search function."""
        try:
            args = json.loads(args_json) if args_json else {}
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON arguments: {args_json}"}

        query = args.get("query", "")
        intent = args.get("intent", "look up")
        logger.info("Executing tool %s with query='%s' intent='%s'", name, query, intent)
        print(f"\n  [Intent: {intent.upper()}  |  Searching {name} for '{query}'...]", flush=True)

        try:
            if name == "search_group":
                results = await self._search.search_group(query)
            elif name == "search_user":
                results = await self._search.search_user(query)
            else:
                return {"error": f"Unknown tool: {name}"}

            print(f"  [Found {len(results)} result(s)]", flush=True)
            if results:
                top_r = results[0]
                confident = top_r.get('_confident', False)
                gap = top_r.get('_gap_to_next', 0)
                print(f"  [Confident: {confident}  |  Gap to #2: {gap} pts]", flush=True)
            for i, r in enumerate(results, 1):
                score = r.get("_match_score", "?")
                strategies = r.get("_match_strategies", "")
                if name == "search_group":
                    print(f"    {i}. [{r.get('GroupID', '?')}] {r.get('GroupName', '?')}  (score: {score})  [{strategies}]", flush=True)
                elif name == "search_user":
                    print(f"    {i}. [{r.get('id', '?')}] {r.get('FirstName', '')} {r.get('LastName', '')}  (score: {score})  [{strategies}]", flush=True)
            return {"results": results, "count": len(results)}
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SIRE Voice Agent — VoiceLive + AI Search",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--use-token-credential", action="store_true",
                    help="Use AzureCliCredential instead of API key")
    p.add_argument("--verbose", action="store_true",
                    help="Enable DEBUG logging")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    cfg = AppConfig.from_env()

    # Credential
    cred: Union[AzureKeyCredential, AsyncTokenCredential]
    if args.use_token_credential or cfg.voicelive.use_token_credential:
        cred = AzureCliCredential()
        logger.info("Using AzureCliCredential")
    else:
        assert cfg.voicelive.api_key, "Set AZURE_VOICELIVE_API_KEY or use --use-token-credential"
        cred = AzureKeyCredential(cfg.voicelive.api_key)
        logger.info("Using API key credential")

    agent = SIREVoiceAgent(cfg, cred)

    # Graceful shutdown
    def _sig(_s: int, _f: Any) -> None:
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\nSIRE shutting down. Goodbye!")
    except Exception as e:
        print(f"Fatal error: {e}")
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    # Pre-flight: check audio devices
    try:
        p = pyaudio.PyAudio()
        has_input = any(
            cast(Union[int, float], p.get_device_info_by_index(i).get("maxInputChannels", 0) or 0) > 0
            for i in range(p.get_device_count())
        )
        has_output = any(
            cast(Union[int, float], p.get_device_info_by_index(i).get("maxOutputChannels", 0) or 0) > 0
            for i in range(p.get_device_count())
        )
        p.terminate()
        if not has_input:
            print("No microphone found.")
            sys.exit(1)
        if not has_output:
            print("No speaker found.")
            sys.exit(1)
    except Exception as e:
        print(f"Audio check failed: {e}")
        sys.exit(1)

    print("SIRE Voice Agent — Azure VoiceLive + AI Search")
    print("=" * 50)
    main()
