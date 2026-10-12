#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-3000}"
export PORT
export WHATSAPP_SERVICE_URL="${WHATSAPP_SERVICE_URL:-http://127.0.0.1:${PORT}}"

NODE_PID=""
PY_PID=""

cleanup() {
  echo "[cloud] Encerrando processos..."
  if [[ -n "${PY_PID}" ]]; then kill "${PY_PID}" 2>/dev/null || true; fi
  if [[ -n "${NODE_PID}" ]]; then kill "${NODE_PID}" 2>/dev/null || true; fi
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[cloud] Iniciando WhatsApp/Baileys na porta ${PORT}..."
node whatsapp-service/index.js &
NODE_PID=$!

# Espera o Express responder antes de iniciar o Python.
# Nao exige WhatsApp conectado; apenas confirma que /health esta disponivel.
echo "[cloud] Aguardando /health do servico WhatsApp..."
READY=0
for attempt in $(seq 1 30); do
  if ! kill -0 "$NODE_PID" 2>/dev/null; then
    echo "[cloud] O processo Node encerrou antes de ficar pronto."
    exit 1
  fi

  if python3 - <<PY >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:${PORT}/health", timeout=2).read()
PY
  then
    READY=1
    break
  fi

  sleep 1
done

if [[ "$READY" != "1" ]]; then
  echo "[cloud] /health nao respondeu em 30 segundos."
  exit 1
fi

echo "[cloud] WhatsApp Service HTTP pronto."
echo "[cloud] Iniciando Auvello Python (Descoberta=${DISCOVERY_INTERVAL_MINUTES:-10}m | Especificos=${SPECIFIC_GROUP_INTERVAL_MINUTES:-10}m | Geral=${GENERAL_GROUP_INTERVAL_MINUTES:-5}m)..."
python3 main.py &
PY_PID=$!

# Render deve reiniciar o container se um dos dois processos principais cair.
set +e
wait -n "$NODE_PID" "$PY_PID"
STATUS=$?
set -e

echo "[cloud] Um processo principal encerrou (status ${STATUS})."
exit "$STATUS"
