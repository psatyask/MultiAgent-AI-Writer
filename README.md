# MultiAgent-AI-Writer

A multi-agent technical blog generator built with **LangGraph**. Give it a topic and a team of agents decides whether research is needed, searches the web, plans the article, writes every section in parallel, then merges the draft and generates diagrams with Gemini. Progress streams live to a **FastAPI + Bootstrap** web UI, and every run is checkpointed in **PostgreSQL**.

---

## Features

- Router agent decides whether the topic needs live web research
- Tavily-powered research with a structured evidence pack
- Orchestrator produces a section-by-section plan
- Parallel worker agents write sections at the same time (LangGraph `Send` fan-out)
- Reducer subgraph merges sections, decides where images help, and generates them
- AI-generated figures via Google Gemini image models, with automatic model fallback
- Live progress over Server-Sent Events (SSE)
- Download the article as `.md`, and the images as a single file or ZIP
- State checkpointed in PostgreSQL (Render)

---

## Architecture

```
                    ┌────────────────────────────┐
                    │  Browser (Bootstrap UI)    │
                    │  index.html / app.js / css │
                    └──────────────┬─────────────┘
                                   │  POST /api/run  (SSE stream back)
                    ┌──────────────▼─────────────┐
                    │  FastAPI  (app.py)         │
                    │  streams LangGraph updates │
                    └──────────────┬─────────────┘
                                   │  workflow.stream(...)
                    ┌──────────────▼─────────────┐
                    │  LangGraph  (backend.py)   │──── checkpoints ───▶  PostgreSQL
                    └───┬────────────┬───────┬───┘
                        │            │       │
                  OpenAI LLM      Tavily   Gemini
                (plan / write)   (search)  (images)
```

| Layer | File | Responsibility |
|---|---|---|
| Frontend | `templates/index.html`, `static/css/style.css`, `static/js/app.js` | Topic input, live timeline, plan view, article preview, downloads |
| API | `app.py` | Runs the graph, converts graph updates into SSE events, serves downloads |
| Agents | `backend.py` | The LangGraph workflow, models, tools and checkpointer |

---

## Workflow

```
START
  │
  ▼
router ──needs research?──┬── yes ──▶ research ──┐
                          └── no ────────────────┤
                                                 ▼
                                           orchestrator
                                                 │  fan-out: one Send per task
                              ┌──────────┬───────┴───┬──────────┐
                              ▼          ▼           ▼          ▼
                           worker 1   worker 2    worker 3   worker N   (parallel)
                              └──────────┴───────┬───┴──────────┘
                                                 ▼
                                      reducer (subgraph)
                          merge_content → decide_images → generate_and_place_images
                                                 │
                                                 ▼
                                                END
```

### 1. Router
Reads the topic and returns a `RouterDecision`: `needs_research`, a `mode` and, when research is needed, 3-10 specific search queries.

| Mode | Research | Used for |
|---|---|---|
| `closed_book` | No | Evergreen topics (concepts, fundamentals) |
| `hybrid` | Yes | Evergreen topics that need up-to-date examples, tools or models |
| `open_book` | Yes | Volatile topics: "latest", weekly roundups, pricing, policy |

`route_next` sends the graph to `research` or straight to `orchestrator`.

### 2. Research (Tavily)
Runs each query through `langchain_tavily.TavilySearch` (6 results per query), normalises the hits, then asks the LLM to condense them into an `EvidencePack` of `EvidenceItem`s (title, URL, snippet, `published_at`, source). Items without a URL are dropped, duplicates are removed by URL, and dates are never guessed. Workers may only cite this evidence.

### 3. Orchestrator
Produces a `Plan`: `blog_title`, `audience`, `tone`, `blog_kind` (`explainer`, `tutorial`, `news_roundup`, `comparison` or `system_design`), `constraints` and 5-9 `Task`s. Each `Task` has an id, title, one-sentence `goal`, 3-6 `bullets`, a `target_words` count (120-550), `tags`, and the flags `requires_research`, `requires_citations` and `requires_code`. In `open_book` mode the plan is forced to `news_roundup`.

### 4. Workers (parallel fan-out)
`fanout` returns one `Send("worker", ...)` per task, so all sections are written at once. Each worker receives its task, the plan, the mode and the evidence, and writes one section as Markdown starting with `## <Section Title>`. In `open_book` mode a worker may only state event claims backed by an Evidence URL and cites them as `([Source](URL))`. Results are appended to `sections` as `(task_id, markdown)` tuples using an `operator.add` reducer, and the single `worker -> reducer` edge waits for all of them.

### 5. Reducer subgraph
A nested LangGraph subgraph with three steps:

1. **`merge_content`** – sorts sections by task id and joins them into one Markdown document under the blog title.
2. **`decide_images`** – the LLM returns a `GlobalImagePlan`: the Markdown with `[[IMAGE_1]]`-style placeholders plus 1-3 `ImageSpec`s (placeholder, filename, alt, caption, prompt, size, quality). Three guards keep it reliable: if the returned Markdown looks truncated (under 80% of the original) the original is used; if no images come back a default diagram is added; and any missing placeholder is inserted after a `##` section.
3. **`generate_and_place_images`** – calls Gemini for each spec, saves the file to `images/`, and replaces the placeholder with `![alt](images/<file>)` plus an italic caption. Images that already exist are reused. If an image fails, its placeholder becomes an `[IMAGE GENERATION FAILED]` block containing the prompt and error, so the article still completes. The final Markdown is also written to `outputs/<blog title>.md`.

