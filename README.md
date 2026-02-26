# SIRE Voice Agent

Real-time voice assistant that uses the **Azure VoiceLive SDK** for speech I/O and **Azure AI Search** for entity resolution. A user speaks naturally, and the agent identifies their intent, searches for the matching person or hospital group, and confirms the result — all by voice.

## Architecture

```
                          ┌──────────────────────────────────┐
  Microphone (24 kHz)     │      Azure AI Foundry            │
  ──────────────────────► │  gpt-realtime  (function calling) │
                          │                                    │
                          │  1. STT  →  intent + entity        │
                          │  2. Tool call  →  search_user /    │
                          │                   search_group     │
                          │  3. Review results  →  TTS         │
                          └────────────┬───────────────────────┘
                                       │  REST
                          ┌────────────▼───────────────────────┐
                          │      Azure AI Search               │
                          │  ┌─────────────────────────────┐   │
                          │  │ user-slot-mapping-index      │   │
                          │  │ group-slot-mapping-index     │   │
                          │  └─────────────────────────────┘   │
                          └────────────┬───────────────────────┘
                                       │
                          ┌────────────▼───────────────────────┐
                          │  Multi-Strategy Search + RRF       │
                          │  exact · fuzzy · phonetic ·        │
                          │  normalised · field-targeted       │
                          │  → Reciprocal Rank Fusion (K=10)   │
                          │  → Confidence auto-confirm         │
                          └────────────────────────────────────┘
```

### Conversation Flow

1. **Voice Input** — user speaks naturally via microphone (24 kHz PCM16)
2. **Intent + Entity Extraction** — the realtime model extracts the action verb (e.g. *call*, *log in*, *page*) and the entity name via function-calling tools
3. **Multi-Strategy Search** — the tool queries Azure AI Search using multiple strategies in parallel and fuses results with RRF scoring
4. **Confidence Check** — if the top result's RRF score ≥ 70 *and* the gap to #2 ≥ 25 pts, the result is auto-confirmed; otherwise the model reads the top candidates and asks the user to disambiguate
5. **Verification** — the model reads back the matched name + ID and waits for user confirmation

---

## Multi-Strategy Search

Each query is run through **multiple search strategies** simultaneously, and the results are merged using **Reciprocal Rank Fusion (RRF)** with K = 10.

### Strategies

| Strategy | Weight | Description |
|----------|--------|-------------|
| **Field-targeted AND** | 2.0 | Splits multi-word input into `FirstName:X AND LastName:Y` |
| **Exact (all terms)** | 1.0 | `searchMode: all` — every word must match |
| **Exact (any term)** | 0.5 | `searchMode: any` fallback |
| **Normalised** | 0.85 | Number words → digits ("two" → "2") before searching |
| **Fuzzy** | 0.70 | Levenshtein edit-distance `~1` / `~2` depending on word length |
| **Phonetic (Double Metaphone)** | 0.50 | Searches `*_phonetic_dm` fields |
| **Phonetic (Soundex)** | 0.40 | Searches `*_phonetic_soundex` fields |
| **Phonetic (Beider-Morse)** | 0.35 | Searches `*_phonetic_bm` fields |

### Confidence Detection

After RRF scoring, results are normalised to 0–100. A result is flagged `_confident = true` when:

- **Score ≥ 70** *and*
- **Gap to next result ≥ 25 points**

The `_confident` flag is passed to the model so it can auto-confirm high-confidence matches or read alternatives for disambiguation.

---

## Indexes

| Index | Key Fields | Custom Analyzers |
|-------|------------|-----------------|
| `user-slot-mapping-index` | UserID, FirstName, LastName, FullName, ASN1–4 | `user_name_analyzer`, Double Metaphone, Soundex, Beider-Morse phonetic fields |
| `group-slot-mapping-index` | GroupID, GroupName, AlternateName1–3 | `hospital_group_analyzer` (char mapping, shingle, edge n-gram), normalised name fields |

---

## Running the App

### Prerequisites

- Python 3.11+
- Microphone + speakers (configure device indices in `.env`)
- Azure AI Foundry resource with a **`gpt-realtime`** deployment
- Azure AI Search with `group-slot-mapping-index` and `user-slot-mapping-index`
- PortAudio (macOS only: `brew install portaudio`)

### Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment — edit .env with your endpoints and keys
#    (see Environment Variables below)

# 3. Login to Azure (for Entra ID / token-based auth)
az login
```

### CLI Mode (terminal)

```bash
python main.py --use-token-credential          # Entra ID auth
python main.py --use-token-credential --verbose # with DEBUG logging
```

### Streamlit UI

```bash
streamlit run streamlit_app.py
```

The Streamlit app provides:

- **START / STOP** session controls
- **Live transcript** panel (user + assistant turns)
- **Intent & Best Match** panel — shows the extracted intent, entity, top result with score, and whether it was auto-confirmed or needs disambiguation
- **Search results** table with RRF scores and contributing strategies
- **Manual search test** — run ad-hoc queries against either index without a voice session

---

## Environment Variables

Create a `.env` file in the project root:

```dotenv
# Azure AI Foundry (VoiceLive)
AZURE_VOICELIVE_ENDPOINT=https://<your-foundry>.services.ai.azure.com/
AZURE_VOICELIVE_MODEL=gpt-realtime
AZURE_VOICELIVE_USE_TOKEN=true
# AZURE_VOICELIVE_API_KEY=...        # alternative to token auth

# Azure AI Search
AZURE_SEARCH_ENDPOINT=https://<your-search>.search.windows.net
AZURE_SEARCH_API_KEY=<key>
AZURE_SEARCH_GROUP_INDEX=group-slot-mapping-index
AZURE_SEARCH_USER_INDEX=user-slot-mapping-index

# Audio devices (run `python -c "import pyaudio; p=pyaudio.PyAudio(); [print(i,p.get_device_info_by_index(i)['name']) for i in range(p.get_device_count())]"` to list)
AUDIO_INPUT_DEVICE_INDEX=2
AUDIO_OUTPUT_DEVICE_INDEX=4
```

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | Voice agent entry point — VoiceLive session, event loop, function-call dispatch with intent extraction |
| `search_client.py` | Async multi-strategy Azure AI Search client with RRF scoring and confidence detection |
| `config.py` | Typed dataclass configuration loaded from `.env` |
| `streamlit_app.py` | Streamlit web UI — session controls, live transcript, search results, manual test |
| `export_indexes.py` | Export top records from each AI Search index to `SIRE_AI_Search_Data.xlsx` |
| `test_enhanced_search.py` | Test harness for multi-strategy search with sample queries |
| `setup_mcp_ai_search.ps1` | PowerShell script to configure the MCP AI Search connector |
| `.env` | Environment variables (not committed) |
| `requirements.txt` | Python dependencies |

---

## CLI Options

```
python main.py [--use-token-credential] [--verbose]
```

| Flag | Description |
|------|-------------|
| `--use-token-credential` | Use `AzureCliCredential` (Entra ID) instead of API key |
| `--verbose` | Enable DEBUG logging |
