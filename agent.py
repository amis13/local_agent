#!/usr/bin/env python3
"""Agente de código local: OpenAI Agents SDK + LM Studio.

Uso:
    python agent.py                          # REPL en el directorio actual
    python agent.py -p "haz X"               # una tarea y salir (scriptable)
    python agent.py --workspace /ruta        # trabajar sobre otro proyecto
    python agent.py --model <id> --yes       # otro modelo, sin confirmaciones

Dentro del REPL: /help /model /tools /reset /salir
"""

import argparse
import asyncio
import hashlib
import os
import platform
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Primero el .env del proyecto donde estés (si existe), luego el del agente
# como valores por defecto (load_dotenv no pisa variables ya definidas).
load_dotenv()
load_dotenv(Path(__file__).resolve().parent / ".env")


def _parse_args():
    ap = argparse.ArgumentParser(description="Agente de código sobre LM Studio")
    ap.add_argument("-p", "--prompt", help="ejecuta una sola tarea y sale (modo no interactivo)")
    ap.add_argument("--workspace", help="directorio de trabajo (defecto: el actual)")
    ap.add_argument("--model", help="id del modelo en LM Studio (defecto: MODEL_ID de .env)")
    ap.add_argument("--session", help="nombre de sesión para la memoria persistente")
    ap.add_argument("--yes", action="store_true", help="no pedir confirmación para shell/borrados")
    ap.add_argument("--max-turns", type=int, help="máximo de pasos por tarea")
    return ap.parse_args()


ARGS = _parse_args()
if ARGS.workspace:
    os.environ["WORKSPACE"] = ARGS.workspace
if ARGS.yes:
    os.environ["AUTO_APPROVE"] = "1"

# tools lee WORKSPACE/AUTO_APPROVE del entorno: importar después de fijarlos
import requests
from openai import AsyncOpenAI
from openai.types.responses import ResponseTextDeltaEvent

# LM Studio envía el razonamiento como reasoning_content; el SDK lo convierte
# en eventos "summary". Escuchamos ambas variantes por compatibilidad.
REASONING_EVENT_TYPES = (
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
)

from agents import (
    Agent,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelSettings,
    OpenAIChatCompletionsModel,
    Runner,
    SQLiteSession,
    enable_verbose_stdout_logging,
    set_tracing_disabled,
)
from rich.console import Console
from rich.panel import Panel

import tools

BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
LMSTUDIO_ROOT = BASE_URL.rsplit("/v1", 1)[0]
MODEL_ID = ARGS.model or os.getenv("MODEL_ID", "gemma-4-e4b-uncensored-hauhaucs-aggressive-q8_k_p")
MAX_TURNS = ARGS.max_turns or int(os.getenv("MAX_TURNS", "25"))

console = Console()

if os.getenv("AGENT_DEBUG", "0") == "1":
    enable_verbose_stdout_logging()


# ------------------------------------------------------- metadatos LM Studio

def lmstudio_models() -> dict:
    """Metadatos de la API nativa de LM Studio (/api/v0): estado, contexto, capacidades."""
    try:
        resp = requests.get(f"{LMSTUDIO_ROOT}/api/v0/models", timeout=5)
        resp.raise_for_status()
        return {m["id"]: m for m in resp.json().get("data", [])}
    except requests.RequestException:
        return {}


def describe_model(model_id: str, meta: dict) -> str:
    m = meta.get(model_id)
    if not m:
        return f"modelo:    {model_id} [sin metadatos: ¿servidor apagado?]"
    state = "cargado" if m.get("state") == "loaded" else "se cargará al usarlo (JIT)"
    ctx = m.get("loaded_context_length") or m.get("max_context_length", "?")
    quant = m.get("quantization", "?")
    line = f"modelo:    {model_id}\n           {quant} · contexto {ctx} tokens · {state}"
    if "tool_use" not in m.get("capabilities", []):
        line += "\n           ⚠ este modelo NO declara tool calling: el agente puede fallar"
    return line


# ------------------------------------------------------------------- agentes

