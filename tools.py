"""Herramientas del agente: archivos, código, ejecución, web e interacción.

Todas las rutas se resuelven dentro del WORKSPACE (por defecto, el directorio
desde el que se lanza el agente). Las acciones peligrosas (shell, ejecutar
código, borrar) piden confirmación salvo que AUTO_APPROVE=1.
"""

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
from agents import function_tool
from bs4 import BeautifulSoup
from rich.console import Console

console = Console()

WORKSPACE = Path(os.getenv("WORKSPACE", os.getcwd())).resolve()
MAX_OUTPUT = int(os.getenv("MAX_TOOL_OUTPUT", "8000"))
SHELL_TIMEOUT = int(os.getenv("SHELL_TIMEOUT", "120"))
AUTO_APPROVE = os.getenv("AUTO_APPROVE", "0") == "1"
LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
EMBEDDING_MODEL_ID = os.getenv("EMBEDDING_MODEL_ID", "text-embedding-nomic-embed-text-v1.5")

CACHE_DIR = Path.home() / ".cache" / "local_agent"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".agent_cache",
             "dist", "build", ".idea", ".vscode", ".pytest_cache", ".mypy_cache", "target"}
TEXT_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".md", ".txt", ".html", ".css",
             ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".bash", ".sql", ".rs", ".go",
             ".java", ".c", ".h", ".cpp", ".hpp", ".rb", ".php", ".xml", ".csv", ".env",
             ".gitignore", ".dockerfile", ""}


# ---------------------------------------------------------------- utilidades

