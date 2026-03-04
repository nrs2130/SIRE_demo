"""
SIRE Voice Agent — Streamlit UI

Provides a browser-based control panel for the real-time voice agent.
Audio still flows through the system mic/speakers (PyAudio), while the UI
shows live transcripts, search results, session status, and a prominent
Stop button to disconnect instantly and avoid realtime-model costs.

Usage:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Union

import streamlit as st
from dotenv import load_dotenv

# ── Change to script dir so .env is found ──────────────────────────────────
os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(override=True)

import pyaudio
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

from config import AppConfig
from search_client import SIRESearchClient

# MCP server tool functions (same search logic, but through MCP wrapper)
from mcp_server.server import (
    search_group as mcp_search_group,
    search_user as mcp_search_user,
)

# Re-use tool definitions and system prompt from main module
from main import TOOLS, SYSTEM_INSTRUCTIONS

logger = logging.getLogger("sire.streamlit")


# ---------------------------------------------------------------------------
# MCP Search Adapter — same interface as SIRESearchClient but via MCP tools
# ---------------------------------------------------------------------------

class MCPSearchAdapter:
    """Wraps MCP server tool functions to match the SIRESearchClient interface.

    This lets us swap the search backend in the agent loop without changing
    any call-site code.  The MCP path serialises/deserialises through JSON
    (exactly as a real MCP client would), so any serialisation issues surface
    here too.
    """

    async def search_group(self, query: str, top: int = 5) -> list[dict[str, Any]]:
        raw = await mcp_search_group(query, top=top)
        return json.loads(raw)

    async def search_user(self, query: str, top: int = 5) -> list[dict[str, Any]]:
        raw = await mcp_search_user(query, top=top)
        return json.loads(raw)

# ---------------------------------------------------------------------------
# Shared state between the agent thread and Streamlit UI
# ---------------------------------------------------------------------------

@dataclass
class UIEvent:
    """An event pushed from the agent thread to the UI."""
    timestamp: str
    kind: str          # "status" | "user" | "assistant" | "search" | "error" | "info" | "intent"
    text: str
    data: Any = None   # optional structured data (e.g. search results)


@dataclass
class AgentState:
    """Thread-safe shared state between agent and UI."""
    connected: bool = False
    session_id: str = ""
    session_start: Optional[float] = None
    stop_requested: bool = False
    events: list[UIEvent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, kind: str, text: str, data: Any = None):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.events.append(UIEvent(timestamp=ts, kind=kind, text=text, data=data))

    def get_events(self) -> list[UIEvent]:
        with self._lock:
            return list(self.events)

    def clear(self):
        with self._lock:
            self.events.clear()
            self.connected = False
            self.session_id = ""
            self.session_start = None
            self.stop_requested = False


# Singleton state — persists across Streamlit reruns via module-level variable
# We use st.session_state to hold the reference.
def _get_state() -> AgentState:
    if "agent_state" not in st.session_state:
        st.session_state["agent_state"] = AgentState()
    return st.session_state["agent_state"]


# ---------------------------------------------------------------------------
# Audio processor (same as main.py but with no console prints)
# ---------------------------------------------------------------------------

class StreamlitAudioProcessor:
    loop: asyncio.AbstractEventLoop

    class _Packet:
        __slots__ = ("seq", "data")
        def __init__(self, seq: int, data: Optional[bytes]):
            self.seq = seq
            self.data = data

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.audio = pyaudio.PyAudio()
        self.fmt = pyaudio.paInt16
        self.channels = 1
        self.rate = 24_000
        self.chunk = 1200

        _in = os.getenv("AUDIO_INPUT_DEVICE_INDEX")
        _out = os.getenv("AUDIO_OUTPUT_DEVICE_INDEX")
        self.input_device_index: Optional[int] = int(_in) if _in else None
        self.output_device_index: Optional[int] = int(_out) if _out else None

        self.input_stream: Optional[pyaudio.Stream] = None
        self._pq: queue.Queue[StreamlitAudioProcessor._Packet] = queue.Queue()
        self._pb_base = 0
        self._seq = 0
        self.output_stream: Optional[pyaudio.Stream] = None

    def start_capture(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.input_stream:
            return
        self.loop = loop

        def _cb(in_data: bytes, _fc: int, _ti: Any, _sf: int):
            b64 = base64.b64encode(in_data).decode()
            asyncio.run_coroutine_threadsafe(
                self.connection.input_audio_buffer.append(audio=b64),
                self.loop,
            )
            return (None, pyaudio.paContinue)

        kw: dict[str, Any] = dict(
            format=self.fmt, channels=self.channels, rate=self.rate,
            input=True, frames_per_buffer=self.chunk, stream_callback=_cb,
        )
        if self.input_device_index is not None:
            kw["input_device_index"] = self.input_device_index
        self.input_stream = self.audio.open(**kw)

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

        kw: dict[str, Any] = dict(
            format=self.fmt, channels=self.channels, rate=self.rate,
            output=True, frames_per_buffer=self.chunk, stream_callback=_pb,
        )
        if self.output_device_index is not None:
            kw["output_device_index"] = self.output_device_index
        self.output_stream = self.audio.open(**kw)

    def _next_seq(self) -> int:
        s = self._seq; self._seq += 1; return s

    def enqueue(self, data: Optional[bytes]) -> None:
        self._pq.put(self._Packet(self._next_seq(), data))

    def skip(self) -> None:
        self._pb_base = self._next_seq()

    def shutdown(self) -> None:
        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
            self.input_stream = None
        if self.output_stream:
            self.skip(); self.enqueue(None)
            self.output_stream.stop_stream()
            self.output_stream.close()
            self.output_stream = None
        self.audio.terminate()


# ---------------------------------------------------------------------------
# Agent runner (runs in a background thread)
# ---------------------------------------------------------------------------

async def _run_agent(state: AgentState, search_backend: str = "python"):
    """Async entry point for the voice agent, driven by AgentState."""
    cfg = AppConfig.from_env()
    cred: Union[AzureKeyCredential, AsyncTokenCredential]
    if cfg.voicelive.use_token_credential:
        cred = AzureCliCredential()
        state.push("info", "Using Entra ID (AzureCliCredential)")
    else:
        assert cfg.voicelive.api_key
        cred = AzureKeyCredential(cfg.voicelive.api_key)
        state.push("info", "Using API key credential")

    # Select search backend
    if search_backend == "mcp":
        search = MCPSearchAdapter()
        state.push("info", "Search backend: MCP Server")
    else:
        search = SIRESearchClient(cfg.search)
        state.push("info", "Search backend: Custom Python")
    vc = cfg.voicelive

    state.push("status", f"Connecting to {vc.endpoint} ...")

    fn_call_id: Optional[str] = None
    fn_call_name: Optional[str] = None
    fn_call_args: str = ""
    active_response = False
    response_done = False
    assistant_transcript = ""

    try:
        async with connect(
            endpoint=vc.endpoint,
            credential=cred,
            model=vc.model,
        ) as conn:
            ap = StreamlitAudioProcessor(conn)

            # Configure session
            voice_cfg: Any = AzureStandardVoice(name=vc.voice) if "-" in vc.voice else vc.voice
            session = RequestSession(
                modalities=[Modality.TEXT, Modality.AUDIO],
                instructions=SYSTEM_INSTRUCTIONS,
                voice=voice_cfg,
                input_audio_format=InputAudioFormat.PCM16,
                output_audio_format=OutputAudioFormat.PCM16,
                turn_detection=ServerVad(threshold=0.5, prefix_padding_ms=300, silence_duration_ms=800),
                input_audio_echo_cancellation=AudioEchoCancellation(),
                input_audio_noise_reduction=AudioNoiseReduction(type="azure_deep_noise_suppression"),
                tools=TOOLS,
            )
            await conn.session.update(session=session)
            ap.start_playback()
            state.push("info", f"Session configured with {len(TOOLS)} tool(s)")

            loop = asyncio.get_event_loop()

            try:
                async for event in conn:
                    # Check for stop request
                    if state.stop_requested:
                        state.push("status", "Stop requested — disconnecting...")
                        break

                    etype = event.type

                    if etype == ServerEventType.SESSION_UPDATED:
                        state.session_id = event.session.id
                        state.session_start = time.time()
                        state.connected = True
                        ap.start_capture(loop)
                        state.push("status", f"Connected — session {event.session.id}")

                    elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                        state.push("info", "Speech detected")
                        ap.skip()
                        if active_response and not response_done:
                            try:
                                await conn.response.cancel()
                            except Exception:
                                pass

                    elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                        state.push("info", "Processing speech...")

                    elif etype == ServerEventType.RESPONSE_CREATED:
                        active_response = True
                        response_done = False
                        assistant_transcript = ""

                    elif etype == ServerEventType.RESPONSE_AUDIO_DELTA:
                        ap.enqueue(event.delta)

                    elif etype == ServerEventType.RESPONSE_DONE:
                        active_response = False
                        response_done = True
                        if assistant_transcript.strip():
                            state.push("assistant", assistant_transcript.strip())
                            assistant_transcript = ""

                    elif str(etype) == "response.audio_transcript.delta":
                        assistant_transcript += getattr(event, "delta", "")

                    elif str(etype) == "response.audio_transcript.done":
                        full = getattr(event, "transcript", assistant_transcript)
                        if full and full.strip():
                            state.push("assistant", full.strip())
                        assistant_transcript = ""

                    elif str(etype) == "conversation.item.input_audio_transcription.completed":
                        transcript = getattr(event, "transcript", "")
                        if transcript and transcript.strip():
                            state.push("user", transcript.strip())

                    elif etype == ServerEventType.CONVERSATION_ITEM_CREATED:
                        item = getattr(event, "item", None)
                        if item and getattr(item, "type", None) == "function_call":
                            fn_call_id = getattr(item, "call_id", None)
                            fn_call_name = getattr(item, "name", None)
                            fn_call_args = ""

                    elif str(etype) == "response.function_call_arguments.delta":
                        fn_call_args += getattr(event, "delta", "")

                    elif str(etype) == "response.function_call_arguments.done":
                        call_id = getattr(event, "call_id", fn_call_id)
                        name = getattr(event, "name", fn_call_name)
                        args_str = getattr(event, "arguments", fn_call_args)

                        try:
                            args = json.loads(args_str) if args_str else {}
                        except json.JSONDecodeError:
                            args = {}
                        query = args.get("query", "")
                        intent = args.get("intent", "look up")

                        # Show extracted intent from the model
                        entity_type = "user" if name == "search_user" else "group"
                        state.push("intent", f"Intent: {intent.upper()}  |  Entity: '{query}' ({entity_type})")
                        state.push("info", f"Searching {name} for '{query}'...")

                        try:
                            if name == "search_group":
                                results = await search.search_group(query)
                            elif name == "search_user":
                                results = await search.search_user(query)
                            else:
                                results = []

                            # Push best match info
                            if results:
                                best = results[0]
                                score = best.get('_match_score', '?')
                                confident = best.get('_confident', False)
                                gap = best.get('_gap_to_next', 0)
                                conf_label = "✅ AUTO-CONFIRM" if confident else "⚠️ DISAMBIGUATE"
                                if name == "search_user":
                                    best_label = f"{best.get('FirstName', '')} {best.get('LastName', '')}  [{best.get('id', '?')}]"
                                else:
                                    best_label = f"{best.get('GroupName', '?')}  [{best.get('GroupID', '?')}]"
                                state.push("intent", f"Best match: {best_label}  |  score={score}  gap={gap}  {conf_label}")

                            state.push("search", f"{name}('{query}') → {len(results)} result(s)", data=results)
                            result_payload = {"results": results, "count": len(results)}
                        except Exception as e:
                            state.push("error", f"Search failed: {e}")
                            result_payload = {"error": str(e)}

                        await conn.conversation.item.create(
                            item=FunctionCallOutputItem(
                                call_id=call_id,
                                output=json.dumps(result_payload, default=str),
                            )
                        )
                        await conn.response.create()
                        fn_call_id = None
                        fn_call_name = None
                        fn_call_args = ""

                    elif etype == ServerEventType.ERROR:
                        msg = event.error.message
                        if "no active response" not in msg.lower():
                            state.push("error", msg)

            finally:
                ap.shutdown()

    except Exception as e:
        state.push("error", f"Fatal: {e}")
        logger.exception("Agent error")
    finally:
        state.connected = False
        state.push("status", "Disconnected")


def _agent_thread(state: AgentState, search_backend: str = "python"):
    """Thread target — runs the async agent."""
    try:
        asyncio.run(_run_agent(state, search_backend=search_backend))
    except Exception as e:
        state.push("error", f"Thread error: {e}")
    finally:
        state.connected = False


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

def render_ui():
    st.set_page_config(
        page_title="SIRE Voice Agent",
        page_icon="🎙️",
        layout="wide",
    )

    state = _get_state()

    # ── Custom CSS ──────────────────────────────────────────────────────
    st.markdown("""
    <style>
    .stApp { max-width: 1200px; margin: 0 auto; }
    .event-user { background: #e3f2fd; padding: 8px 12px; border-radius: 8px; margin: 4px 0; border-left: 4px solid #1976d2; }
    .event-assistant { background: #f3e5f5; padding: 8px 12px; border-radius: 8px; margin: 4px 0; border-left: 4px solid #7b1fa2; }
    .event-search { background: #e8f5e9; padding: 8px 12px; border-radius: 8px; margin: 4px 0; border-left: 4px solid #388e3c; }
    .event-error { background: #ffebee; padding: 8px 12px; border-radius: 8px; margin: 4px 0; border-left: 4px solid #d32f2f; }
    .event-info { background: #fff3e0; padding: 8px 12px; border-radius: 8px; margin: 4px 0; border-left: 4px solid #f57c00; }
    .event-status { background: #e0f7fa; padding: 8px 12px; border-radius: 8px; margin: 4px 0; border-left: 4px solid #0097a7; }
    .cost-warning { background: #fff9c4; padding: 12px; border-radius: 8px; border: 2px solid #fbc02d; margin: 8px 0; }
    </style>
    """, unsafe_allow_html=True)

    # ── Header ──────────────────────────────────────────────────────────
    st.title("🎙️ SIRE Voice Agent")
    st.caption("Real-time voice assistant — VoiceLive + AI Search")

    # ── Sidebar: Controls ───────────────────────────────────────────────
    with st.sidebar:
        st.header("Session Control")

        # Search backend selector
        st.markdown("---")
        st.subheader("Search Backend")
        backend_choice = st.radio(
            "Choose which search path to use:",
            ["Custom Python", "MCP Server"],
            index=0,
            help=(
                "**Custom Python** — calls SIRESearchClient directly.\n\n"
                "**MCP Server** — calls through MCP tool wrappers "
                "(same underlying logic, but serialised via JSON as an MCP client would)."
            ),
            disabled=state.connected,  # can't switch mid-session
        )
        search_backend = "mcp" if backend_choice == "MCP Server" else "python"
        st.session_state["search_backend"] = search_backend

        if search_backend == "mcp":
            st.caption("🔌 MCP Server path active — results flow through MCP tool wrappers")
        else:
            st.caption("🐍 Custom Python path active — direct SIRESearchClient calls")
        st.markdown("---")

        if state.connected:
            st.success(f"🟢 Connected")
            if state.session_id:
                st.caption(f"Session: `{state.session_id[:20]}...`")
            if state.session_start:
                elapsed = int(time.time() - state.session_start)
                mins, secs = divmod(elapsed, 60)
                st.metric("Session Duration", f"{mins}m {secs}s")

            st.markdown("---")
            st.markdown('<div class="cost-warning">⚠️ <b>Realtime model is active</b><br>You are being billed per session-second.</div>', unsafe_allow_html=True)

            if st.button("🛑 STOP SESSION", type="primary", use_container_width=True):
                state.stop_requested = True
                st.rerun()
        else:
            st.info("🔵 Disconnected")
            if st.button("▶️ START SESSION", type="primary", use_container_width=True):
                state.clear()
                state.push("status", f"Starting agent ({backend_choice} backend)...")
                t = threading.Thread(target=_agent_thread, args=(state, search_backend), daemon=True)
                t.start()
                st.session_state["agent_thread"] = t
                time.sleep(1)
                st.rerun()

        st.markdown("---")
        st.header("Settings")
        cfg = AppConfig.from_env()
        st.text_input("Endpoint", value=cfg.voicelive.endpoint, disabled=True)
        st.text_input("Model", value=cfg.voicelive.model, disabled=True)
        st.text_input("Voice", value=cfg.voicelive.voice, disabled=True)
        st.text_input("Auth", value="Entra ID" if cfg.voicelive.use_token_credential else "API Key", disabled=True)

        _in = os.getenv("AUDIO_INPUT_DEVICE_INDEX", "default")
        _out = os.getenv("AUDIO_OUTPUT_DEVICE_INDEX", "default")
        st.text_input("Mic device", value=_in, disabled=True)
        st.text_input("Speaker device", value=_out, disabled=True)

        if st.button("Clear log", use_container_width=True):
            state.events.clear()
            st.rerun()

    # ── Main area: Transcript + Search Results ──────────────────────────
    col_transcript, col_results = st.columns([3, 2])

    events = state.get_events()

    with col_transcript:
        st.subheader("💬 Conversation")

        if not events:
            st.info("Start a session and speak into your microphone to see the conversation here.")
        else:
            transcript_container = st.container(height=500)
            with transcript_container:
                for ev in events:
                    ts = f"<small style='color:#999'>{ev.timestamp}</small>"
                    if ev.kind == "user":
                        st.markdown(f'<div class="event-user">{ts} &nbsp; 🧑 <b>You:</b> {ev.text}</div>', unsafe_allow_html=True)
                    elif ev.kind == "assistant":
                        st.markdown(f'<div class="event-assistant">{ts} &nbsp; 🤖 <b>SIRE:</b> {ev.text}</div>', unsafe_allow_html=True)
                    elif ev.kind == "search":
                        st.markdown(f'<div class="event-search">{ts} &nbsp; 🔍 {ev.text}</div>', unsafe_allow_html=True)
                    elif ev.kind == "error":
                        st.markdown(f'<div class="event-error">{ts} &nbsp; ❌ {ev.text}</div>', unsafe_allow_html=True)
                    elif ev.kind == "info":
                        st.markdown(f'<div class="event-info">{ts} &nbsp; ℹ️ {ev.text}</div>', unsafe_allow_html=True)
                    elif ev.kind == "intent":
                        st.markdown(f'<div class="event-status">{ts} &nbsp; 🎯 <b>{ev.text}</b></div>', unsafe_allow_html=True)
                    elif ev.kind == "status":
                        st.markdown(f'<div class="event-status">{ts} &nbsp; 📡 {ev.text}</div>', unsafe_allow_html=True)

    with col_results:
        st.subheader("🎯 Intent & Best Match")

        intent_events = [e for e in events if e.kind == "intent"]
        if intent_events:
            # Show the latest intent pair (intent line + best match line)
            recent_intents = intent_events[-2:] if len(intent_events) >= 2 else intent_events[-1:]
            for ie in recent_intents:
                if "Intent:" in ie.text:
                    st.info(f"**{ie.text}**")
                elif "Best match:" in ie.text:
                    st.success(f"**{ie.text}**")
        else:
            st.info("Intent and best match will appear here when the agent processes your speech.")

        st.markdown("---")
        st.subheader("🔍 Search Results")

        search_events = [e for e in events if e.kind == "search" and e.data]
        if not search_events:
            st.info("Search results will appear here when the agent looks up names or groups.")
        else:
            # Show the most recent search result expanded, others collapsed
            for i, ev in enumerate(reversed(search_events)):
                with st.expander(f"{ev.timestamp} — {ev.text}", expanded=(i == 0)):
                    if ev.data:
                        st.dataframe(ev.data, use_container_width=True)

    # ── Manual search testing (always available) ────────────────────────
    st.markdown("---")
    st.subheader("🧪 Manual Search Test")
    st.caption("Test AI Search directly without voice — doesn't use the realtime model.")

    test_col1, test_col2, test_col3 = st.columns(3)
    with test_col1:
        search_type = st.selectbox("Search type", ["User", "Group"])
    with test_col2:
        search_query = st.text_input("Query", placeholder="e.g. Barbara, Cardiology...")
    with test_col3:
        manual_backend = st.selectbox(
            "Backend",
            ["Both (compare)", "Custom Python", "MCP Server"],
            help="Run one backend or both side-by-side to compare results.",
        )

    if st.button("Search", disabled=not search_query):
        import asyncio as _aio
        import time as _time
        cfg = AppConfig.from_env()

        async def _run_python_search():
            client = SIRESearchClient(cfg.search)
            if search_type == "User":
                return await client.search_user(search_query, top=10)
            else:
                return await client.search_group(search_query, top=10)

        async def _run_mcp_search():
            adapter = MCPSearchAdapter()
            if search_type == "User":
                return await adapter.search_user(search_query, top=10)
            else:
                return await adapter.search_group(search_query, top=10)

        run_python = manual_backend in ("Both (compare)", "Custom Python")
        run_mcp = manual_backend in ("Both (compare)", "MCP Server")

        python_results, mcp_results = None, None
        python_ms, mcp_ms = 0.0, 0.0

        if run_python:
            try:
                t0 = _time.perf_counter()
                python_results = _aio.run(_run_python_search())
                python_ms = (_time.perf_counter() - t0) * 1000
            except Exception as e:
                st.error(f"Python search failed: {e}")

        if run_mcp:
            try:
                t0 = _time.perf_counter()
                mcp_results = _aio.run(_run_mcp_search())
                mcp_ms = (_time.perf_counter() - t0) * 1000
            except Exception as e:
                st.error(f"MCP search failed: {e}")

        # ── Display results ──────────────────────────────────────────
        if run_python and run_mcp:
            # Side-by-side comparison
            cmp_col1, cmp_col2 = st.columns(2)
            with cmp_col1:
                st.markdown(f"**🐍 Custom Python** — {len(python_results or [])} result(s) in {python_ms:.0f} ms")
                if python_results:
                    st.dataframe(python_results, use_container_width=True)
            with cmp_col2:
                st.markdown(f"**🔌 MCP Server** — {len(mcp_results or [])} result(s) in {mcp_ms:.0f} ms")
                if mcp_results:
                    st.dataframe(mcp_results, use_container_width=True)

            # Quick comparison summary
            if python_results and mcp_results:
                py_ids = [r.get("id") or r.get("GroupID") for r in python_results]
                mcp_ids = [r.get("id") or r.get("GroupID") for r in mcp_results]
                if py_ids == mcp_ids:
                    st.success("✅ Results match — same documents in same order")
                    py_scores = [r.get("_match_score") for r in python_results]
                    mcp_scores = [r.get("_match_score") for r in mcp_results]
                    if py_scores == mcp_scores:
                        st.success("✅ Scores identical")
                    else:
                        st.warning("⚠️ Same order but scores differ (possible floating-point variance)")
                else:
                    st.warning("⚠️ Results differ — different documents or ordering")

                delta_ms = abs(python_ms - mcp_ms)
                faster = "Python" if python_ms < mcp_ms else "MCP"
                st.info(f"⏱️ {faster} was faster by {delta_ms:.0f} ms  (Python: {python_ms:.0f} ms, MCP: {mcp_ms:.0f} ms)")
        else:
            # Single backend
            results = python_results if run_python else mcp_results
            ms = python_ms if run_python else mcp_ms
            label = "🐍 Custom Python" if run_python else "🔌 MCP Server"
            st.success(f"{label} — {len(results or [])} result(s) in {ms:.0f} ms")
            if results:
                st.dataframe(results, use_container_width=True)

    # ── Auto-refresh while session is active ────────────────────────────
    # Refresh when connected OR when the agent thread is still running
    # (covers the initial connection window before state.connected=True)
    agent_thread = st.session_state.get("agent_thread")
    thread_alive = agent_thread is not None and agent_thread.is_alive()
    if state.connected or thread_alive:
        time.sleep(1.5)
        st.rerun()


if __name__ == "__main__":
    render_ui()
