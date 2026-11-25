from pathlib import Path
from datetime import date
import hashlib
import operator
import os
import re
import time
import uuid
import traceback
from typing import TypedDict, List, Annotated, Optional, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langgraph.checkpoint.postgres import PostgresSaver

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_tavily import TavilySearch
from langchain_google_genai import ChatGoogleGenerativeAI

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

load_dotenv()  # Load environment variables from .env file

# Anchor output folders to this file, not to the current working directory,
# so app.py serves exactly the folder this file writes to.
BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
OUTPUTS_DIR = BASE_DIR / "outputs"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

image_llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-image",
    response_modalities=["TEXT", "IMAGE"],
)


def get_database_url():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError(
            "DATABASE_URL is missing. Please add your Render PostgreSQL External Database URL to .env"
        )

    if "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{separator}sslmode=require"

    return database_url


# -----------------------------
# 1) Schemas
# -----------------------------
class Task(BaseModel):
    id: int
    title: str

    goal: str = Field(
        ...,
        description="One sentence describing what the reader should be able to do/understand after this section.",
    )
    bullets: List[str] = Field(
        ...,
        min_length=3,
        max_length=6,
        description="3-6 concrete, non-overlapping subpoints to cover in this section.",
    )
    target_words: int = Field(..., description="Target word count for this section (120-550).")

    tags: List[str] = Field(default_factory=list)
    requires_research: bool = False
    requires_citations: bool = False
    requires_code: bool = False


class Plan(BaseModel):
    blog_title: str
    audience: str
    tone: str
    blog_kind: Literal["explainer", "tutorial", "news_roundup", "comparison", "system_design"] = "explainer"
    constraints: List[str] = Field(default_factory=list)
    tasks: List[Task]


class EvidenceItem(BaseModel):
    title: str
    url: str
    published_at: Optional[str] = None  # keep if Tavily provides; DO NOT rely on it
    snippet: Optional[str] = None
    source: Optional[str] = None


class RouterDecision(BaseModel):
    needs_research: bool
    mode: Literal["closed_book", "hybrid", "open_book"]
    queries: List[str] = Field(default_factory=list)


class EvidencePack(BaseModel):
    evidence: List[EvidenceItem] = Field(default_factory=list)


class ImageSpec(BaseModel):
    placeholder: str = Field(..., description="e.g. [[IMAGE_1]]")
    filename: str = Field(..., description="Save under images/, e.g. qkv_flow.png")
    alt: str
    caption: str
    prompt: str = Field(..., description="Prompt to send to the image model.")
    size: Literal["1024x1024", "1024x1536", "1536x1024"] = "1024x1024"
    quality: Literal["low", "medium", "high"] = "medium"


class GlobalImagePlan(BaseModel):
    md_with_placeholders: str
    images: List[ImageSpec] = Field(default_factory=list)


class State(TypedDict):
    topic: str
    as_of: str
    recency_days: int

    # routing / research
    mode: str
    needs_research: bool
    queries: List[str]
    evidence: List[EvidenceItem]
    plan: Optional[Plan]

    # workers
    sections: Annotated[List[tuple[int, str]], operator.add]  # (task_id, section_md)

    # reducer/image
    merged_md: str
    md_with_placeholders: str
    image_specs: List[dict]

    final: str


# -----------------------------
# 2) LLM
# -----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY is missing. Please add it to your .env file.")

llm = ChatOpenAI(
    model="gpt-6.1-terra",
    api_key=OPENAI_API_KEY,
    max_retries=2,
)


# -----------------------------
# 3) Router (decide upfront)
# -----------------------------
ROUTER_SYSTEM = """You are a routing module for a technical blog planner.

Decide whether web research is needed BEFORE planning.

Modes:
- closed_book (needs_research=false):
  Evergreen topics where correctness does not depend on recent facts (concepts, fundamentals).
- hybrid (needs_research=true):
  Mostly evergreen but needs up-to-date examples/tools/models to be useful.
- open_book (needs_research=true):
  Mostly volatile: weekly roundups, "this week", "latest", rankings, pricing, policy/regulation.

If needs_research=true:
- Output 3-10 high-signal queries.
- Queries should be scoped and specific (avoid generic queries like just "AI" or "LLM").
- If user asked for "last week/this week/latest", reflect that constraint IN THE QUERIES.
"""


def router_node(state: State) -> dict:
    topic = state["topic"]
    decider = llm.with_structured_output(RouterDecision)
    decision = decider.invoke(
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(content=f"Topic: {topic}"),
        ]
    )

    return {
        "needs_research": decision.needs_research,
        "mode": decision.mode,
        "queries": decision.queries,
    }


