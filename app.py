from __future__ import annotations

import io
import json
import logging
import re
import uuid
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Generator

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import uvicorn

# ---------------------------------------------------------
# backend.app is the compiled LangGraph workflow.
# IMAGES_DIR is the folder backend.py saves generated images into,
# so app.py and backend.py always agree on the path.
# ---------------------------------------------------------
from backend import app as workflow, IMAGES_DIR


# ---------------------------------------------------------
# Paths
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
OUTPUTS_DIR = BASE_DIR / "outputs"

for _directory in (TEMPLATES_DIR, STATIC_DIR, IMAGES_DIR, OUTPUTS_DIR):
    _directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("langgraph-fastapi")


# ---------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------
app = FastAPI(
    title="MultiAgent-AI-Writer",
    description="FastAPI frontend for the existing LangGraph workflow.",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# backend.py saves generated images inside IMAGES_DIR
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ---------------------------------------------------------
# Request schema
# ---------------------------------------------------------
class AgentRunRequest(BaseModel):
    topic: str = Field(
        ...,
        min_length=3,
        max_length=1000,
        description="The technical blog topic.",
    )


# ---------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------
def make_serializable(value: Any) -> Any:
    """Recursively convert Pydantic models and other values to JSON-compatible values."""

    if hasattr(value, "model_dump"):
        return make_serializable(value.model_dump())

    if hasattr(value, "dict"):
        return make_serializable(value.dict())

    if isinstance(value, dict):
        return {str(key): make_serializable(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [make_serializable(item) for item in value]

    return value


def create_sse_event(payload: dict[str, Any], event_name: str | None = None) -> str:
    """Convert a dictionary to a Server-Sent Event message."""

    encoded_payload = json.dumps(jsonable_encoder(payload), ensure_ascii=False)

    lines: list[str] = []
    if event_name:
        lines.append(f"event: {event_name}")
    lines.append(f"data: {encoded_payload}")

    return "\n".join(lines) + "\n\n"


def stage_event(stage_id: str, label: str, status: str, detail: str) -> str:
    return create_sse_event(
        {
            "type": "stage",
            "id": stage_id,
            "label": label,
            "status": status,
            "detail": detail,
        }
    )


def substage_event(
    sub_id: str,
    label: str,
    status: str,
    namespace: tuple[str, ...],
) -> str:
    return create_sse_event(
        {
            "type": "substage",
            "id": sub_id,
            "label": label,
            "status": status,
            "namespace": list(namespace),
        }
    )


def normalize_stream_chunk(chunk: Any) -> tuple[tuple[str, ...], dict[str, Any]]:
    """
    Normalize LangGraph streaming chunks.

    With subgraphs=True LangGraph returns (namespace, update).
    Root graph updates may also be plain dictionaries depending on the version.
    """

    if isinstance(chunk, tuple) and len(chunk) == 2 and isinstance(chunk[1], dict):
        namespace = tuple(str(item) for item in (chunk[0] or ()))
        return namespace, chunk[1]

    if isinstance(chunk, dict):
        return (), chunk

    return (), {}


def get_plan_task_map(plan: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Create a task lookup using each task ID."""

    task_map: dict[int, dict[str, Any]] = {}

    tasks = plan.get("tasks", [])
    if not isinstance(tasks, list):
        return task_map

    for task in tasks:
        if not isinstance(task, dict):
            continue
        try:
            task_id = int(task["id"])
        except (KeyError, TypeError, ValueError):
            continue
        task_map[task_id] = task

    return task_map


def save_final_markdown(run_id: str, markdown: str) -> Path:
    """Save a predictable copy of the generated Markdown for the download endpoint."""

    run_directory = OUTPUTS_DIR / run_id
    run_directory.mkdir(parents=True, exist_ok=True)

    output_file = run_directory / "blog.md"
    output_file.write_text(markdown, encoding="utf-8")

    return output_file


def collect_image_files(markdown: str) -> list[Path]:
    """Return existing image files referenced as images/<name> in the markdown."""

    files: list[Path] = []
    seen: set[str] = set()

    for name in re.findall(r"!\[[^\]]*\]\(\s*images/([^)\s]+)\s*\)", markdown):
        safe_name = Path(name).name
        if safe_name in seen:
            continue
        candidate = IMAGES_DIR / safe_name
        if candidate.is_file() and candidate.stat().st_size > 0:
            seen.add(safe_name)
            files.append(candidate)

    return files


# ---------------------------------------------------------
# LangGraph streaming
# ---------------------------------------------------------
STAGE_ROUTER = ("router", "Analyze the request")
STAGE_RESEARCH = ("research", "Research authoritative sources")
STAGE_ORCHESTRATOR = ("orchestrator", "Create the article plan")
STAGE_WORKERS = ("workers", "Write the planned sections")
STAGE_REDUCER = ("reducer", "Assemble the final article")


def stream_workflow(topic: str, run_id: str) -> Generator[str, None, None]:
    """
    Run the LangGraph workflow and stream observable execution updates
    (node status, routing, research, plan, sections, image status, final Markdown).
    """

    config = {"configurable": {"thread_id": run_id}}

    # Same full input that backend.py expects.
    workflow_input = {
        "topic": topic,
        "mode": "",
        "needs_research": False,
        "queries": [],
        "evidence": [],
        "plan": None,
        "as_of": date.today().isoformat(),
        "recency_days": 7,
        "sections": [],
        "merged_md": "",
        "md_with_placeholders": "",
        "image_specs": [],
        "final": "",
    }

    task_map: dict[int, dict[str, Any]] = {}
    completed_task_ids: set[int] = set()

    final_markdown = ""
    workers_completed_event_sent = False
    reducer_started_event_sent = False

    yield create_sse_event({"type": "run_started", "run_id": run_id, "topic": topic})

    yield stage_event(
        *STAGE_ROUTER,
        "running",
        "Determining whether the topic requires current web research.",
    )

    try:
        stream = workflow.stream(
            workflow_input,
            config=config,
            stream_mode="updates",
            subgraphs=True,
        )

        for raw_chunk in stream:
            namespace, updates = normalize_stream_chunk(raw_chunk)

            if not updates:
                continue

            for node_name, raw_node_update in updates.items():
                node_update = make_serializable(raw_node_update)

                if not isinstance(node_update, dict):
                    node_update = {}

                # ---------------- Router ----------------
                if node_name == "router":
                    mode = str(node_update.get("mode", "closed_book"))
                    needs_research = bool(node_update.get("needs_research", False))
                    queries = node_update.get("queries", [])

                    yield create_sse_event(
                        {
                            "type": "routing",
                            "mode": mode,
                            "needs_research": needs_research,
                            "queries": queries,
                        }
                    )

                    yield stage_event(
                        *STAGE_ROUTER,
                        "completed",
                        f"Selected {mode.replace('_', ' ')} mode.",
                    )

                    if needs_research:
                        yield stage_event(
                            *STAGE_RESEARCH,
                            "running",
                            "Searching the web and preparing a deduplicated evidence pack.",
                        )
                    else:
                        yield stage_event(
                            *STAGE_ORCHESTRATOR,
                            "running",
                            "Creating the article structure, goals and writing tasks.",
                        )

                # ---------------- Research ----------------
                elif node_name == "research":
                    evidence = node_update.get("evidence", [])
                    if not isinstance(evidence, list):
                        evidence = []

                    yield create_sse_event(
                        {
                            "type": "research_complete",
                            "count": len(evidence),
                            "evidence": evidence[:12],
                        }
                    )

                    yield stage_event(
                        *STAGE_RESEARCH,
                        "completed",
                        f"Prepared {len(evidence)} deduplicated sources.",
                    )

                    yield stage_event(
                        *STAGE_ORCHESTRATOR,
                        "running",
                        "Creating sections, goals, bullets and target word counts.",
                    )

                # ---------------- Orchestrator ----------------
                elif node_name == "orchestrator":
                    plan = node_update.get("plan", {})
                    if not isinstance(plan, dict):
                        plan = {}

                    task_map = get_plan_task_map(plan)

                    yield create_sse_event({"type": "plan", "plan": plan})

                    yield stage_event(
                        *STAGE_ORCHESTRATOR,
                        "completed",
                        f"Created {len(task_map)} article sections.",
                    )

                    yield stage_event(
                        *STAGE_WORKERS,
                        "running",
                        "Section workers are writing the article in parallel.",
                    )

                # ---------------- Workers ----------------
                elif node_name == "worker":
                    sections = node_update.get("sections", [])
                    if not isinstance(sections, list):
                        sections = []

                    for section in sections:
                        if not isinstance(section, (list, tuple)) or len(section) != 2:
                            continue

                        raw_task_id, section_markdown = section

                        try:
                            task_id = int(raw_task_id)
                        except (TypeError, ValueError):
                            continue

                        # A parallel worker update should be sent once.
                        if task_id in completed_task_ids:
                            continue

                        completed_task_ids.add(task_id)

                        title = task_map.get(task_id, {}).get("title", f"Section {task_id}")

                        yield create_sse_event(
                            {
                                "type": "section_complete",
                                "task_id": task_id,
                                "title": title,
                                "markdown": str(section_markdown),
                                "completed": len(completed_task_ids),
                                "total": len(task_map),
                            }
                        )

                    if (
                        task_map
                        and len(completed_task_ids) >= len(task_map)
                        and not workers_completed_event_sent
                    ):
                        workers_completed_event_sent = True

                        yield stage_event(
                            *STAGE_WORKERS,
                            "completed",
                            f"Completed all {len(task_map)} sections.",
                        )

                        yield stage_event(
                            *STAGE_REDUCER,
                            "running",
                            "Merging the sections and planning useful visuals.",
                        )

                        reducer_started_event_sent = True

                # ---------------- Reducer subgraph: merge ----------------
                elif node_name == "merge_content":
                    if not reducer_started_event_sent:
                        yield stage_event(
                            *STAGE_REDUCER,
                            "running",
                            "Merging the sections and planning useful visuals.",
                        )
                        reducer_started_event_sent = True

                    yield substage_event(
                        "merge_content",
                        "Merged all written sections",
                        "completed",
                        namespace,
                    )

                # ---------------- Reducer subgraph: image plan ----------------
                elif node_name == "decide_images":
                    image_specs = node_update.get("image_specs", [])
                    if not isinstance(image_specs, list):
                        image_specs = []

                    yield create_sse_event(
                        {
                            "type": "images_planned",
                            "count": len(image_specs),
                            "images": image_specs,
                        }
                    )

                    yield substage_event(
                        "decide_images",
                        (
                            f"Planned {len(image_specs)} technical visual"
                            f"{'' if len(image_specs) == 1 else 's'}"
                        ),
                        "completed",
                        namespace,
                    )

                    # Image generation is the slowest step, so show it as running.
                    yield substage_event(
                        "generate_images",
                        "Generating visuals (this can take a minute)",
                        "running",
                        namespace,
                    )

                # ---------------- Reducer subgraph: image generation ----------------
                elif node_name == "generate_and_place_images":
                    generated_final = node_update.get("final")
                    if generated_final:
                        final_markdown = str(generated_final)

                    yield substage_event(
                        "generate_images",
                        "Generated and placed visuals",
                        "completed",
                        namespace,
                    )

                # ---------------- Root reducer update ----------------
                elif node_name == "reducer":
                    generated_final = node_update.get("final")
                    if generated_final:
                        final_markdown = str(generated_final)

        # Fall back to the final checkpoint if the stream did not carry the Markdown.
        if not final_markdown:
            snapshot = workflow.get_state(config)
            state_values = getattr(snapshot, "values", {})

            if isinstance(state_values, dict):
                final_markdown = str(state_values.get("final", ""))

        if not final_markdown:
            raise RuntimeError("The workflow completed but did not return final Markdown.")

        save_final_markdown(run_id=run_id, markdown=final_markdown)

        yield stage_event(
            *STAGE_REDUCER,
            "completed",
            "The final Markdown article is ready.",
        )

        # backend.py keeps the article usable when an image fails and writes the error into it.
        if "[IMAGE GENERATION FAILED]" in final_markdown:
            yield create_sse_event(
                {
                    "type": "warning",
                    "message": (
                        "One or more images could not be generated. "
                        "Check the server console for the IMAGE ERROR lines."
                    ),
                }
            )

        image_count = len(collect_image_files(final_markdown))

        yield create_sse_event(
            {
                "type": "final",
                "run_id": run_id,
                "markdown": final_markdown,
                "download_url": f"/api/runs/{run_id}/download",
                "images_url": f"/api/runs/{run_id}/images" if image_count else None,
                "image_count": image_count,
            }
        )

        yield create_sse_event({"type": "done", "run_id": run_id})

    except GeneratorExit:
        logger.info("Browser disconnected from run %s", run_id)
        raise

    except Exception as error:
        logger.exception("Workflow run %s failed", run_id)

        yield create_sse_event(
            {
                "type": "error",
                "run_id": run_id,
                "message": str(error),
            }
        )


# ---------------------------------------------------------
# Page endpoint
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"page_title": "MultiAgent-AI-Writer"},
    )


# ---------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------
@app.get("/api/health")
def health_check():
    return {"status": "ok", "workflow": "loaded"}


# ---------------------------------------------------------
# Agent execution endpoint
# ---------------------------------------------------------
@app.post("/api/run")
def run_agent(request_data: AgentRunRequest):
    topic = request_data.topic.strip()

    if len(topic) < 3:
        raise HTTPException(status_code=422, detail="Please provide a valid topic.")

    run_id = uuid.uuid4().hex

    return StreamingResponse(
        stream_workflow(topic=topic, run_id=run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------
# Markdown download endpoint
# ---------------------------------------------------------
@app.get("/api/runs/{run_id}/download")
def download_markdown(run_id: str):
    # Only allow safe run ID characters.
    safe_run_id = "".join(
        character
        for character in run_id
        if character.isalnum() or character in {"-", "_"}
    )

    if safe_run_id != run_id:
        raise HTTPException(status_code=400, detail="Invalid run ID.")

    output_file = OUTPUTS_DIR / safe_run_id / "blog.md"

    if not output_file.is_file():
        raise HTTPException(status_code=404, detail="Generated Markdown file was not found.")

    return FileResponse(
        path=output_file,
        media_type="text/markdown",
        filename=f"generated-blog-{safe_run_id[:8]}.md",
    )


# ---------------------------------------------------------
# Image download endpoint
# ---------------------------------------------------------
@app.get("/api/runs/{run_id}/images")
def download_images(run_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID.")

    markdown_file = OUTPUTS_DIR / run_id / "blog.md"

    if not markdown_file.is_file():
        raise HTTPException(status_code=404, detail="Run was not found.")

    images = collect_image_files(markdown_file.read_text(encoding="utf-8"))

    if not images:
        raise HTTPException(status_code=404, detail="No generated images were found.")

    if len(images) == 1:
        return FileResponse(path=images[0], filename=images[0].name)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for image in images:
            archive.write(image, arcname=image.name)

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="images-{run_id[:8]}.zip"'
        },
    )


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
