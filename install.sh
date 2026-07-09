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

# 5. Comando global
mkdir -p "${BIN_DIR}"
cat > "${LAUNCHER}" <<EOF
#!/usr/bin/env bash
# Lanzador de local_agent (generado por install.sh)
exec "${AGENT_DIR}/.venv/bin/python" "${AGENT_DIR}/agent.py" "\$@"
EOF
chmod +x "${LAUNCHER}"
ok "Comando instalado en ${LAUNCHER}"

# 6. ¿Está ~/.local/bin en el PATH?
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
echo "Instalación completa. Pasos siguientes:"
echo "  1. Edita ${AGENT_DIR}/.env  →  MODEL_ID y (opcional) TAVILY_API_KEY"
echo "  2. Arranca el servidor de LM Studio (Developer → Start Server)"
echo "  3. Ejecuta:  local_agent"
echo
