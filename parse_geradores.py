#!/usr/bin/env python3
"""
parse_geradores.py

Busca os itens do board "Geradores" no Monday.com via API GraphQL e gera
data/geradores.json, no mesmo espirito do parse_excel.py usado para o
dashboard de MTBF/MTTR.

NOVO: compara com a leitura anterior (o proprio data/geradores.json) para
calcular a variacao de horimetro desde a ultima busca e detectar mudancas
de status_operacional, alimentando o ticker estilo "bolsa de valores" e o
destaque de mudanca recente no painel.

Variaveis de ambiente necessarias:
    MONDAY_API_TOKEN   Token de API pessoal do Monday.com

Uso:
    python parse_geradores.py --board-id 18415734554
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

import requests

MONDAY_API_URL = "https://api.monday.com/v2"

# Nomes das colunas como aparecem no board (usados para localizar a coluna
# certa mesmo que o Monday troque o "id" interno da coluna).
COLUMN_ALIASES = {
    "status_operacional": ["status operacion"],
    "horimetro": ["horimetro"],
    "ultimo_teste": ["ultimo teste"],
    "ultima_preventiva": ["ultima preventiva"],
    "proxima_preventiva": ["proxima preventiva"],
    "relatorio_dcco": ["relatorio dcco"],
    "observacoes": ["observaco"],
    "status": ["status"],  # cuidado: precisa vir depois de status_operacional
}


def normalizar(texto: str) -> str:
    """Remove acentos e caixa alta para comparar nomes de coluna com seguranca."""
    if texto is None:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.lower().strip()


def montar_mapa_colunas(colunas_board):
    """
    Recebe a lista de colunas do board (id, title) e devolve um dicionario
    {chave_interna: column_id}, casando pelo titulo normalizado.
    """
    mapa = {}
    usados = set()

    # status_operacional tem prioridade sobre "status" generico, entao
    # processamos essa chave primeiro.
    ordem = ["status_operacional"] + [k for k in COLUMN_ALIASES if k != "status_operacional"]

    for chave in ordem:
        aliases = COLUMN_ALIASES[chave]
        for coluna in colunas_board:
            col_id = coluna["id"]
            if col_id in usados:
                continue
            titulo_norm = normalizar(coluna["title"])
            if any(alias in titulo_norm for alias in aliases):
                mapa[chave] = col_id
                usados.add(col_id)
                break
    return mapa


def extrair_texto(valor_coluna):
    """Extrai um texto legivel de um column_value do Monday."""
    if valor_coluna is None:
        return ""
    texto = valor_coluna.get("text")
    if texto:
        return texto.strip()
    return ""


def extrair_horimetro(valor_coluna):
    """Converte o valor da coluna Horimetro Atual para float (aceita virgula ou ponto)."""
    texto = extrair_texto(valor_coluna)
    if not texto:
        return None
    texto_limpo = re.sub(r"[^0-9.\-]", "", texto.replace(",", "."))
    try:
        return float(texto_limpo)
    except ValueError:
        return None


def buscar_board(board_id: str, token: str):
    query = """
    query ($boardId: [ID!]) {
      boards(ids: $boardId) {
        columns {
          id
          title
        }
        groups {
          title
        }
        items_page(limit: 100) {
          items {
            id
            name
            column_values {
              id
              text
            }
          }
        }
      }
    }
    """
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }
    resposta = requests.post(
        MONDAY_API_URL,
        json={"query": query, "variables": {"boardId": [board_id]}},
        headers=headers,
        timeout=30,
    )
    resposta.raise_for_status()
    corpo = resposta.json()
    if "errors" in corpo:
        raise RuntimeError(f"Erro na API do Monday: {corpo['errors']}")
    boards = corpo["data"]["boards"]
    if not boards:
        raise RuntimeError(f"Board {board_id} nao encontrado (verifique o token e o ID).")
    return boards[0]


def carregar_leitura_anterior(caminho: str):
    """Le o geradores.json existente (se houver) e devolve um dict
    {equipamento: {horimetro, status_operacional}} para comparacao."""
    if not os.path.exists(caminho):
        return {}
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            dados_anteriores = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    anterior_por_equipamento = {}
    for g in dados_anteriores.get("geradores", []):
        anterior_por_equipamento[g["equipamento"]] = {
            "horimetro": g.get("horimetro"),
            "status_operacional": g.get("status_operacional"),
        }
    return anterior_por_equipamento


def montar_json(board: dict, anterior_por_equipamento: dict) -> dict:
    colunas = board["columns"]
    mapa = montar_mapa_colunas(colunas)
    agora = datetime.now(timezone.utc).isoformat()

    geradores = []
    for item in board["items_page"]["items"]:
        valores = {cv["id"]: cv for cv in item["column_values"]}

        def pegar(chave):
            col_id = mapa.get(chave)
            if not col_id:
                return ""
            return extrair_texto(valores.get(col_id))

        horimetro_valor = None
        if "horimetro" in mapa:
            horimetro_valor = extrair_horimetro(valores.get(mapa["horimetro"]))

        status_operacional_atual = pegar("status_operacional") or "Desconhecido"
        equipamento = item["name"]

        # Compara com a leitura anterior para calcular variacao de horimetro
        # e detectar troca de status_operacional (para o ticker e o destaque).
        anterior = anterior_por_equipamento.get(equipamento)
        delta_horimetro = None
        status_mudou = False
        status_anterior = None
        if anterior:
            status_anterior = anterior.get("status_operacional")
            if (
                horimetro_valor is not None
                and anterior.get("horimetro") is not None
            ):
                delta_horimetro = round(horimetro_valor - anterior["horimetro"], 1)
            if status_anterior and status_anterior != status_operacional_atual:
                status_mudou = True

        geradores.append(
            {
                "equipamento": equipamento,
                "status_operacional": status_operacional_atual,
                "horimetro": horimetro_valor,
                "delta_horimetro": delta_horimetro,
                "status_anterior": status_anterior,
                "status_mudou": status_mudou,
                "status_mudou_em": agora if status_mudou else None,
                "ultimo_teste": pegar("ultimo_teste"),
                "ultima_preventiva": pegar("ultima_preventiva"),
                "proxima_preventiva": pegar("proxima_preventiva"),
                "relatorio_dcco": pegar("relatorio_dcco"),
                "observacoes": pegar("observacoes"),
                "status": pegar("status") or "Desconhecido",
            }
        )

    return {
        "atualizado_em": agora,
        "total_geradores": len(geradores),
        "geradores": geradores,
    }


def preservar_status_mudou_recente(dados_novos: dict, caminho: str, janela_horas: float = 2.0):
    """Se um gerador ja tinha status_mudou=True na leitura anterior e ainda
    esta dentro da janela de destaque, mantem o sinal ligado mesmo que essa
    rodada nao tenha detectado troca (evita que o destaque suma no proximo
    ciclo de 5 minutos, antes da janela de tempo acabar)."""
    if not os.path.exists(caminho):
        return dados_novos
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            dados_anteriores = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dados_novos

    anteriores_por_nome = {
        g["equipamento"]: g for g in dados_anteriores.get("geradores", [])
    }

    agora = datetime.now(timezone.utc)
    for g in dados_novos["geradores"]:
        if g["status_mudou"]:
            continue
        anterior = anteriores_por_nome.get(g["equipamento"])
        if not anterior or not anterior.get("status_mudou_em"):
            continue
        try:
            quando = datetime.fromisoformat(anterior["status_mudou_em"])
        except ValueError:
            continue
        horas_passadas = (agora - quando).total_seconds() / 3600
        if horas_passadas < janela_horas:
            g["status_mudou"] = True
            g["status_mudou_em"] = anterior["status_mudou_em"]
            g["status_anterior"] = anterior.get("status_anterior")

    return dados_novos


def main():
    parser = argparse.ArgumentParser(description="Gera geradores.json a partir do board do Monday.com")
    parser.add_argument("--board-id", required=True, help="ID do board do Monday.com (ex: 18415734554)")
    parser.add_argument(
        "--output",
        default=os.path.join(os.path.dirname(__file__), "data", "geradores.json"),
        help="Caminho do arquivo JSON de saida",
    )
    args = parser.parse_args()

    token = os.environ.get("MONDAY_API_TOKEN")
    if not token:
        print("Erro: variavel de ambiente MONDAY_API_TOKEN nao definida.", file=sys.stderr)
        sys.exit(1)

    anterior_por_equipamento = carregar_leitura_anterior(args.output)

    board = buscar_board(args.board_id, token)
    dados = montar_json(board, anterior_por_equipamento)
    dados = preservar_status_mudou_recente(dados, args.output)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

    print(f"OK: {dados['total_geradores']} geradores gravados em {args.output}")


if __name__ == "__main__":
    main()
