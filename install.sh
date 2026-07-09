#!/usr/bin/env bash
# Instalador de local_agent: crea el venv, instala dependencias, prepara el
# .env y deja el comando global `local_agent` listo en ~/.local/bin.
# Uso: ./install.sh   (desde la raíz del repositorio clonado)
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
LAUNCHER="${BIN_DIR}/local_agent"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
info() { printf '  \033[36m·\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }

echo
echo "Instalando local_agent desde: ${AGENT_DIR}"
echo

# 1. Python 3.10+
command -v python3 >/dev/null || fail "No se encontró python3. Instálalo primero."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "Se necesita Python 3.10 o superior (tienes $(python3 --version))."
ok "Python $(python3 --version | cut -d' ' -f2)"

# 2. Entorno virtual
if [ ! -f "${AGENT_DIR}/.venv/bin/python" ]; then
    info "Creando entorno virtual (.venv)..."
    python3 -m venv "${AGENT_DIR}/.venv"
fi
ok "Entorno virtual listo"

# 3. Dependencias
info "Instalando dependencias (puede tardar un poco)..."
"${AGENT_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${AGENT_DIR}/.venv/bin/pip" install --quiet -r "${AGENT_DIR}/requirements.txt"
ok "Dependencias instaladas"

# 4. Configuración
if [ ! -f "${AGENT_DIR}/.env" ]; then
    cp "${AGENT_DIR}/.env.example" "${AGENT_DIR}/.env"
    ok "Creado .env a partir de la plantilla"
else
    ok "Ya existe .env (no se toca)"
fi

# 5. Detectar los LLMs de LM Studio y elegir el modelo por defecto
# Python solo lista y aplica; todas las lecturas de teclado las hace bash
# (si Python leyera stdin, su buffer se tragaría las respuestas siguientes).
SELECT_MODEL_PY="$(cat <<'PYEOF'
import json, re, sys, urllib.request

cmd, env_path = sys.argv[1], sys.argv[2]
choice = sys.argv[3].strip() if len(sys.argv) > 3 else ""
text = open(env_path).read()

def get(key, default=""):
    m = re.search(rf"^{key}=(.*)$", text, re.M)
    return m.group(1).strip() if m else default

root = get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").rsplit("/v1", 1)[0]
current = get("MODEL_ID")
try:
    with urllib.request.urlopen(f"{root}/api/v0/models", timeout=5) as r:
        data = json.load(r)["data"]
    models = [m for m in data if m.get("type") in ("llm", "vlm")]
except Exception:
    models = None

if models is None or not models:
    if cmd == "list":
        if models is None:
            print("  · LM Studio no responde (¿servidor apagado?). Configura MODEL_ID en .env cuando quieras.")
        else:
            print("  · LM Studio no tiene LLMs descargados todavía; configura MODEL_ID en .env más tarde.")
    sys.exit(1)

if cmd == "list":
    print("\n  Modelos LLM detectados en tu LM Studio:")
    for i, m in enumerate(models, 1):
        notes = []
        if "tool_use" not in m.get("capabilities", []):
            notes.append("sin tool calling: NO recomendado")
        if m["id"] == current:
            notes.append("actual")
        extra = f"  ({', '.join(notes)})" if notes else ""
        print(f"   {i:>2}. {m['id']}  [ctx {m.get('max_context_length', '?')}]{extra}")
    sys.exit(0)

# cmd == "apply"
if choice.isdigit() and 1 <= int(choice) <= len(models):
    chosen = models[int(choice) - 1]["id"]
    if re.search(r"^MODEL_ID=", text, re.M):
        text = re.sub(r"^MODEL_ID=.*$", f"MODEL_ID={chosen}", text, count=1, flags=re.M)
    else:
        text += f"\nMODEL_ID={chosen}\n"
    open(env_path, "w").write(text)
    print(f"  \033[32m✓\033[0m Modelo por defecto: {chosen}")
else:
    print(f"  \033[32m✓\033[0m Se mantiene MODEL_ID={current or '(plantilla)'}")
PYEOF
)"
if "${AGENT_DIR}/.venv/bin/python" -c "${SELECT_MODEL_PY}" list "${AGENT_DIR}/.env"; then
    printf '\n  ¿Cuál usar por defecto? [número, Enter = mantener actual] '
    read -r MODEL_CHOICE || MODEL_CHOICE=""
    "${AGENT_DIR}/.venv/bin/python" -c "${SELECT_MODEL_PY}" apply "${AGENT_DIR}/.env" "${MODEL_CHOICE}"
