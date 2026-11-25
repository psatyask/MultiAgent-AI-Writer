# LangGraph Subgraphs: Build a Practical Multi-Agent Workflow

## Define the workflow and subgraph boundaries

The parent graph coordinates two specialist teams in a fixed sequence:

```text
User request → Research subgraph → Writing subgraph → Final draft
```

The research team turns the request into findings. The writing team uses those findings to produce the draft.

The parent state has three shared fields: `request`, `findings`, and `draft`. Each team can also maintain private working state—for example, research notes or an editing checklist—without exposing those fields to the parent. LangGraph supports adding a compiled subgraph as a parent node when their state schemas share keys. ([Source](https://docs.langchain.com/oss/python/langgraph/use-subgraphs))

In this example, deterministic Python nodes simulate the specialists: the same input produces the same output, with no API key or LLM required. In a real application, those nodes could call agents or models instead; the team boundaries and state handoffs would remain the same.

A single flat graph could run the same nodes and may be simpler for a small workflow. Subgraphs become worthwhile when teams have enough internal steps or private state to benefit from separate boundaries, or when a team workflow should be reused.

This design follows the hierarchical-team framing in LangChain’s multi-agent workflow example, but uses a fixed handoff rather than a supervisor choosing the next team. ([Source](https://www.langchain.com/blog/langgraph-multi-agent-workflows))

**Tested package versions:** Not found in provided sources.

## Set up the parent and research team states

This example targets Python 3.11 and `langgraph==1.0.0`. These versions and the current documentation could not be independently verified in this environment.

Create a virtual environment and install LangGraph:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install "langgraph==1.0.0"
```

The parent and research states share `request` and `findings`. The research team also has a private `notes` key. Shared keys provide the contract for adding the compiled research graph to a parent graph later ([Source](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)).

Save this as `research.py`:

```python
from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph


class SharedState(TypedDict):
    request: str
    findings: NotRequired[str]


class ParentState(SharedState):
    pass


class ResearchState(SharedState):
    notes: NotRequired[str]


def gather_notes(state: ResearchState) -> dict[str, str]:
    return {
        "notes": f"Investigate scope and examples for: {state['request']}"
    }


def summarize_notes(state: ResearchState) -> dict[str, str]:
    return {"findings": f"Research brief: {state['notes']}"}


builder = StateGraph(ResearchState)
builder.add_node("gather_notes", gather_notes)
builder.add_node("summarize_notes", summarize_notes)
builder.add_edge(START, "gather_notes")
builder.add_edge("gather_notes", "summarize_notes")
builder.add_edge("summarize_notes", END)

research_graph = builder.compile()

if __name__ == "__main__":
    result = research_graph.invoke({"request": "LangGraph subgraphs"})
    print(result["findings"])
```

Run it with `python research.py`. Both specialists are deterministic stand-ins: the first turns the request into notes, and the second turns those notes into findings. In a real workflow, replace their bodies with agent or model calls while preserving their state updates. The later writing team can consume `findings`.

`START` selects the entry node, the next edge passes control to the summarizer, and `END` finishes the run. Compiling produces an executable graph, which can be called with `invoke` ([Source](https://reference.langchain.com/python/langgraph/graph/state/StateGraph)).

## Build the writing team and compose the parent graph

The writing team shares `findings` and `draft` with the parent, but keeps its intermediate `outline` in its own state. Its two deterministic nodes first outline the findings, then turn that outline into a draft. These are placeholders, not model-generated content.

The parent runs two compiled subgraphs in order: research, then writing. The research team below is also deterministic; it produces example findings rather than searching for evidence.

LangGraph passes shared state keys into a subgraph added as a node and returns shared updates to the parent. Since `outline` is absent from the parent schema, it is not part of the parent’s state contract. This is a state boundary, not a secrecy guarantee: debugging or persistence may expose child state. ([Source](https://docs.langchain.com/oss/python/langgraph/use-subgraphs))

![Parent graph passing topic to research, findings to writing, and returning a draft while outline remains in writing state.](images/subgraph_state_handoff_d634cfb5.png)
*The parent coordinates compiled teams through shared keys; the writing outline stays outside the parent state contract.*

The following complete listing compiles both teams and the parent. It targets `langgraph==1.0.0` and requires no API keys. **The package version and execution were not tested in this environment**; the graph construction follows the documented `StateGraph` API. ([Source](https://reference.langchain.com/python/langgraph/graph/state/StateGraph))

```python
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class WritingState(TypedDict, total=False):
    findings: list[str]
    draft: str
    outline: list[str]  # Private to the writing subgraph


def outline_findings(state: WritingState) -> dict:
    return {"outline": [f"Section: {item}" for item in state["findings"]]}


def draft_article(state: WritingState) -> dict:
    return {"draft": "\n".join(state["outline"])}


writing_builder = StateGraph(WritingState)
writing_builder.add_node("outline", outline_findings)
writing_builder.add_node("draft", draft_article)
writing_builder.add_edge(START, "outline")
writing_builder.add_edge("outline", "draft")
writing_builder.add_edge("draft", END)
writing_team = writing_builder.compile()


class ResearchState(TypedDict, total=False):
    topic: str
    findings: list[str]


def research(state: ResearchState) -> dict:
    return {
        "findings": [
            f"Question: {state['topic']}",
            "Consider evidence and limitations.",
        ]
    }


research_builder = StateGraph(ResearchState)
research_builder.add_node("research", research)
research_builder.add_edge(START, "research")
research_builder.add_edge("research", END)
research_team = research_builder.compile()


class ParentState(TypedDict, total=False):
    topic: str
    findings: list[str]
    draft: str


parent_builder = StateGraph(ParentState)
parent_builder.add_node("research_team", research_team)
parent_builder.add_node("writing_team", writing_team)
parent_builder.add_edge(START, "research_team")
parent_builder.add_edge("research_team", "writing_team")
parent_builder.add_edge("writing_team", END)
parent = parent_builder.compile()

result = parent.invoke({"topic": "Graph-based agent orchestration"})
print(result["draft"])
```

To use real agents, replace the deterministic node bodies with model or research calls while preserving their input and output keys. The parent need not know how either team produces its shared updates.

## Run the workflow and verify the result

This runnable example compiles two subgraphs into a parent graph. The research team writes `findings`; the writing team reads them and writes `draft`. Only fields declared in the parent schema are returned by the parent. ([Source](https://docs.langchain.com/oss/python/langgraph/use-subgraphs))

```python
from typing import TypedDict
from langgraph.graph import START, END, StateGraph


class ParentState(TypedDict):
    request: str
    findings: str
    draft: str


class ResearchState(TypedDict):
    request: str
    findings: str
    research_notes: str  # Private to the research subgraph


class WritingState(TypedDict):
    findings: str
    draft: str
    writing_notes: str  # Private to the writing subgraph


def research(state: ResearchState):
    return {
        "findings": "Example finding: subgraphs share state keys.",
        "research_notes": "Deterministic fixture",
    }


def write(state: WritingState):
    return {
        "draft": f"Draft: {state['findings']}",
        "writing_notes": "Deterministic fixture",
    }


def team(schema, name, node):
    builder = StateGraph(schema)
    builder.add_node(name, node)
    builder.add_edge(START, name)
    builder.add_edge(name, END)
    return builder.compile()


parent = StateGraph(ParentState)
parent.add_node("research", team(ResearchState, "researcher", research))
parent.add_node("writing", team(WritingState, "writer", write))
parent.add_edge(START, "research")
parent.add_edge(""research", "writing")
parent.add_edge("writing", END)
app = parent.compile()

result = app.invoke({"request": "Explain subgraphs"})
print("Findings:", result["findings"])
print("Draft:", result["draft"])

assert result["findings"] in result["draft"]
assert "research_notes" not in result
assert "writing_notes" not in result
```

The representative output comes from the hard-coded fixture, **not an external search**:

```text
Findings: Example finding: subgraphs share state keys.
Draft: Draft: Example finding: subgraphs share state keys.
```

Replace `research` and `write` with model-backed specialists that return the same state keys. A real researcher can use approved sources; the writer should consume its returned findings. Load provider credentials from environment variables or a secret manager, not source code.

The graph uses the documented `StateGraph` compile-and-invoke pattern. ([Source](https://reference.langchain.com/python/langgraph/graph/state/StateGraph)) **Tested package versions:** not verified in this environment; record your installed version with `python -m pip show langgraph` when running the example.

## Handle state mismatches and operational failures

In direct composition, the research team and writing subgraph must agree on the shared key: `findings`. If research returns `notes` instead, the parent schema does not pass that value to the writer. ([Source](https://docs.langchain.com/oss/python/langgraph/use-subgraphs))

```python
from typing import TypedDict
from langgraph.graph import START, END, StateGraph

class State(TypedDict, total=False):
    topic: str
    findings: str
    draft: str

def writer(state):
    if not state.get("findings"):
        raise ValueError("No findings for writing team")
    return {"draft": f"Report: {state['findings']}"}

writing = StateGraph(State)
writing.add_node("writer", writer)
writing.add_edge(START, "writer")
writing.add_edge("writer", END)
writing_team = writing.compile()

def build(*, mismatch=False):
    def research(state):
        if state["topic"] == "fail":
            raise RuntimeError("Research specialist failed")
        value = "" if state["topic"] == "empty" else "Verified facts"
        return {"notes" if mismatch else "findings": value}

    parent = StateGraph(State)
    parent.add_node("research", research)
    parent.add_node("writing", writing_team)
    parent.add_edge(START, "research")
    parent.add_edge("research", "writing")
    parent.add_edge("writing", END)
    return parent.compile()

assert build().invoke({"topic": "graphs"})["draft"] == "Report: Verified facts"

for graph, topic, error in [
    (build(mismatch=True), "graphs", ValueError),
    (build(), "empty", ValueError),
    (build(), "fail", RuntimeError),
]:
    try:
        graph.invoke({"topic": topic})
    except error:
        pass
    else:
        raise AssertionError("Expected workflow to stop")
```

For different schemas, use a wrapper node instead of direct composition. For example, a child expecting `research_notes` can be called with `child.invoke({"research_notes": state["findings"]})`; return its `draft` under the parent’s `draft` key. ([Source](https://machinelearningplus.com/gen-ai/langgraph-subgraphs-composing-reusable-workflows))

![Wrapper maps parent findings to child research_notes and returns the child's draft to the parent.](images/subgraph_schema_adapter_0170afb5.png)
*A wrapper translates between parent and child state schemas when their keys differ.*

These tests choose **stop on failure**: empty findings and specialist exceptions propagate to the caller. In production, catch them at the application boundary or define an explicit error field and routing policy.

If research specialists run concurrently and write to the same key, define an appropriate reducer, such as list concatenation, rather than relying on the default overwrite behavior. ([Source](https://reference.langchain.com/python/langgraph/graph/state/StateGraph))

Redact findings, drafts, personal data, and proprietary source material from logs. Keep credentials outside graph state, including state retained in checkpoints. ([Source](https://docs.langchain.com/oss/python/langgraph/persistence))

**Tested package versions:** Not verified; no execution environment was available. The nodes are deterministic stand-ins for real research and writing agents.

## Observe execution and choose persistence deliberately

Assuming `graph` is the compiled parent workflow and accepts a `topic`, stream updates with subgraph details enabled:

```python
from time import perf_counter

start = perf_counter()
for namespace, update in graph.stream(
    {"topic": "LangGraph subgraphs"},
    stream_mode="updates",
    subgraphs=True,
):
    print("child" if namespace else "parent", namespace, update)

print(f"Elapsed: {perf_counter() - start:.3f}s")
```

The empty namespace identifies parent updates; nonempty namespaces identify child execution. The parent’s updates show what its nodes return, while child updates expose intermediate specialist steps. Streaming output is not the same as the final combined state. ([Source](https://docs.langchain.com/oss/python/langgraph/use-subgraphs))

Subgraph checkpoints have separate namespaces within a thread. A store serves a different purpose: sharing data across graph boundaries and, where appropriate, across threads. ([Source](https://docs.langchain.com/oss/python/langgraph/persistence))

When the parent has a checkpointer, a normally compiled child inherits it. The documentation recommends **per-invocation** persistence for most subgraphs; opt into per-thread child persistence when a specialist must retain state across calls. For a one-shot research-and-writing run, no checkpointer may be necessary. ([Source](https://docs.langchain.com/oss/python/langgraph/use-subgraphs))

The timer measures the whole workflow, including printing. For cleaner latency comparisons, minimize output overhead. When replacing deterministic stand-ins with model calls, record token usage and model-specific cost for each specialist, including retries.

Keep the workflow sequential while writing depends on research findings. Explore parallelism for independent research specialists first, then join their findings before starting the writing team.

**Version note:** Package versions and the API could not be tested or independently verified here; verify the snippet against your installed LangGraph version before use.