def route_next(state: State) -> str:
    return "research" if state["needs_research"] else "orchestrator"


# -----------------------------
# 4) Research (Tavily)
# -----------------------------
def _tavily_search(query: str, max_results: int = 5) -> List[dict]:
    tool = TavilySearch(max_results=max_results)
    resp = tool.invoke({"query": query})

    # TavilySearch returns {"query": ..., "results": [...], ...}
    results = resp.get("results", []) if isinstance(resp, dict) else []
    if not results:
        print("Tavily returned no results:", str(resp)[:200])

    normalized: List[dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        normalized.append(
            {
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "snippet": (r.get("content") or r.get("snippet") or "")[:500],
                "published_at": r.get("published_date") or r.get("published_at"),
                "source": r.get("source"),
            }
        )
    return normalized


RESEARCH_SYSTEM = """You are a research synthesizer for technical writing.

Given raw web search results, produce a deduplicated list of EvidenceItem objects.

Rules:
- Only include items with a non-empty url.
- Prefer relevant + authoritative sources (company blogs, docs, reputable outlets).
- If a published date is explicitly present in the result payload, keep it as YYYY-MM-DD.
  If missing or unclear, set published_at=null. Do NOT guess.
- Keep snippets short.
- Deduplicate by URL.
"""


def research_node(state: State) -> dict:
    queries = (state.get("queries", []) or [])
    max_results = 6

    raw_results: List[dict] = []

    for q in queries:
        raw_results.extend(_tavily_search(q, max_results=max_results))

    if not raw_results:
        return {"evidence": []}

    extractor = llm.with_structured_output(EvidencePack)
    pack = extractor.invoke(
        [
            SystemMessage(content=RESEARCH_SYSTEM),
            HumanMessage(content=f"Raw results:\n{raw_results}"),
        ]
    )

    # Deduplicate by URL
    dedup = {}
    for e in pack.evidence:
        if e.url:
            dedup[e.url] = e

    return {"evidence": list(dedup.values())}


# -----------------------------
# 5) Orchestrator (Plan)
# -----------------------------
ORCH_SYSTEM = """You are a senior technical writer and developer advocate.
Your job is to produce a highly actionable outline for a technical blog post.

Hard requirements:
- Create 5-9 sections (tasks) suitable for the topic and audience.
- Each task must include:
  1) goal (1 sentence)
  2) 3-6 bullets that are concrete, specific, and non-overlapping
  3) target word count (120-550)

Quality bar:
- Assume the reader is a developer; use correct terminology.
- Bullets must be actionable: build/compare/measure/verify/debug.
- Ensure the overall plan includes at least 2 of these somewhere:
  * minimal code sketch / MWE (set requires_code=True for that section)
  * edge cases / failure modes
  * performance/cost considerations
  * security/privacy considerations (if relevant)
  * debugging/observability tips

Grounding rules:
- Mode closed_book: keep it evergreen; do not depend on evidence.
- Mode hybrid:
  - Use evidence for up-to-date examples (models/tools/releases) in bullets.
  - Mark sections using fresh info as requires_research=True and requires_citations=True.
- Mode open_book:
  - Set blog_kind = "news_roundup".
  - Every section is about summarizing events + implications.
  - DO NOT include tutorial/how-to sections unless user explicitly asked for that.
  - If evidence is empty or insufficient, create a plan that transparently says "insufficient sources"
    and includes only what can be supported.

Output must strictly match the Plan schema.
"""


def orchestrator_node(state: State) -> dict:
    planner = llm.with_structured_output(Plan)

    evidence = state.get("evidence", [])
    mode = state.get("mode", "closed_book")

    plan = planner.invoke(
        [
            SystemMessage(content=ORCH_SYSTEM),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Mode: {mode}\n\n"
                    f"Evidence (ONLY use for fresh claims; may be empty):\n"
                    f"{[e.model_dump() for e in evidence][:16]}"
                )
            ),
        ]
    )

    return {"plan": plan}


# -----------------------------
# 6) Fanout
# -----------------------------
def fanout(state: State):
    return [
        Send(
            "worker",
            {
                "task": task.model_dump(),
                "topic": state["topic"],
                "mode": state["mode"],
                "plan": state["plan"].model_dump(),
                "evidence": [e.model_dump() for e in state.get("evidence", [])],
            },
        )
        for task in state["plan"].tasks
    ]