def make_agent(model_id: str) -> Agent:
    client = AsyncOpenAI(base_url=BASE_URL, api_key=os.getenv("LMSTUDIO_API_KEY", "lm-studio"))
    model = OpenAIChatCompletionsModel(model=model_id, openai_client=client)

    extra_body = {}
    if os.getenv("LMSTUDIO_TTL", "").strip():
        extra_body["ttl"] = int(os.getenv("LMSTUDIO_TTL"))  # auto-descarga tras inactividad
    settings = ModelSettings(
        temperature=float(os.getenv("TEMPERATURE")) if os.getenv("TEMPERATURE", "").strip() else None,
        max_tokens=int(os.getenv("MAX_RESPONSE_TOKENS")) if os.getenv("MAX_RESPONSE_TOKENS", "").strip() else None,
        include_usage=True,
        extra_body=extra_body or None,
    )

    researcher = Agent(
        name="Investigador",
        instructions=(
            "Eres un investigador web. Responde siempre en español. Usa web_search para "
            "encontrar fuentes y fetch_url para leer las más prometedoras. Contrasta al menos "
            "dos fuentes. Devuelve una síntesis clara con los datos concretos encontrados y "
            "termina listando las URLs consultadas."
        ),
        model=model,
        model_settings=settings,
        tools=tools.RESEARCH_TOOLS,
    )
    investigar = researcher.as_tool(
        tool_name="investigar",
        tool_description=(
            "Encarga a un sub-agente una investigación web en profundidad sobre un tema "
            "(varias búsquedas y lecturas de páginas) y devuelve una síntesis con fuentes. "
            "Para dudas puntuales usa web_search directamente; esto es para temas que "
            "requieren contrastar varias fuentes."
        ),
        max_turns=8,
    )

    instructions = f"""Eres un agente de programación que trabaja en la máquina del usuario.
Fecha: {date.today().isoformat()} · Sistema: {platform.system()} ({platform.machine()})
Workspace (directorio de trabajo): {tools.WORKSPACE}

Método de trabajo:
1. Explora antes de tocar: tree/list_dir para orientarte, read_file para leer.
   Para localizar código usa search_code (texto exacto/regex) o semantic_search (por significado).
2. Modifica con edit_file (cambios puntuales) o write_file (archivos nuevos/reescrituras).
3. Verifica SIEMPRE tu trabajo ejecutándolo: run_shell (comandos, tests) o run_python (pruebas rápidas).
4. Si algo falla, lee el error, corrige y reintenta. No des una tarea por terminada sin verificarla.
5. Información externa: web_search/fetch_url para dudas puntuales, investigar para temas complejos,
   http_request para probar APIs.
6. Si estás bloqueado o hay una decisión importante, pregunta con ask_user.

Reglas:
- Responde siempre en español, breve y directo.
- No inventes contenido de archivos ni APIs: lee y comprueba.
- Rutas relativas al workspace.
- Al terminar, resume en 2-3 frases qué hiciste y cómo lo verificaste."""

    return Agent(
        name="AgenteDeCodigo",
        instructions=instructions,
        model=model,
        model_settings=settings,
        tools=tools.ALL_TOOLS + [investigar],
    )


# ----------------------------------------------------------------- streaming

DIM, RESET = "\033[2m", "\033[0m"


async def run_turn(agent: Agent, user_input: str, session) -> None:
    """Un turno del agente: razonamiento atenuado, texto en streaming, tool calls."""
    result = Runner.run_streamed(agent, user_input, session=session, max_turns=MAX_TURNS)
    mode = None  # None | "reasoning" | "text"

    def switch(new_mode):
        nonlocal mode
        if mode is not None and mode != new_mode:
            if mode == "reasoning":
                print(RESET, end="")
            print()
        mode = new_mode

    try:
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                data = event.data
                if getattr(data, "type", "") in REASONING_EVENT_TYPES:
                    if mode != "reasoning":
                        switch("reasoning")
                        print(f"{DIM}💭 ", end="")
                    print(data.delta, end="", flush=True)
                elif isinstance(data, ResponseTextDeltaEvent) and data.delta:
                    if mode != "text":
                        switch("text")
                    print(data.delta, end="", flush=True)
            elif event.type == "run_item_stream_event":
                item = event.item
                if item.type == "tool_call_item":
                    switch(None)
                    name = getattr(item.raw_item, "name", "?")
                    args = (getattr(item.raw_item, "arguments", "") or "").replace("\n", " ")
                    console.print(f"[dim]→ {name}({args[:180]})[/dim]")
                    mode = None
                elif item.type == "tool_call_output_item":
                    preview = str(item.output).replace("\n", " ")[:160]
                    console.print(f"[dim]← {preview}[/dim]")
    except KeyboardInterrupt:
        try:
            result.cancel()
        except Exception:
            pass
        print(RESET)
        console.print("[yellow]Turno interrumpido con Ctrl+C.[/yellow]")
        return
    if mode == "reasoning":
        print(RESET, end="")
    if mode is not None:
        print()

    u = result.context_wrapper.usage
    if u and u.requests:
        console.print(
            f"[dim]· {u.requests} llamadas al modelo · {u.input_tokens} tokens entrada "
            f"· {u.output_tokens} salida[/dim]"
        )


