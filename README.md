# local_agent — agente de código sobre LM Studio

Agente estilo Codex que corre 100% en tu máquina: el "cerebro" es el modelo que
tengas en [LM Studio](https://lmstudio.ai) y el loop de agente lo hace el
[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) apuntando al
servidor local. Lee/escribe/edita archivos, ejecuta comandos y código, busca en
internet, hace búsqueda semántica sobre tu código con embeddings locales, tiene
memoria persistente entre sesiones y un sub-agente investigador.

```
╭──────────────────────────────────────────────╮
│ Agente de código local                       │
│ modelo:    openai/gpt-oss-20b                │
│            MXFP4 · contexto 8192 · cargado   │
│ workspace: ~/mi-proyecto                     │
│ sesión:    ws-a1b2c3 (recuerda 12 mensajes)  │
╰──────────────────────────────────────────────╯
tú › arregla el test que falla y explícame por qué fallaba
```

## Requisitos

- **LM Studio** con el servidor activado (**Developer → Start Server**, puerto
  1234) y algún modelo con capacidad de *tool calling* descargado. No hace falta
  cargarlo a mano: LM Studio lo carga al primer uso (JIT).
- **Python 3.10+**.
- Opcional: API key gratuita de [Tavily](https://tavily.com) para la búsqueda
  web, y el modelo de embeddings `nomic-embed-text` en LM Studio para la
  búsqueda semántica.

## Instalación

```bash
git clone https://github.com/<tu-usuario>/local_agent.git
cd local_agent
./install.sh
```

El instalador crea el entorno virtual, instala las dependencias, genera tu
`.env` a partir de la plantilla, **detecta los LLMs de tu LM Studio y te
pregunta cuál usar por defecto**, y deja el comando global `local_agent` en
`~/.local/bin` (avisa si esa carpeta no está en tu PATH). Es idempotente:
puedes relanzarlo cuando quieras (p. ej. tras un `git pull` o para cambiar
el modelo por defecto).

Después, si quieres búsqueda web, pon tu `TAVILY_API_KEY` en `.env`.

<details>
<summary>Instalación manual (sin install.sh)</summary>

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# lanzador global opcional (desde la raíz del repo):
printf '#!/usr/bin/env bash\nexec "%s/.venv/bin/python" "%s/agent.py" "$@"\n' \
    "$(pwd)" "$(pwd)" > ~/.local/bin/local_agent
chmod +x ~/.local/bin/local_agent
```

</details>

## Uso

```bash
local_agent                          # REPL sobre el directorio actual
local_agent --workspace /ruta        # sobre otro proyecto
local_agent -p "arregla el bug X"    # una tarea y salir (scriptable)
local_agent --model <id> --yes       # otro modelo, sin confirmaciones
local_agent --session experimento    # memoria con nombre propio
```

El agente trabaja sobre el directorio donde lo lances y cada workspace tiene su
propia memoria persistente: si cierras y vuelves, recuerda la conversación.

### Comandos del REPL

| Comando | Qué hace |
|---|---|
| `/model` | lista los modelos de LM Studio con estado, contexto y si soportan tool calling |
| `/model <id>` | cambia de modelo en caliente (LM Studio lo carga solo) |
| `/tools` | lista las herramientas del agente |
| `/reset` | borra la memoria de la sesión actual |
| `/help` · `/salir` | ayuda · terminar |

## Herramientas del agente (17)

| Grupo | Herramientas |
|---|---|
| Explorar | `tree`, `list_dir`, `read_file` (con rango de líneas), `find_files` (glob) |
| Buscar código | `search_code` (regex tipo grep), `semantic_search` (embeddings locales, por significado) |
| Modificar | `write_file`, `edit_file`, `move_path`, `delete_path` |
| Ejecutar | `run_shell`, `run_python` (piden confirmación salvo `--yes`/`AUTO_APPROVE=1`) |
| Web | `web_search` (Tavily), `fetch_url`, `http_request`, `investigar` (sub-agente que contrasta varias fuentes) |
| Humano | `ask_user` (el agente te pregunta cuando está bloqueado) |

`semantic_search` usa un modelo de embeddings local de LM Studio: indexa el
workspace por fragmentos (índice incremental cacheado en `~/.cache/local_agent/`)
y encuentra código por significado («dónde se maneja la autenticación»), no solo
por texto literal.

Las rutas de las herramientas de archivos quedan confinadas al workspace. El
shell es un shell real: por eso pide confirmación comando a comando.

## Configuración (`.env`)

| Variable | Qué controla |
|---|---|
| `MODEL_ID` | modelo por defecto (`/model` para ver los disponibles) |
| `LMSTUDIO_BASE_URL` | servidor (defecto `http://localhost:1234/v1`) |
| `TAVILY_API_KEY` | habilita la búsqueda web |
| `TEMPERATURE` / `MAX_RESPONSE_TOKENS` | muestreo y tope de respuesta |
| `MAX_TURNS` | pasos máximos por tarea |
| `MAX_TOOL_OUTPUT` / `SHELL_TIMEOUT` | truncado de salidas / timeout de comandos |
| `LMSTUDIO_TTL` | segundos de inactividad tras los que LM Studio descarga el modelo |
| `EMBEDDING_MODEL_ID` | modelo de embeddings para `semantic_search` |
| `AUTO_APPROVE` | `1` = no pedir confirmaciones |
| `AGENT_DEBUG` | `1` = logs internos del SDK |

Si un proyecto necesita configuración propia (otro modelo, otra temperatura),
pon un `.env` en su raíz: tiene prioridad sobre el del agente.

## Consejos para exprimir LM Studio

- **Contexto**: al cargar por JIT, LM Studio usa el contexto por defecto del
  modelo (a menudo 4-8k). Para tareas largas súbelo en LM Studio; el agente
  muestra el contexto cargado en el banner y los tokens usados tras cada turno.
- **Modelos**: elige uno con capacidad `tool_use` (`/model` marca en rojo los
  que no la tienen). Para código serio, `openai/gpt-oss-20b` o un Qwen ≥ q4
  rinden mucho mejor que los modelos pequeños.
- **Razonamiento**: si el modelo es *reasoning*, verás su pensamiento atenuado
  con 💭 en tiempo real.
- **Velocidad**: activa *speculative decoding* en LM Studio (modelo draft
  pequeño de la misma familia).
- **RAM**: con `LMSTUDIO_TTL=600` el modelo se descarga solo tras 10 min de
  inactividad; el auto-evict de LM Studio mantiene un solo LLM cargado.

## Extender el agente

- **Herramienta nueva**: en `tools.py`, función con type hints y docstring
  estilo Google, decorada con `@function_tool`, y añádela a `ALL_TOOLS`. El SDK
  genera el JSON schema solo.
- **Sub-agente nuevo**: en `agent.py`, crea otro `Agent(...)` y expónlo con
  `.as_tool(...)` (mira `investigar` como ejemplo).

## Estructura

```
agent.py    REPL, streaming, sesiones, sub-agente, integración LM Studio
tools.py    las 17 herramientas y la sandbox del workspace
.env        tu configuración (no se sube a git; plantilla en .env.example)
```
