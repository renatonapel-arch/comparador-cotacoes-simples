"""Extração genérica de itens de cotação via Gemini (texto, PDF ou imagem).

Padrão idêntico ao já validado em produção (backend/app/integrations/gemini.py):
REST puro via httpx (evita pin de SDK), gemini-2.5-flash, inline_data base64,
responseMimeType=json + thinkingBudget=0, schema só no prompt (Gemini é finicky
com response_schema aninhado), parse defensivo.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

MODEL = os.environ.get("COMPARADOR_MODEL", "gemini-2.5-flash")

_MIME_BY_EXT = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

PROMPT = """Você recebe a COTAÇÃO enviada por um fornecedor (pode ser PDF, foto,
print de e-mail/WhatsApp, ou texto colado). Extraia os dados e responda APENAS
um JSON válido (sem texto antes ou depois, sem ```), com EXATAMENTE estas chaves:

{
  "fornecedor_nome": "razão social ou nome do fornecedor — string ou null. Se o \
nome não aparecer no texto, procure em logos/marcas/imagens do documento.",
  "itens": [
    {
      "codigo": "código do produto no fornecedor, como aparece no documento — string",
      "descricao": "descrição do item — string",
      "qtde_cotada": número (quantidade cotada, na unidade de venda do fornecedor — ex: 6 caixas),
      "unidade_venda": "unidade em que o fornecedor vende — ex: CX, FD, UN, KG — string ou null",
      "valor_unitario_documento": número (valor unitário EXATAMENTE como está no documento, \
na unidade de venda do fornecedor — ex: preço por caixa, não por unidade individual),
      "unidades_por_embalagem": número (quantas unidades INDIVIDUAIS existem dentro de \
cada embalagem/caixa/fardo cotado — ex: "CX 12*0.45KG" = 12 unidades por caixa; \
"FD 5*5.0KG" = 5 unidades por fardo. Se o item já é vendido por unidade individual, \
sem embalagem múltipla, use 1.)
    }
  ]
}

REGRAS IMPORTANTES:
- "unidades_por_embalagem" é o número ANTES do "*" na descrição da embalagem \
(ex: "CX 12*0.45KG" → 12; "FD 4*2.5KG" → 4). Se não houver padrão de embalagem \
múltipla, use 1.
- Números com PONTO decimal, sem separador de milhar (ex: 66.5137, não 66,5137).
- Campo ausente = null. NÃO invente valores.
- Responda somente o JSON."""


def _mime_from_filename(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def _chamar_gemini(parts: list[dict], api_key: str) -> dict:
    body = {
        "contents": [{"parts": parts}],
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
        logger.warning("Gemini HTTP %s: %s", r.status_code, r.text[:300])
        return {"_erro": f"gemini_http_{r.status_code}"}
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        return {"_erro": "sem_candidato"}
    partes = ((cands[0].get("content") or {}).get("parts")) or []
    raw = "".join(p.get("text", "") for p in partes).strip()

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
        return {"_erro": "json_invalido", "_raw": raw[:600]}
    return d


def extrair_de_texto(texto: str, api_key: str) -> dict:
    parts = [{"text": PROMPT}, {"text": f"\n\nCOTAÇÃO (texto colado):\n{texto}"}]
    return _chamar_gemini(parts, api_key)


def extrair_de_arquivo(conteudo: bytes, filename: str, api_key: str) -> dict:
    mime = _mime_from_filename(filename)
    if mime == "application/octet-stream":
        return {"_erro": f"tipo_arquivo_nao_suportado:{filename}"}
    parts = [
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(conteudo).decode()}},
        {"text": PROMPT},
    ]
    return _chamar_gemini(parts, api_key)


def extrair_de_planilha(texto_tabular: str, api_key: str) -> dict:
    """Planilha (XLSX/CSV) já convertida em texto tabular (ver app.py)."""
    parts = [{"text": PROMPT}, {"text": f"\n\nCOTAÇÃO (planilha, convertida em texto):\n{texto_tabular}"}]
    return _chamar_gemini(parts, api_key)
