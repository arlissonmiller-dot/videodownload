#!/bin/bash
set -e

echo "=== Video Downloader - Setup ==="
echo ""

# Check dependencies
check_cmd() {
  if ! command -v "$1" &>/dev/null; then
    echo "❌  $1 não encontrado. Instale com: $2"
    exit 1
  else
    echo "✅  $1 encontrado"
  fi
}

echo "Verificando dependências..."
check_cmd python3 "brew install python"
check_cmd ffmpeg "brew install ffmpeg"
check_cmd node "brew install node"
echo ""

# Backend
echo "Configurando backend..."
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt
deactivate
cd ..
echo "✅  Backend configurado"
echo ""

# Frontend
echo "Configurando frontend..."
cd frontend
npm install --silent
cd ..
echo "✅  Frontend configurado"
echo ""

echo "=== Setup concluído! ==="
echo ""
echo "Para iniciar, execute: ./start.sh"