# ---------------------------------------------------------------------- REPL

HELP = """[bold]Comandos[/bold]
  /model          lista los modelos de LM Studio (estado, contexto, tool calling)
  /model <id>     cambia de modelo en caliente (LM Studio lo carga solo)
  /tools          lista las herramientas del agente
  /reset          borra la memoria de esta sesión
  /help           esta ayuda · /salir termina

La conversación se guarda en disco: si cierras y vuelves a abrir con la misma
sesión, el agente recuerda lo anterior."""


def print_models(meta: dict, current: str) -> None:
    if not meta:
        console.print("[red]No pude leer /api/v0/models. ¿Está el servidor de LM Studio activo?[/red]")
        return
    for mid, m in meta.items():
        if m.get("type") not in ("llm", "vlm"):
            continue
        mark = "[bold green]●[/bold green]" if mid == current else ("[green]○[/green]" if m.get("state") == "loaded" else "[dim]○[/dim]")
        tool_ok = "" if "tool_use" in m.get("capabilities", []) else " [red](sin tool calling)[/red]"
        ctx = m.get("max_context_length", "?")
        console.print(f" {mark} {mid} [dim]· {m.get('quantization', '?')} · ctx {ctx}[/dim]{tool_ok}")


async def main() -> None:
    set_tracing_disabled(True)  # sin esto el SDK intenta subir trazas a OpenAI

    global MODEL_ID
    agent = make_agent(MODEL_ID)

    session_id = ARGS.session or f"ws-{hashlib.md5(str(tools.WORKSPACE).encode()).hexdigest()[:12]}"
    session = SQLiteSession(session_id, tools.CACHE_DIR / "sessions.db")
    previous = len(await session.get_items())

    if ARGS.prompt:  # modo una-tarea (scriptable)
        await run_turn(agent, ARGS.prompt, session)
        return

    meta = lmstudio_models()
    console.print(Panel.fit(
        f"[bold]Agente de código local[/bold]\n"
        f"{describe_model(MODEL_ID, meta)}\n"
        f"servidor:  {BASE_URL}\n"
        f"workspace: {tools.WORKSPACE}\n"
        f"sesión:    {session_id}"
        + (f" [dim](recuerda {previous} mensajes previos)[/dim]" if previous else "")
        + "\n\n[dim]/help para comandos · /salir para terminar[/dim]",
        border_style="cyan",
    ))
    if not os.getenv("TAVILY_API_KEY", "").strip():
        console.print("[yellow]Aviso: sin TAVILY_API_KEY en .env — búsqueda web deshabilitada.[/yellow]")

    while True:
        try:
            user_input = console.input("\n[bold cyan]tú ›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input:
            continue
        if user_input in ("/salir", "/exit", "exit", "quit"):
            break
        if user_input == "/help":
            console.print(HELP)
            continue
        if user_input == "/tools":
            for t in agent.tools:
                console.print(f"  [bold]{t.name}[/bold][dim] — {(t.description or '').splitlines()[0]}[/dim]")
            continue
        if user_input == "/reset":
            await session.clear_session()
            console.print("[dim]Memoria de la sesión borrada.[/dim]")
            continue
        if user_input.startswith("/model"):
            arg = user_input[len("/model"):].strip()
            meta = lmstudio_models()
            if not arg:
                print_models(meta, MODEL_ID)
                continue
            if meta and arg not in meta:
                console.print(f"[red]'{arg}' no está en LM Studio. /model para ver la lista.[/red]")
                continue
            MODEL_ID = arg
            agent = make_agent(MODEL_ID)
            console.print(describe_model(MODEL_ID, meta))
            continue

        try:
            await run_turn(agent, user_input, session)
        except MaxTurnsExceeded:
            console.print(f"\n[yellow]Alcanzado el límite de {MAX_TURNS} pasos (MAX_TURNS). "
                          f"Divide la tarea o súbelo en .env.[/yellow]")
        except ModelBehaviorError as e:
            console.print(f"\n[red]El modelo produjo una salida inválida: {e}[/red]\n"
                          f"[dim]Típico de modelos pequeños/cuantizados. Reintenta o cambia de modelo con /model.[/dim]")
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]\n"
                          f"[dim]Comprueba que LM Studio está corriendo y el modelo cargado.[/dim]")

    console.print("[dim]Hasta luego.[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