# -----------------------------
# 7) Worker (write one section)
# -----------------------------
WORKER_SYSTEM = """You are a senior technical writer and developer advocate.
Write ONE section of a technical blog post in Markdown.

Hard constraints:
- Follow the provided Goal and cover ALL Bullets in order (do not skip or merge bullets).
- Stay close to Target words (+/-15%).
- Output ONLY the section content in Markdown (no blog title H1, no extra commentary).
- Start with a '## <Section Title>' heading.

Scope guard:
- If blog_kind == "news_roundup": do NOT turn this into a tutorial/how-to guide.
  Do NOT teach web scraping, RSS, automation, or "how to fetch news" unless bullets explicitly ask for it.
  Focus on summarizing events and implications.

Grounding policy:
- If mode == open_book:
  - Do NOT introduce any specific event/company/model/funding/policy claim unless it is supported by provided Evidence URLs.
  - For each event claim, attach a source as a Markdown link: ([Source](URL)).
  - Only use URLs provided in Evidence. If not supported, write: "Not found in provided sources."
- If requires_citations == true:
  - For outside-world claims, cite Evidence URLs the same way.
- Evergreen reasoning is OK without citations unless requires_citations is true.

Code:
- If requires_code == true, include at least one minimal, correct code snippet relevant to the bullets.

Style:
- Short paragraphs, bullets where helpful, code fences for code.
- Avoid fluff/marketing. Be precise and implementation-oriented.
"""


def worker_node(payload: dict) -> dict:
    task = Task(**payload["task"])
    plan = Plan(**payload["plan"])
    evidence = [EvidenceItem(**e) for e in payload.get("evidence", [])]
    topic = payload["topic"]
    mode = payload.get("mode", "closed_book")

    bullets_text = "\n- " + "\n- ".join(task.bullets)

    evidence_text = ""
    if evidence:
        evidence_text = "\n".join(
            f"- {e.title} | {e.url} | {e.published_at or 'date:unknown'}".strip()
            for e in evidence[:20]
        )

    section_md = llm.invoke(
        [
            SystemMessage(content=WORKER_SYSTEM),
            HumanMessage(
                content=(
                    f"Blog title: {plan.blog_title}\n"
                    f"Audience: {plan.audience}\n"
                    f"Tone: {plan.tone}\n"
                    f"Blog kind: {plan.blog_kind}\n"
                    f"Constraints: {plan.constraints}\n"
                    f"Topic: {topic}\n"
                    f"Mode: {mode}\n\n"
                    f"Section title: {task.title}\n"
                    f"Goal: {task.goal}\n"
                    f"Target words: {task.target_words}\n"
                    f"Tags: {task.tags}\n"
                    f"requires_research: {task.requires_research}\n"
                    f"requires_citations: {task.requires_citations}\n"
                    f"requires_code: {task.requires_code}\n"
                    f"Bullets:{bullets_text}\n\n"
                    f"Evidence (ONLY use these URLs when citing):\n{evidence_text}\n"
                )
            ),
        ]
    ).content.strip()

    return {"sections": [(task.id, section_md)]}


# ============================================================
# 8) ReducerWithImages (subgraph)
#    merge_content -> decide_images -> generate_and_place_images
# ============================================================
IMAGE_MODELS = [
    "gemini-3.1-flash-image",       # Nano Banana 2
    "gemini-3.1-flash-lite-image",  # Nano Banana 2 Lite
    "gemini-2.5-flash-image",       # legacy, last resort
]


def _safe_name(text: str, default: str = "blog") -> str:
    """Windows-safe filename stem."""
    cleaned = re.sub(r'[\\/:*?"<>|]', "", text or "").strip().rstrip(".")
    return cleaned or default


def merge_content(state: State) -> dict:
    plan = state["plan"]

    ordered_sections = [md for _, md in sorted(state["sections"], key=lambda x: x[0])]
    body = "\n\n".join(ordered_sections).strip()
    merged_md = f"# {plan.blog_title}\n\n{body}\n"
    return {"merged_md": merged_md}


DECIDE_IMAGES_SYSTEM = """You are an expert technical editor.
Decide which images/diagrams THIS blog needs.

Rules:
- Include at least 1 image and at most 3 images in total.
- Each image must materially improve understanding (architecture diagram, flow, comparison visual).
- Insert placeholders exactly: [[IMAGE_1]], [[IMAGE_2]], [[IMAGE_3]] on their own line,
  right after the paragraph or section they illustrate.
- Return the FULL original markdown in md_with_placeholders, unchanged except for the placeholders.
- Each image prompt must describe a clean technical diagram with a white background,
  flat vector style and short readable labels. Avoid decorative or photographic images.
- filename must be lowercase, end with .png, and contain no spaces (e.g. self_attention_flow.png).
Return strictly GlobalImagePlan.
"""