fi

# 6. TAVILY_API_KEY (opcional, habilita la búsqueda web)
CURRENT_TAVILY="$(grep -E '^TAVILY_API_KEY=' "${AGENT_DIR}/.env" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
if [ -n "${CURRENT_TAVILY}" ]; then
    ok "TAVILY_API_KEY ya configurada (búsqueda web activa)"
else
    echo
    info "Búsqueda web (opcional): consigue una API key gratuita en https://www.tavily.com/"
    printf '  Pega tu TAVILY_API_KEY [Enter para omitir]: '
    read -r TAVILY_KEY || TAVILY_KEY=""
    TAVILY_KEY="$(printf '%s' "${TAVILY_KEY}" | tr -d '[:space:]')"
    if [ -n "${TAVILY_KEY}" ]; then
        if grep -qE '^TAVILY_API_KEY=' "${AGENT_DIR}/.env"; then
            sed -i "s|^TAVILY_API_KEY=.*|TAVILY_API_KEY=${TAVILY_KEY}|" "${AGENT_DIR}/.env"
        else
            printf 'TAVILY_API_KEY=%s\n' "${TAVILY_KEY}" >> "${AGENT_DIR}/.env"
        fi
        CURRENT_TAVILY="${TAVILY_KEY}"
        ok "TAVILY_API_KEY guardada en .env (búsqueda web activa)"
    else
        info "Omitida; el agente funcionará sin búsqueda web."
    fi
fi

# 7. Comando global
mkdir -p "${BIN_DIR}"
cat > "${LAUNCHER}" <<EOF
#!/usr/bin/env bash
# Lanzador de local_agent (generado por install.sh)
exec "${AGENT_DIR}/.venv/bin/python" "${AGENT_DIR}/agent.py" "\$@"
EOF
chmod +x "${LAUNCHER}"
ok "Comando instalado en ${LAUNCHER}"

# 8. ¿Está ~/.local/bin en el PATH?
case ":${PATH}:" in
    *":${BIN_DIR}:"*)
        ok "~/.local/bin ya está en tu PATH"
        ;;
    *)
        echo
        info "~/.local/bin NO está en tu PATH."
        RC_FILE=""
        case "$(basename "${SHELL:-bash}")" in
            zsh) RC_FILE="${HOME}/.zshrc" ;;
            *)   RC_FILE="${HOME}/.bashrc" ;;
        esac
        if [ -t 0 ]; then
            printf '  ¿Añadir "export PATH=\"$HOME/.local/bin:$PATH\"" a %s? [s/N] ' "${RC_FILE}"
            read -r answer
            if [ "${answer}" = "s" ] || [ "${answer}" = "S" ]; then
                printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "${RC_FILE}"
                ok "Añadido a ${RC_FILE} (abre una terminal nueva o haz: source ${RC_FILE})"
            else
                info "Añádelo tú mismo: export PATH=\"\$HOME/.local/bin:\$PATH\""
            fi
        else
            info "Añade a tu shell: export PATH=\"\$HOME/.local/bin:\$PATH\""
        fi
        ;;
esac

echo
echo "Instalación completa."
if [ -z "${CURRENT_TAVILY}" ]; then
    echo "  · Recuerda: sin TAVILY_API_KEY el agente no puede buscar en internet."
    echo "    Consíguela gratis en https://www.tavily.com/ y pégala en ${AGENT_DIR}/.env"
fi
echo "  · Con el servidor de LM Studio activo (Developer → Start Server), ejecuta:  local_agent"
echo