def _resolve(path: str) -> Path:
    """Resuelve una ruta relativa al workspace y bloquea escapes fuera de él."""
    p = (WORKSPACE / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    if not p.is_relative_to(WORKSPACE):
        raise ValueError(f"Ruta fuera del workspace ({WORKSPACE}): {path}")
    return p


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... [salida truncada a {MAX_OUTPUT} caracteres]"


def _confirm(what: str, detail: str) -> bool:
    """Pide confirmación al usuario para una acción peligrosa."""
    if AUTO_APPROVE:
        return True
    console.print(f"\n[bold yellow]El agente quiere {what}:[/bold yellow] [white]{detail}[/white]")
    try:
        answer = console.input("[bold yellow]¿Permitir? \\[s/N][/bold yellow] ").strip().lower()
    except EOFError:
        return False
    return answer in ("s", "si", "sí", "y", "yes")


def _walk_text_files():
    for root, dirs, files in os.walk(WORKSPACE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            p = Path(root) / name
            if p.suffix.lower() in TEXT_EXTS or p.name in (".env", "Makefile", "Dockerfile"):
                try:
                    if p.stat().st_size < 300_000:
                        yield p
                except OSError:
                    continue


# ------------------------------------------------------------------ archivos

@function_tool
def list_dir(path: str) -> str:
    """Lista archivos y subdirectorios de un directorio, con tamaños.

    Args:
        path: Ruta del directorio relativa al workspace. Usa "." para la raíz.
    """
    p = _resolve(path)
    if not p.is_dir():
        return f"Error: {path} no es un directorio."
    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    lines = []
    for e in entries:
        if e.is_dir():
            lines.append(f"d        {e.name}/")
        else:
            lines.append(f"f {e.stat().st_size:>7} {e.name}")
    return _clip("\n".join(lines) or "(directorio vacío)")


@function_tool
def tree(path: str, max_depth: int = 3) -> str:
    """Muestra la estructura de directorios en árbol para hacerse una idea del proyecto.

    Args:
        path: Directorio raíz del árbol, relativo al workspace ("." para todo).
        max_depth: Profundidad máxima (3 suele bastar).
    """
    root = _resolve(path)
    if not root.is_dir():
        return f"Error: {path} no es un directorio."
    lines = []

    def walk(d: Path, prefix: str, depth: int):
        if depth > max_depth or len(lines) > 400:
            return
        entries = sorted([e for e in d.iterdir()
                          if e.name not in SKIP_DIRS and not e.name.startswith(".")],
                         key=lambda e: (e.is_file(), e.name.lower()))
        for e in entries:
            lines.append(f"{prefix}{e.name}{'/' if e.is_dir() else ''}")
            if e.is_dir():
                walk(e, prefix + "  ", depth + 1)

    lines.append(f"{root.name or root}/")
    walk(root, "  ", 1)
    return _clip("\n".join(lines))


@function_tool
def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Lee un archivo de texto del workspace, entero o un rango de líneas.

    Args:
        path: Ruta del archivo relativa al workspace.
        start_line: Primera línea a leer (1-indexada). 0 = desde el principio.
        end_line: Última línea (inclusive). 0 = hasta el final.
    """
    p = _resolve(path)
    if not p.is_file():
        return f"Error: {path} no existe o no es un archivo."
    text = p.read_text(encoding="utf-8", errors="replace")
    if start_line or end_line:
        all_lines = text.splitlines()
        s = max(start_line, 1)
        e = end_line if end_line else len(all_lines)
        text = "\n".join(all_lines[s - 1:e])
        return _clip(f"[líneas {s}-{min(e, len(all_lines))} de {len(all_lines)}]\n{text}")
    return _clip(text)


@function_tool
def write_file(path: str, content: str) -> str:
    """Crea o sobrescribe un archivo con el contenido dado. Crea directorios intermedios.

    Args:
        path: Ruta del archivo relativa al workspace.
        content: Contenido completo que tendrá el archivo.
    """
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    console.print(f"[dim]  ✏ escrito {p.relative_to(WORKSPACE)} ({len(content)} caracteres)[/dim]")
    return f"OK: escrito {path} ({len(content)} caracteres)."


@function_tool
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """Reemplaza un fragmento exacto de texto dentro de un archivo existente.

    old_text debe aparecer exactamente una vez. Para cambios grandes usa write_file.

    Args:
        path: Ruta del archivo relativa al workspace.
        old_text: Texto exacto a sustituir (único en el archivo).
        new_text: Texto de reemplazo.
    """
    p = _resolve(path)
    if not p.is_file():
        return f"Error: {path} no existe."
    text = p.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        return "Error: old_text no aparece en el archivo. Léelo con read_file y copia el texto exacto."
    if count > 1:
        return f"Error: old_text aparece {count} veces; amplíalo para que sea único."
    p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    console.print(f"[dim]  ✏ editado {p.relative_to(WORKSPACE)}[/dim]")
    return f"OK: editado {path}."


@function_tool
def move_path(source: str, destination: str) -> str:
    """Mueve o renombra un archivo o directorio dentro del workspace.

    Args:
        source: Ruta actual, relativa al workspace.
        destination: Ruta nueva, relativa al workspace.
    """
    src, dst = _resolve(source), _resolve(destination)
    if not src.exists():
        return f"Error: {source} no existe."
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    console.print(f"[dim]  ↪ movido {source} → {destination}[/dim]")
    return f"OK: {source} → {destination}."


@function_tool
def delete_path(path: str) -> str:
    """Borra un archivo o un directorio (recursivo) del workspace. Pide confirmación al usuario.

    Args:
        path: Ruta a borrar, relativa al workspace.
    """
    p = _resolve(path)
    if not p.exists():
        return f"Error: {path} no existe."
    kind = "directorio" if p.is_dir() else "archivo"
    if not _confirm(f"borrar el {kind}", str(p)):
        return "El usuario rechazó el borrado."
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return f"OK: borrado {path}."


# ---------------------------------------------------------- búsqueda de código

@function_tool
def find_files(pattern: str) -> str:
    """Busca archivos por nombre con un patrón glob recursivo.

    Args:
        pattern: Patrón glob, p. ej. "**/*.py" o "src/**/test_*.js".
    """
    matches = []
    for p in sorted(WORKSPACE.glob(pattern)):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        matches.append(str(p.relative_to(WORKSPACE)) + ("/" if p.is_dir() else ""))
        if len(matches) >= 200:
            matches.append("... [más resultados omitidos]")
            break
    return _clip("\n".join(matches) or "Sin coincidencias.")


@function_tool
def search_code(pattern: str, file_glob: str = "") -> str:
    """Busca una expresión regular en el contenido de los archivos de texto (como grep).

    Args:
        pattern: Expresión regular a buscar, p. ej. "def main" o "TODO|FIXME".
        file_glob: Opcional, limita a archivos que casen con este glob (p. ej. "*.py"). Vacío = todos.
    """
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"Error: regex inválida: {e}"
    results = []
    for p in _walk_text_files():
        if file_glob and not p.match(file_glob):
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    rel = p.relative_to(WORKSPACE)
                    results.append(f"{rel}:{i}: {line.strip()[:160]}")
                    if len(results) >= 100:
                        results.append("... [más resultados omitidos]")
                        return _clip("\n".join(results))
        except OSError:
            continue
    return _clip("\n".join(results) or "Sin coincidencias.")


# ------------------------------------------------- búsqueda semántica (embeddings)

def _embed(texts: list[str]) -> list[list[float]]:
    """Embeddings vía LM Studio (modelo local nomic)."""
    vectors = []
    for i in range(0, len(texts), 32):
        resp = requests.post(
            f"{LMSTUDIO_BASE_URL}/embeddings",
            json={"model": EMBEDDING_MODEL_ID, "input": texts[i:i + 32]},
            timeout=180,
        )
        resp.raise_for_status()
        vectors.extend(item["embedding"] for item in resp.json()["data"])
    return vectors


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _index_path() -> Path:
    ws_hash = hashlib.md5(str(WORKSPACE).encode()).hexdigest()[:12]
    return CACHE_DIR / f"semantic-index-{ws_hash}.json"


def _build_or_update_index() -> dict:
    """Índice incremental: solo (re)embebe archivos nuevos o modificados."""
    index_file = _index_path()
    index = {}
    if index_file.exists():
        try:
            index = json.loads(index_file.read_text())
        except (json.JSONDecodeError, OSError):
            index = {}

    current = {}
    for p in _walk_text_files():
        rel = str(p.relative_to(WORKSPACE))
        current[rel] = p.stat().st_mtime

    for rel in list(index):
        if rel not in current:
            del index[rel]

    pending = [rel for rel, mtime in current.items()
               if rel not in index or index[rel]["mtime"] != mtime]

    total_chunks = sum(len(v["chunks"]) for v in index.values())
    for rel in pending:
        if total_chunks > 3000:
            break
        text = (WORKSPACE / rel).read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        chunks = []
        step, size = 40, 50
        for start in range(0, max(len(lines), 1), step):
            chunk_lines = lines[start:start + size]
            body = "\n".join(chunk_lines).strip()
            if body:
                chunks.append({"start": start + 1, "end": start + len(chunk_lines), "text": body})
        if chunks:
            vectors = _embed([f"{rel}\n{c['text']}" for c in chunks])
            for c, v in zip(chunks, vectors):
                c["vector"] = v
                del c["text"]
            total_chunks += len(chunks)
        index[rel] = {"mtime": current[rel], "chunks": chunks}

    index_file.write_text(json.dumps(index))
    return index


@function_tool
def semantic_search(query: str) -> str:
    """Búsqueda semántica en el código del workspace usando embeddings locales.

    A diferencia de search_code (texto literal/regex), encuentra código por
    significado: "dónde se maneja la autenticación", "parseo del config", etc.

    Args:
        query: Qué buscas, en lenguaje natural.
    """
    t0 = time.time()
    try:
        index = _build_or_update_index()
        qvec = _embed([query])[0]
    except requests.RequestException as e:
        return (f"Error con el servidor de embeddings de LM Studio: {e}. "
                f"Comprueba que el modelo '{EMBEDDING_MODEL_ID}' está disponible.")
    scored = []
    for rel, info in index.items():
        for c in info["chunks"]:
            scored.append((_cosine(qvec, c["vector"]), rel, c["start"], c["end"]))
    if not scored:
        return "El workspace no tiene archivos de texto indexables."
    scored.sort(reverse=True)
    out = [f"Top resultados (índice: {sum(len(v['chunks']) for v in index.values())} fragmentos, {time.time() - t0:.1f}s):"]
    for score, rel, start, end in scored[:6]:
        try:
            lines = (WORKSPACE / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            snippet = "\n".join(lines[start - 1:start - 1 + 8])
        except OSError:
            snippet = ""
        out.append(f"\n── {rel}:{start}-{end} (similitud {score:.2f})\n{snippet}")
    return _clip("\n".join(out))


# ----------------------------------------------------------------- ejecución

@function_tool
def run_shell(command: str) -> str:
    """Ejecuta un comando de shell (bash) en el workspace y devuelve stdout/stderr.

    Para ejecutar código, tests, git, instalar dependencias, etc.

    Args:
        command: Comando bash a ejecutar.
    """
    if not _confirm("ejecutar", command):
        return "El usuario rechazó la ejecución. Propón una alternativa o pregúntale con ask_user."
    try:
        result = subprocess.run(command, shell=True, cwd=WORKSPACE, capture_output=True,
                                text=True, timeout=SHELL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"Error: el comando superó el timeout de {SHELL_TIMEOUT}s."
    out = ""
    if result.stdout:
        out += f"stdout:\n{result.stdout}\n"
    if result.stderr:
        out += f"stderr:\n{result.stderr}\n"
    out += f"exit code: {result.returncode}"
    return _clip(out)


@function_tool
def run_python(code: str) -> str:
    """Ejecuta un fragmento de código Python y devuelve su salida.

    Útil para cálculos rápidos o probar ideas sin crear archivos. Dispone de
    la librería estándar más requests y bs4. Usa print() para ver resultados.

    Args:
        code: Código Python a ejecutar.
    """
    preview = code if len(code) <= 200 else code[:200] + "…"
    if not _confirm("ejecutar Python", preview):
        return "El usuario rechazó la ejecución."
    try:
        result = subprocess.run([sys.executable, "-c", code], cwd=WORKSPACE,
                                capture_output=True, text=True, timeout=SHELL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"Error: superó el timeout de {SHELL_TIMEOUT}s."
    out = ""
    if result.stdout:
        out += f"stdout:\n{result.stdout}\n"
    if result.stderr:
        out += f"stderr:\n{result.stderr}\n"
    out += f"exit code: {result.returncode}"
    return _clip(out)


# ----------------------------------------------------------------------- web

@function_tool
def web_search(query: str) -> str:
    """Busca en internet (Tavily) y devuelve un resumen, títulos, URLs y extractos.

    Para documentación, errores, librerías, noticias. Abre después un resultado
    concreto con fetch_url si necesitas más detalle.

    Args:
        query: Consulta de búsqueda; en inglés suele dar mejores resultados.
    """
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        return ("Error: falta TAVILY_API_KEY en .env. Informa al usuario: cuenta gratuita "
                "en https://tavily.com y pegar la clave en .env.")
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": 5, "include_answer": True},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"Error consultando Tavily: {e}"
    parts = []
    if data.get("answer"):
        parts.append(f"Resumen: {data['answer']}\n")
    for i, r in enumerate(data.get("results", []), 1):
        parts.append(f"{i}. {r.get('title')}\n   {r.get('url')}\n   {r.get('content', '')[:300]}")
    return _clip("\n".join(parts) or "Sin resultados.")


@function_tool
def fetch_url(url: str) -> str:
    """Descarga una página web y devuelve su texto sin HTML.

    Args:
        url: URL completa (http/https) de la página a leer.
    """
    try:
        resp = requests.get(url, timeout=30,
                            headers={"User-Agent": "Mozilla/5.0 (local-agent)"})
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"Error descargando {url}: {e}"
    if "html" in resp.headers.get("content-type", ""):
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = "\n".join(s for s in (line.strip() for line in soup.get_text("\n").splitlines()) if s)
    else:
        text = resp.text
    return _clip(text)


@function_tool
def http_request(method: str, url: str, headers_json: str, body: str) -> str:
    """Hace una petición HTTP arbitraria (para probar APIs REST).

    Args:
        method: GET, POST, PUT, PATCH o DELETE.
        url: URL completa del endpoint.
        headers_json: Cabeceras como JSON, p. ej. '{"Authorization": "Bearer x"}'. Usa '{}' si no hay.
        body: Cuerpo de la petición (texto o JSON). Cadena vacía si no aplica.
    """
    try:
        headers = json.loads(headers_json) if headers_json.strip() else {}
    except json.JSONDecodeError as e:
        return f"Error: headers_json no es JSON válido: {e}"
    try:
        resp = requests.request(method.upper(), url, headers=headers,
                                data=body.encode() if body else None, timeout=30)
    except requests.RequestException as e:
        return f"Error en la petición: {e}"
    return _clip(f"status: {resp.status_code}\ncontent-type: {resp.headers.get('content-type')}\n\n{resp.text}")


# --------------------------------------------------------------- interacción

@function_tool
def ask_user(question: str) -> str:
    """Hace una pregunta al usuario y devuelve su respuesta escrita.

    Úsalo cuando estés bloqueado: falta información, hay que elegir entre
    opciones, o necesitas permiso/aclaración antes de seguir.

    Args:
        question: La pregunta, clara y concreta.
    """
    console.print(f"\n[bold magenta]El agente pregunta:[/bold magenta] {question}")
    try:
        answer = console.input("[bold magenta]tu respuesta ›[/bold magenta] ").strip()
    except EOFError:
        return "(El usuario no está disponible para responder; decide tú razonablemente.)"
    return answer or "(sin respuesta)"


ALL_TOOLS = [
    list_dir, tree, read_file, write_file, edit_file, move_path, delete_path,
    find_files, search_code, semantic_search,
    run_shell, run_python,
    web_search, fetch_url, http_request,
    ask_user,
]

# Subconjunto para el sub-agente investigador (solo lectura de la web)
RESEARCH_TOOLS = [web_search, fetch_url]