def _insert_placeholders(md: str, specs: List[dict]) -> str:
    """Insert any missing placeholders after the Nth '## ' section (fallback)."""
    chunks = re.split(r"(?m)^(?=## )", md)
    for i, spec in enumerate(specs):
        ph = spec["placeholder"]
        if ph in md:
            continue
        idx = min(i + 1, len(chunks) - 1)
        chunks[idx] = chunks[idx].rstrip() + f"\n\n{ph}\n\n"
        md = "".join(chunks)
    return md


def decide_images(state: State) -> dict:
    planner = llm.with_structured_output(GlobalImagePlan)
    merged_md = state["merged_md"]
    plan = state["plan"]
    assert plan is not None

    image_plan = planner.invoke(
        [
            SystemMessage(content=DECIDE_IMAGES_SYSTEM),
            HumanMessage(
                content=(
                    f"Blog kind: {plan.blog_kind}\n"
                    f"Topic: {state['topic']}\n\n"
                    "Insert placeholders + propose image prompts.\n\n"
                    f"{merged_md}"
                )
            ),
        ]
    )

    specs = [img.model_dump() for img in image_plan.images][:3]
    md_ph = image_plan.md_with_placeholders or ""

    # Guard 1: the LLM may truncate a long blog. If so, use the original markdown.
    if len(md_ph) < 0.8 * len(merged_md):
        print("WARNING: md_with_placeholders looks truncated, using merged_md instead")
        md_ph = merged_md

    # Guard 2: always produce at least one image.
    if not specs:
        print("No image specs returned, adding a default diagram")
        specs = [
            {
                "placeholder": "[[IMAGE_1]]",
                "filename": "diagram_1.png",
                "alt": f"Diagram: {state['topic']}",
                "caption": f"Overview diagram: {state['topic']}",
                "prompt": (
                    f"A clean technical architecture diagram with short readable labels explaining: "
                    f"{state['topic']}. White background, flat vector style."
                ),
                "size": "1536x1024",
                "quality": "medium",
            }
        ]

    # Guard 3: make sure every placeholder actually exists in the markdown.
    md_ph = _insert_placeholders(md_ph, specs)

    return {
        "md_with_placeholders": md_ph,
        "image_specs": specs,
    }


def _gemini_generate_image_bytes(prompt: str) -> bytes:
    """
    Returns raw image bytes generated by Gemini.
    Requires: pip install -U google-genai
    Env var: GOOGLE_API_KEY
    """
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set.")

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=90_000),  # 90 seconds, in ms
    )
    errors = []

    for model in IMAGE_MODELS:
        for attempt in range(2):  # one retry for temporary 429/503 errors
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_modalities=["TEXT", "IMAGE"],
                    ),
                )

                cands = resp.candidates or []
                if not cands:
                    raise RuntimeError(
                        f"No candidates returned. prompt_feedback={getattr(resp, 'prompt_feedback', None)}"
                    )

                cand = cands[0]
                parts = (cand.content.parts if cand.content else None) or []

                for part in parts:
                    inline = getattr(part, "inline_data", None)
                    if inline and getattr(inline, "data", None):
                        return inline.data

                texts = [p.text for p in parts if getattr(p, "text", None)]
                raise RuntimeError(
                    f"No image in response. finish_reason={cand.finish_reason}, model_text={texts[:1]}"
                )

            except Exception as e:
                msg = f"[{model}] {type(e).__name__}: {e}"
                errors.append(msg)
                print("IMAGE ERROR:", msg)
                transient = any(code in str(e) for code in ("429", "503", "UNAVAILABLE", "timed out", "Timeout"))
                if transient and attempt == 0:
                    time.sleep(5)
                    continue
                break  # move on to the next model

    raise RuntimeError(" | ".join(errors))


