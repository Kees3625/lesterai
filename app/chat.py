"""Chat with retrieval over the knowledge base.

Two providers:
- anthropic: agentic RAG — Claude decides when/what to search via tool use.
- ollama:    retrieve-then-answer — we search up front and hand passages to a
             local open-source model (their tool calling is too unreliable).
"""

import json
from typing import Generator

import anthropic
import httpx

from . import config, db, semantic

MAX_HISTORY_MESSAGES = 20  # cap conversation length sent to the API

SYSTEM_PROMPT = """You are Lester, the Virtual IT Assistant for Capital Vacations employees.

Your job is to answer questions using the company knowledge base. Rules:

- For any question that could be answered by company documents (policies, procedures, \
products, systems, HR, IT, operations), you MUST call the search_knowledge_base tool \
before answering — do not answer from general knowledge. Search with 2-3 different \
phrasings if the first search comes up empty.
- Base your answer on the retrieved passages. Cite the source document title in \
brackets after the relevant statement, e.g. [Employee Handbook].
- If the knowledge base has no relevant information, say so plainly and suggest who or \
what team the user might ask instead. Never invent policy details.
- Only use links, websites, and download locations exactly as written in the retrieved \
passages. Never add or guess URLs, vendor websites, or app/download pages.
- Do not tell the user to read, see, or "refer to" another guide or document by name — \
the knowledge documents are your own reference, not pages the user can open. Give the \
relevant steps yourself; if the problem isn't resolved, tell them to submit a ticket at \
https://support.capitalvacations.com. (Citing a source title in brackets is still fine.)
- Keep answers concise and practical. Use bullet points for multi-step procedures.
- You may answer general workplace questions (e.g. how to write a formula in Excel) \
without searching, but make clear when an answer is general knowledge rather than \
company policy."""

TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the company knowledge base (uploaded policies, procedures, FAQs, "
            "and reference documents). Call this whenever the user's question could be "
            "answered by company documentation. Returns the most relevant passages with "
            "their source document titles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to search for. Use specific nouns and synonyms, not full sentences.",
                }
            },
            "required": ["query"],
        },
    }
]

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _run_search(query: str, sources: dict) -> str:
    results = semantic.hybrid_search(query, limit=8)
    if not results:
        return "No results found in the knowledge base for this query."
    parts = []
    for r in results:
        sources[r["doc_id"]] = r["title"]
        parts.append(f"<passage source=\"{r['title']}\">\n{r['content']}\n</passage>")
    return "\n\n".join(parts)


def stream_chat(history: list[dict]) -> Generator[str, None, None]:
    """Yield NDJSON lines:
    {"type":"text","text":...} | {"type":"status","text":...} | {"type":"sources","sources":[...]} | {"type":"error","text":...} | {"type":"done"}
    """
    if config.LLM_PROVIDER == "ollama":
        yield from _stream_ollama(history)
    else:
        yield from _stream_anthropic(history)


def _stream_anthropic(history: list[dict]) -> Generator[str, None, None]:
    client = get_client()
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history[-MAX_HISTORY_MESSAGES:]
    ]
    sources: dict = {}

    try:
        for _ in range(8):  # hard cap on tool-use rounds
            with client.messages.stream(
                model=config.CLAUDE_MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=TOOLS,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield json.dumps({"type": "text", "text": text}) + "\n"
                response = stream.get_final_message()

            if response.stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "search_knowledge_base":
                    query = (block.input or {}).get("query", "")
                    yield json.dumps({"type": "status", "text": f"Searching: {query}"}) + "\n"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _run_search(query, sources),
                    })
            messages.append({"role": "user", "content": tool_results})

        if sources:
            yield json.dumps({
                "type": "sources",
                "sources": sorted(sources.values()),
            }) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    except anthropic.AuthenticationError:
        yield json.dumps({"type": "error", "text": "Invalid Anthropic API key. Check ANTHROPIC_API_KEY in .env."}) + "\n"
    except anthropic.RateLimitError:
        yield json.dumps({"type": "error", "text": "The AI service is rate-limited right now. Try again in a minute."}) + "\n"
    except anthropic.APIError as e:
        yield json.dumps({"type": "error", "text": f"AI service error: {e.message}"}) + "\n"


# ---------- Ollama (free, self-hosted) ----------

OLLAMA_SYSTEM_PROMPT = """You are Lester, the Virtual IT Assistant for Capital Vacations employees.

Answer the user's question using ONLY the knowledge base passages provided below.
Rules:
- Cite the source document title in brackets after relevant statements, e.g. [Employee Handbook].
- If the passages don't contain the answer, say so plainly and suggest who the user \
might ask instead. Never invent policy details.
- Only use links, websites, and download locations exactly as written in the passages. \
Never add or guess URLs, vendor websites, or app/download pages.
- Do not tell the user to read, see, or "refer to" another guide or document by name — \
the knowledge documents are your own reference, not pages the user can open. Give the \
relevant steps yourself; if the problem isn't resolved, tell them to submit a ticket at \
https://support.capitalvacations.com. (Citing a source title in brackets is still fine.)
- Keep answers concise and practical. Use bullet points for multi-step procedures.

Knowledge base passages:

{context}"""


def _stream_ollama(history: list[dict]) -> Generator[str, None, None]:
    question = history[-1]["content"]
    yield json.dumps({"type": "status", "text": "Searching the knowledge base"}) + "\n"

    sources: dict = {}
    results = semantic.hybrid_search(question, limit=8)
    if results:
        for r in results:
            sources[r["doc_id"]] = r["title"]
        context = "\n\n".join(
            f"<passage source=\"{r['title']}\">\n{r['content']}\n</passage>" for r in results
        )
    else:
        context = "(No matching passages were found in the knowledge base.)"

    messages = [{"role": "system", "content": OLLAMA_SYSTEM_PROMPT.format(context=context)}]
    messages += [
        {"role": m["role"], "content": m["content"]}
        for m in history[-MAX_HISTORY_MESSAGES:]
    ]

    try:
        with httpx.stream(
            "POST",
            f"{config.OLLAMA_URL}/api/chat",
            json={"model": config.OLLAMA_MODEL, "messages": messages, "stream": True},
            timeout=httpx.Timeout(300.0, connect=10.0),
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                detail = resp.text[:300]
                yield json.dumps({"type": "error",
                                  "text": f"Ollama returned an error ({resp.status_code}): {detail}"}) + "\n"
                return
            answered = False
            for line in resp.iter_lines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                text = ev.get("message", {}).get("content", "")
                if text:
                    answered = True
                    yield json.dumps({"type": "text", "text": text}) + "\n"
                if ev.get("done"):
                    break
            if answered and sources:
                yield json.dumps({"type": "sources", "sources": sorted(sources.values())}) + "\n"
        yield json.dumps({"type": "done"}) + "\n"
    except httpx.ConnectError:
        yield json.dumps({"type": "error",
                          "text": f"Can't reach Ollama at {config.OLLAMA_URL}. Is it running? (ollama serve)"}) + "\n"
    except httpx.HTTPError as e:
        yield json.dumps({"type": "error", "text": f"Ollama error: {e}"}) + "\n"
