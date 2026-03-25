# SIRE Voice Agent

Real-time voice assistant that uses the **Azure VoiceLive SDK** for speech I/O and **Azure AI Search** for entity resolution. A user speaks naturally, and the agent identifies their intent, searches for the matching person or hospital group, and confirms the result — all by voice.

## Architecture

```
                          ┌──────────────────────────────────┐
  Microphone (24 kHz)     │      Microsoft Foundry            │
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

The search pipeline runs **multiple query strategies in parallel** against a single Azure AI Search index, then fuses the ranked results using **Reciprocal Rank Fusion (RRF)**. The realtime model handles **entity extraction via function-calling** and routes to the correct index — only one index is searched per tool call.

### Entity Routing

The model extracts the user's intent and entity type, then calls the appropriate search tool:

| User Says | Model Extracts | Tool Called | Index Searched |
|-----------|---------------|-------------|----------------|
| "Call Nick Stewart" | action=call, name="Nick Stewart" | `search_user` | `user-slot-mapping-index` |
| "Log in to St. Mary's Group 3" | action=login, group="St. Mary's Group 3" | `search_group` | `group-slot-mapping-index` |
| "Page Dr. Johnson" | action=page, name="Dr. Johnson" | `search_user` | `user-slot-mapping-index` |

There is **no cross-index search** — the model decides the target based on conversational context.

### User Index Strategies (up to 8)

Optimised for **person name resolution** — handles STT pronunciation errors, misspellings, and partial names.

| # | Strategy | Query Type | Weight | Description |
|---|----------|-----------|--------|-------------|
| 1 | **exact** | simple | 1.0 | Standard BM25 text search on `FirstName`, `LastName`, `FullName` |
| 2 | **fuzzy** | full Lucene | 0.70 | `~2` edit distance for words ≥ 4 chars, `~1` otherwise — catches STT typos |
| 3 | **targeted** | full Lucene | 2.0 | Field-targeted AND: `FirstName:X AND LastName:Y` — strongest signal when both names match (multi-word queries only) |
| 4 | **targeted-fuzzy** | full Lucene | 1.6 | Fuzzy field-targeted AND — combines edit-distance with structured matching |
| 5 | **targeted-phonetic** | full Lucene | 1.2 | Phonetic field-targeted AND using Double Metaphone fields |
| 6 | **phonetic-dm** | simple | 0.50 | Double Metaphone — best general-purpose phonetic algorithm, produces two encodings per name |
| 7 | **phonetic-soundex** | simple | 0.40 | Soundex — classic algorithm, effective for common English surname confusion ("Smith"/"Smyth") |
| 8 | **phonetic-bm** | simple | 0.35 | Beider-Morse — cross-language name matching (Germanic, Slavic, Romance origins), higher false-positive rate |

**Why phonetic search for users?** STT commonly garbles proper names ("Nguyen" → "Win", "Karen" → "Darren"). No single phonetic algorithm covers all name origins, so all three run in parallel with decreasing weights reflecting their accuracy.

**Why field-targeted AND gets the highest weight (2.0)?** Matching *both* first and last name is the single strongest signal for person resolution.

### Group Index Strategies (7)

Optimised for **site/group name resolution** — handles number format confusion, abbreviations, and common word variations.

| # | Strategy | Query Type | Weight | Description |
|---|----------|-----------|--------|-------------|
| 1 | **exact-all** | simple, `searchMode=all` | 1.0 | All query terms must match — highest precision |
| 2 | **exact-any** | simple, `searchMode=any` | 0.5 | Partial term match fallback |
| 3 | **fuzzy** | full Lucene `~1` | 0.70 | 1-edit-distance for spelling errors |
| 4 | **normalised** | simple, `searchMode=all` | 0.85 | Number↔word conversion (`"3"` → `"three"`, `"two"` → `"2"`) before searching |
| 5 | **normalised-any** | simple, `searchMode=any` | 0.51 | Normalised query with partial matching |
| 6 | **fuzzy-norm** | full Lucene `~1` | 0.63 | Fuzzy search on the normalised query — combines both strategies |
| 7 | **norm-fields** | simple, `searchMode=all` | 0.90 | Searches `*_normalized` fields with custom analyzers (`keyword_lowercase` + char mappings like `&`→`and`, `st`→`saint`) |

**Why no phonetic search for groups?** Group names are multi-word phrases with common nouns ("hospital", "medical", "center") — phonetic encoding of these words would match too broadly. The main confusion source for sites spoken aloud is **number format** ("three" vs "3") and **abbreviation** ("St." vs "Saint"), which the normalisation strategies handle precisely.

### Strategy Comparison

| Aspect | User Index | Group Index |
|--------|-----------|-------------|
| **Strategies** | Up to 8 | 7 |
| **Phonetic search** | 3 algorithms (DM, Soundex, Beider-Morse) | None |
| **Number normalisation** | None | 3 variants + normalised-field analyzer |
| **Field-targeted AND** | Yes (weight 2.0) | No (group names don't have first/last structure) |
| **Highest weight** | Field-targeted AND (2.0) | Exact-all (1.0) and norm-fields (0.9) |
| **Fuzzy edit distance** | `~2` for words ≥ 4 chars, `~1` otherwise | `~1` only |

---

## Reciprocal Rank Fusion (RRF)

All strategies — keyword, fuzzy, phonetic, normalised, and field-targeted — are fused into a single ranking using **weighted RRF**. This is a custom client-side implementation in `search_client.py`, distinct from Azure's built-in semantic ranker.

### How It Works

**Step 1 — Parallel execution:** All strategies fire as separate HTTP requests to Azure AI Search simultaneously. Each returns an independently BM25-scored ranked list.

**Step 2 — RRF scoring:** For every unique document that appears in any strategy's results:

```
RRF_score = Σ  weight_i / (K + rank_i)
```

Where:
- `K = 10` — smoothing constant (low K = sharper rank sensitivity; rank #1 vs #2 matters more)
- `rank_i` — the document's 1-based position in strategy `i`'s results
- `weight_i` — the strategy's configured weight (e.g. 2.0 for field-targeted AND, 0.35 for Beider-Morse)
- If a document didn't appear in a strategy, that strategy is excluded from the sum

**Step 3 — Normalisation:** Raw RRF scores are mapped to 0–100:

```
normalised_score = (raw_score / max_raw_score) × 100
```

The top result always scores 100; all others are scaled proportionally.

### Worked Example

Query: **"Nick Stewart"** against the user index

| Strategy | Weight | "Nick Stewart" Rank | RRF Contribution |
|----------|--------|-------------------|------------------|
| Field-targeted AND | 2.0 | #1 | 2.0 / (10 + 1) = 0.182 |
| Exact | 1.0 | #1 | 1.0 / (10 + 1) = 0.091 |
| Fuzzy | 0.7 | #2 | 0.7 / (10 + 2) = 0.058 |
| Phonetic DM | 0.5 | #1 | 0.5 / (10 + 1) = 0.045 |
| Phonetic Soundex | 0.4 | #3 | 0.4 / (10 + 3) = 0.031 |
| Phonetic BM | 0.35 | #1 | 0.35 / (10 + 1) = 0.032 |
| **Raw RRF total** | | | **0.439** |

A correct match surfaces across **many strategies** and in **high positions**, accumulating a large RRF score. Noise results typically appear in only one or two strategies, scoring much lower.

### Why Custom RRF Instead of Azure's Semantic Ranker?

| Aspect | Custom Client-Side RRF | Azure Semantic Ranker |
|--------|------------------------|----------------------|
| **What it does** | Fuses N ranked lists from different query formulations | Reranks a single BM25 list using a cross-encoder transformer |
| **Best for** | Short structured fields (names, codes), STT-corrupted input, entity matching | Long-form natural language text, varied phrasings |
| **Requires vectors?** | No | No (independent of vector search) |
| **Latency** | 1 search round-trip (~50–100ms, parallel) | +200–400ms on top of keyword search |
| **Tuning** | Per-strategy weights, configurable K | No domain-specific tuning knobs |
| **Why not semantic ranker here?** | "Nick Stewart" vs "Nic Stuart" is a **lexical variation** problem, not a semantic similarity problem — phonetic + fuzzy strategies catch it directly; a transformer model has no advantage on short name strings |

### Confidence Detection

After RRF scoring and normalisation, a result is flagged `_confident = true` when:

- **Score ≥ 70** *and*
- **Gap to next result ≥ 25 points**

The `_confident` flag tells the voice model whether to **auto-confirm** the top match or **read back candidates** and ask the user to disambiguate. This avoids costly disambiguation exchanges when the result is unambiguous, while preventing incorrect auto-confirms when multiple similar entities exist.

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
- Microsoft Foundry resource with a **`gpt-realtime`** deployment
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