def generate_and_place_images(state: State) -> dict:
    plan = state["plan"]
    assert plan is not None

    md = state.get("md_with_placeholders") or state["merged_md"]
    image_specs = state.get("image_specs", []) or []
    md_name = OUTPUTS_DIR / f"{_safe_name(plan.blog_title)}.md"

    print(f"image_specs: {len(image_specs)} requested")

    # If no images requested, just write merged markdown
    if not image_specs:
        Path(md_name).write_text(md, encoding="utf-8")
        return {"final": md}

    images_dir = IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)
    print("saving images to:", images_dir)

    for spec in image_specs:
        placeholder = spec["placeholder"]
        # Add a short hash of the prompt so two different blogs never reuse the same
        # file name (the LLM often picks names like diagram_1.png). The same prompt
        # still maps to the same file, so the "already generated" cache keeps working.
        raw_name = re.sub(r"\s+", "_", _safe_name(spec["filename"], default="image.png"))
        raw_path = Path(raw_name)
        ext = raw_path.suffix.lower() if raw_path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else ".png"
        digest = hashlib.sha1(spec["prompt"].encode("utf-8")).hexdigest()[:8]
        filename = f"{raw_path.stem}_{digest}{ext}"
        out_path = images_dir / filename

        if placeholder not in md:
            print(f"WARNING: {placeholder} not found in markdown, image will not be placed")

        # generate only if missing or empty (a 0-byte file from a failed run doesn't count)
        if not out_path.exists() or out_path.stat().st_size == 0:
            try:
                img_bytes = _gemini_generate_image_bytes(spec["prompt"])
                out_path.write_bytes(img_bytes)
                print("saved:", out_path)
            except Exception as e:
                traceback.print_exc()
                prompt_block = (
                    f"> **[IMAGE GENERATION FAILED]** {spec.get('caption', '')}\n>\n"
                    f"> **Alt:** {spec.get('alt', '')}\n>\n"
                    f"> **Prompt:** {spec.get('prompt', '')}\n>\n"
                    f"> **Error:** {e}\n"
                )
                md = md.replace(placeholder, prompt_block)
                continue

        img_md = f"![{spec['alt']}](images/{filename})\n*{spec['caption']}*"
        md = md.replace(placeholder, img_md)

    Path(md_name).write_text(md, encoding="utf-8")
    return {"final": md}


# build reducer subgraph
reducer_graph = StateGraph(State)
reducer_graph.add_node("merge_content", merge_content)
reducer_graph.add_node("decide_images", decide_images)
reducer_graph.add_node("generate_and_place_images", generate_and_place_images)
reducer_graph.add_edge(START, "merge_content")
reducer_graph.add_edge("merge_content", "decide_images")
reducer_graph.add_edge("decide_images", "generate_and_place_images")
reducer_graph.add_edge("generate_and_place_images", END)
reducer_subgraph = reducer_graph.compile()


# -----------------------------
# 9) Build main graph
# -----------------------------
g = StateGraph(State)
g.add_node("router", router_node)
g.add_node("research", research_node)
g.add_node("orchestrator", orchestrator_node)
g.add_node("worker", worker_node)
g.add_node("reducer", reducer_subgraph)

g.add_edge(START, "router")
g.add_conditional_edges("router", route_next, {"research": "research", "orchestrator": "orchestrator"})
g.add_edge("research", "orchestrator")

g.add_conditional_edges("orchestrator", fanout, ["worker"])
g.add_edge("worker", "reducer")
g.add_edge("reducer", END)


# =========================
# PostgreSQL Checkpointer (connection pool, survives idle drops)
# =========================
DATABASE_URL = get_database_url()

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=5,
    max_idle=60,
    check=ConnectionPool.check_connection,  # replaces dead connections before use
    kwargs={
        "autocommit": True,
        "row_factory": dict_row,
        "prepare_threshold": 0,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
        "connect_timeout": 10,
    },
    open=True,
)

checkpointer = PostgresSaver(pool)
checkpointer.setup()

app = g.compile(checkpointer=checkpointer)


# Fresh thread per kernel start. run() also creates a new one for every call.
config = {"configurable": {"thread_id": f"thread-{uuid.uuid4().hex[:8]}"}}


# -----------------------------
# 10) Runner
# -----------------------------
def run(topic: str, as_of: Optional[str] = None):
    if as_of is None:
        as_of = date.today().isoformat()

    run_config = {"configurable": {"thread_id": f"run-{uuid.uuid4().hex[:8]}"}}

    inputs = {
        "topic": topic,
        "mode": "",
        "needs_research": False,
        "queries": [],
        "evidence": [],
        "plan": None,
        "as_of": as_of,
        "recency_days": 7,
        "sections": [],
        "merged_md": "",
        "md_with_placeholders": "",
        "image_specs": [],
        "final": "",
    }

    t0 = time.time()
    for ns, update in app.stream(
        inputs, config=run_config, stream_mode="updates", subgraphs=True
    ):
        for node in update:
            prefix = "/".join(ns) + "/" if ns else ""
            print(f"[{time.time() - t0:6.1f}s] done: {prefix}{node}")

    out = app.get_state(run_config).values
    return out


if __name__ == "__main__":
    result = run("Self Attention in Transformer Architecture also give me with one image of the architecture")
    print(result.get("final", "")[:500])