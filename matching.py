"""Matching item-da-cotação -> produto do catálogo via Gemini, restrito ao
histórico de compras do fornecedor.

Por que IA e não fuzzy: fornecedor e Napel usam VOCABULÁRIOS diferentes pro
mesmo produto (caso real 3F, proposta 1020: "BUCHA SUP. DO JUMELO TRAS PICK-UP
CURRIER" = "BUCHA MOLA TR FO COURIER ALGEMA"; "KIT REPARO DO FEIXE DE MOLAS
TRASEIRO RANGER" = "REP COMPL MOLA TR FO RANGER"). Similaridade textual não
cruza sinônimo (JUMELO=ALGEMA), abreviação (REP COMPL, TR, FO) nem erro de
grafia (CURRIER=COURIER) — e com o catálogo inteiro como candidato ela escolhe
o produto ERRADO da mesma família (matchou "PICK-UP CORSA"). Restringir os
candidatos ao que a Napel JÁ COMPROU desse fornecedor (lista pequena) + deixar
a IA decidir resolveu 3/3 com confiança alta no caso real.

Mesmo padrão REST/httpx do extracao.py.
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

MODEL = os.environ.get("COMPARADOR_MODEL", "gemini-2.5-flash")

_PROMPT_TEMPLATE = """Você é especialista em produtos e autopeças no Brasil. Uma cotação de
fornecedor chegou com os itens abaixo (vocabulário do FORNECEDOR). O catálogo
interno da empresa (vocabulário próprio, cheio de abreviações: TR=traseiro,
DT=dianteiro, FO=Ford, CH=Chevrolet, FT=Fiat, TY=Toyota, VW=Volkswagen,
REP COMPL=reparo completo, PI=pino, EC=e-commerce) tem os produtos JÁ COMPRADOS
desse mesmo fornecedor listados abaixo.

Para CADA item da cotação, diga qual produto do catálogo é O MESMO produto
físico (mesma peça/mercadoria, mesmo veículo/aplicação quando houver). Considere
sinônimos (ex: JUMELO=ALGEMA, KIT REPARO=REP COMPL), abreviações e erros de
grafia (ex: CURRIER=COURIER). Se nenhum produto do catálogo for claramente o
mesmo, use null. NÃO chute: em autopeça, veículo e tipo de peça precisam bater.

ITENS DA COTAÇÃO:
{itens}

CATÁLOGO (produtos já comprados desse fornecedor):
{historico}

Responda APENAS JSON válido:
{{"mapeamentos": [{{"codigo": "<código do item da cotação>", "cod_produto": "<cod_produto do catálogo ou null>", "confianca": "alta|media|baixa"}}]}}"""


def mapear_itens_por_ia(
    itens: list[dict],
    historico: list[dict],
    api_key: str,
) -> dict[str, dict]:
    """itens: [{codigo, descricao}] · historico: [{cod_produto, descricao}].
    Retorna {codigo_item: {"cod_produto": str, "confianca": "alta"|"media"}}.
    Confiança "baixa" e null são descartados. Nunca lança — em falha retorna {}."""
    if not itens or not historico:
        return {}
    try:
        prompt = _PROMPT_TEMPLATE.format(
            itens=json.dumps(
                [{"codigo": i.get("codigo"), "descricao": i.get("descricao")} for i in itens],
                ensure_ascii=False, indent=1,
            ),
            historico=json.dumps(historico, ensure_ascii=False, indent=1),
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
        with httpx.Client(timeout=90.0) as client:
            r = client.post(url, params={"key": api_key}, json=body)
        if r.status_code != 200:
            logger.warning("Gemini matching HTTP %s: %s", r.status_code, r.text[:300])
            return {}
        data = r.json()
        cands = data.get("candidates") or []
        if not cands:
            return {}
        raw = "".join(p.get("text", "") for p in ((cands[0].get("content") or {}).get("parts") or [])).strip()
        d = None
        try:
            d = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    d = json.loads(m.group(0))
                except Exception:
                    d = None
        if not isinstance(d, dict):
            return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini matching falhou: %s", exc)
        return {}

    cods_validos = {str(h["cod_produto"]).strip() for h in historico}
    out: dict[str, dict] = {}
    for m in d.get("mapeamentos") or []:
        if not isinstance(m, dict):
            continue
        codigo = str(m.get("codigo") or "").strip()
        cod_produto = m.get("cod_produto")
        confianca = str(m.get("confianca") or "").strip().lower()
        if not codigo or cod_produto is None or confianca not in ("alta", "media"):
            continue
        cod_produto = str(cod_produto).strip()
        # defesa: só aceita produto que realmente está na lista de candidatos
        if cod_produto not in cods_validos:
            continue
        out[codigo] = {"cod_produto": cod_produto, "confianca": confianca}
    return out
