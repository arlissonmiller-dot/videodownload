#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
ENABLE_RELOAD="${ENABLE_RELOAD:-0}"

echo "Iniciando backend..."
cd "$ROOT/backend"
source venv/bin/activate
BACKEND_CMD=(uvicorn main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT")
if [ "$ENABLE_RELOAD" = "1" ]; then
  BACKEND_CMD+=(--reload)
fi
"${BACKEND_CMD[@]}" &
BACKEND_PID=$!
deactivate

echo "Iniciando frontend..."
cd "$ROOT/frontend"
npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" &
FRONTEND_PID=$!

echo ""
echo "✅  Aplicação rodando!"
echo "   Frontend: http://$FRONTEND_HOST:$FRONTEND_PORT"
echo "   Backend:  http://$BACKEND_HOST:$BACKEND_PORT"
echo ""
echo "Pressione Ctrl+C para encerrar..."

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo 'Encerrado.'" EXIT INT TERM
wait