### 6. Streaming to the UI
`app.py` runs the graph with `stream_mode="updates"` and `subgraphs=True`, and turns each node update into an SSE event: `run_started`, `stage`, `substage`, `routing`, `research_complete`, `plan`, `section_complete`, `images_planned`, `warning`, `final`, `error`, `done`.

---

## Models and Tools

| Purpose | Model / tool | Notes |
|---|---|---|
| Routing, evidence extraction, planning, writing, image planning | OpenAI via `langchain_openai.ChatOpenAI` (`gpt-6.1-terra`, 2 retries) | Uses structured output (Pydantic schemas) for `RouterDecision`, `EvidencePack`, `Plan`, `GlobalImagePlan`; workers use plain text output |
| Web research | Tavily (`langchain_tavily.TavilySearch`) | Returns a dict with a `results` list |
| Image generation | Google Gemini via `google-genai` | Tried in order: `gemini-3.1-flash-image` → `gemini-3.1-flash-lite-image` → `gemini-2.5-flash-image` |

Image generation details:

- Requests use `response_modalities=["TEXT", "IMAGE"]` and a 90-second HTTP timeout.
- Each model gets one retry (after 5 seconds) for temporary errors such as 429, 503 or timeouts, then the next model is tried. If all fail, every error is reported.
- Filenames are sanitised (Windows-safe) and suffixed with a short hash of the prompt, so two different images never overwrite each other.
- Images are saved in `images/` and referenced in the article as `images/<name>.png`.

---

## Database: PostgreSQL

PostgreSQL (hosted on Render) stores **LangGraph checkpoints** through `PostgresSaver`.

- Every node writes its state to the database, so a run's full state can be inspected or resumed by `thread_id`.
- Each run uses a fresh `thread_id` (`run-<id>`), so runs never overwrite each other.
- Tables are created automatically by `checkpointer.setup()`.

Connection handling (important on free tiers, which close idle connections):

```python
pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1, max_size=5, max_idle=60,
    check=ConnectionPool.check_connection,   # replaces dead connections before use
    kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0,
            "keepalives": 1, "keepalives_idle": 30,
            "keepalives_interval": 10, "keepalives_count": 5,
            "connect_timeout": 10},
)
checkpointer = PostgresSaver(pool)
```

Use the **External Database URL** from Render. Some public Wi-Fi networks block port 5432; use a hotspot or VPN if the connection times out.

---

## Project Structure

```
blog_app/
├── app.py                 # FastAPI server + SSE streaming + download endpoints
├── backend.py             # LangGraph agents, models, tools, checkpointer
├── templates/
│   └── index.html         # Bootstrap 5 UI
├── static/
│   ├── css/style.css      # Ember & Ink theme
│   └── js/app.js          # SSE client, timeline, plan, preview, downloads
├── images/                # Generated images (created at runtime)
├── outputs/               # Saved Markdown per run (created at runtime)
├── .env                   # API keys (not committed)
└── README.md
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Web UI |
| GET | `/api/health` | Health check |
| POST | `/api/run` | Start a run; streams SSE events |
| GET | `/api/runs/{run_id}/download` | Download the article as Markdown |
| GET | `/api/runs/{run_id}/images` | Download the article's images (one file, or a ZIP) |

---

## Setup

### 1. Install

```bash
git clone https://github.com/psatyask/MultiAgent-AI-Writer.git
cd MultiAgent-AI-Writer
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install fastapi uvicorn jinja2 python-dotenv pydantic \
            langgraph langgraph-checkpoint-postgres \
            langchain-openai langchain-tavily langchain-google-genai \
            "psycopg[binary,pool]" google-genai
```

### 2. Configure `.env`

```env
OPENAI_API_KEY=your_openai_key
TAVILY_API_KEY=your_tavily_key
GOOGLE_API_KEY=your_google_ai_studio_key
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

- `GOOGLE_API_KEY` comes from Google AI Studio. Image generation models need a billing-enabled key.
- `DATABASE_URL` is the External Database URL from Render.

### 3. Run

```bash
python app.py
```

Open http://127.0.0.1:8000.

You can also test the graph without the UI by running `python backend.py`.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Connection to Postgres times out | Port 5432 may be blocked on your network. Use a hotspot or VPN. |
| `the connection is closed` | Already handled by the pooled connection with health checks and keepalives. |
| Images show `[IMAGE GENERATION FAILED]` | Check the server console for `IMAGE ERROR` lines; confirm `GOOGLE_API_KEY` has billing enabled. |
| `Plan is not fully defined` | Import `Literal`, `List`, `Optional` and do not use `from __future__ import annotations` in `backend.py`. |
| Tavily `'str' object has no attribute 'get'` | Read results from `resp["results"]`, as `_tavily_search` does. |

---

## Tech Stack

LangGraph · LangChain · OpenAI · Tavily · Google Gemini · PostgreSQL (Render) · FastAPI · Server-Sent Events · Bootstrap 5 · Bootstrap Icons · marked + DOMPurify