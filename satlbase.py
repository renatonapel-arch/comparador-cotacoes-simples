"""Acesso SATLBASE (somente leitura) para o Comparador de Cotações — versão simples.

pyodbc testado e funcional no PC do Renato para esta sessão (driver 'SQL Server').
Mantido em módulo isolado para trocar por PowerShell/.NET SqlClient se um dia
o pyodbc voltar a ficar instável (ver CONTINUAR-AQUI.md seção 5).
"""
from __future__ import annotations

import os
import re
import unicodedata

import pyodbc
from rapidfuzz import fuzz

_ENV_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".env")


def _sem_acento(s: str) -> str:
    """SATLBASE guarda Desc_produto_est sem acentuação (ex: 'AJUSTAVEL', não
    'AJUSTÁVEL'). A IA extrai o texto do documento COM acento — sem essa
    normalização, o LIKE do fuzzy match não acha nenhum candidato mesmo
    quando o produto existe (bug real, provado com CORDÃO AJUSTÁVEL/Marine
    Sports: LIKE '%AJUSTÁVEL%' = 0 resultados, LIKE '%AJUSTAVEL%' = 3)."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _read_env_var(name: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=\s*(.*)$")
    with open(_ENV_PATH, encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line)
            if m:
                return m.group(1).strip()
    return ""


def _connect():
    pwd = _read_env_var("AZURE_SQL_PASSWORD")
    cs = (
        "DRIVER={SQL Server};SERVER=SRV-BD;DATABASE=SATLBASE;"
        f"UID=clavis;PWD={pwd};TrustServerCertificate=yes;Connection Timeout=15;"
    )
    return pyodbc.connect(cs, timeout=15)


def find_fornecedor(nome: str) -> dict | None:
    """Acha Cod_cadastro pelo nome (fuzzy, sem CNPJ). Usa o maior trecho contíguo
    do nome extraído para evitar LIKE genérico demais (gotcha documentado).
    Nome_cadastro no SATLBASE é inconsistente quanto a acento (algumas linhas
    têm, outras não) — busca e comparação sem acento dos dois lados, mesmo
    fix de match_produto_fuzzy."""
    nome = (nome or "").strip().upper()
    if not nome:
        return None
    nome_sa = _sem_acento(nome)
    termo = max(nome_sa.split(), key=len) if nome_sa.split() else nome_sa
    if len(termo) < 4:
        termo = nome_sa
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT TOP 20 Cod_cadastro, Nome_cadastro FROM tbCadastroGeral WITH (NOLOCK) "
            "WHERE Nome_cadastro LIKE ?",
            f"%{termo}%",
        )
        candidatos = cur.fetchall()
    if not candidatos:
        return None
    melhor = max(
        candidatos,
        key=lambda r: fuzz.token_set_ratio(nome_sa, _sem_acento((r[1] or "").strip().upper())),
    )
    return {"cod_cadastro": melhor[0], "nome_cadastro": (melhor[1] or "").strip()}


def find_fornecedor_por_codigos(codigos_forn: list[str]) -> dict | None:
    """Identifica o fornecedor pelos códigos de item cotados (mais confiável que
    nome fuzzy — o nome extraído pode ser a MARCA, não a razão social cadastrada
    no SIGE, ex: marca "Supra" = razão social "ALISUL ALIMENTOS SA"). Conta, entre
    todos os cadastros que têm algum desses códigos em tbProdutoFornecedor, qual
    Cod_cadastro cobre mais itens da cotação."""
    codigos_forn = [c for c in codigos_forn if c]
    if not codigos_forn:
        return None
    with _connect() as conn:
        cur = conn.cursor()
        placeholders = ",".join("?" * len(codigos_forn))
        cur.execute(
            f"""
            SELECT pf.Cod_cadastro, cg.Nome_cadastro, COUNT(DISTINCT pf.Cod_produto_forn) AS acertos
            FROM tbProdutoFornecedor pf WITH (NOLOCK)
            LEFT JOIN tbCadastroGeral cg WITH (NOLOCK) ON cg.Cod_cadastro = pf.Cod_cadastro
            WHERE pf.Cod_produto_forn IN ({placeholders})
            GROUP BY pf.Cod_cadastro, cg.Nome_cadastro
            ORDER BY acertos DESC
            """,
            codigos_forn,
        )
        candidatos = cur.fetchall()
    if not candidatos:
        return None
    cod_cadastro, nome, acertos = candidatos[0]
    return {
        "cod_cadastro": cod_cadastro,
        "nome_cadastro": (nome or "").strip(),
        "acertos": acertos,
        "total_itens": len(codigos_forn),
    }


def match_produtos(cod_cadastro: int, codigos_forn: list[str]) -> dict[str, str]:
    """Casa Cod_produto_forn -> Cod_produto via tbProdutoFornecedor (match exato)."""
    if not codigos_forn:
        return {}
    with _connect() as conn:
        cur = conn.cursor()
        placeholders = ",".join("?" * len(codigos_forn))
        cur.execute(
            f"SELECT Cod_produto, Cod_produto_forn FROM tbProdutoFornecedor WITH (NOLOCK) "
            f"WHERE Cod_cadastro = ? AND Cod_produto_forn IN ({placeholders})",
            cod_cadastro,
            *codigos_forn,
        )
        return {
            (r[1] or "").strip(): str(r[0]).strip()
            for r in cur.fetchall()
        }


def match_produto_fuzzy(descricao: str, threshold: float = 0.55) -> dict | None:
    """Fallback: casa por similaridade de descrição em tbproduto (sem filtro de fornecedor).
    Termo de busca e comparação de score SEM acento — ver _sem_acento().

    SEM pré-filtro LIKE de 1 palavra: o SQL Server não tem trigram nativo simples
    (full-text index em tbproduto seria mexer em tabela de terceiro/SIGE), e
    escolher "a maior palavra" como termo é frágil — falha quando essa palavra é
    prefixo de marca/fabricante que a IA extrai do PDF do fornecedor e não existe
    no cadastro do SIGE (bug real 2026-07-20: "MP-PAPEL FOTO GLOSSY..." vs
    "EC PAPEL FOTO GLOSSY..." — produto e histórico existiam, LIKE '%MP-PAPEL%'
    dava 0 candidatos). Roda local (só PC do Renato, sem concorrência) contra
    ~9k produtos — full-scan em Python é rápido o bastante nesse volume."""
    descricao = (descricao or "").strip().upper()
    if not descricao:
        return None
    descricao_sa = _sem_acento(descricao)
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT Cod_produto, Desc_produto_est FROM tbproduto WITH (NOLOCK)")
        candidatos = cur.fetchall()
    if not candidatos:
        return None
    scored = [
        (r, fuzz.token_set_ratio(descricao_sa, _sem_acento((r[1] or "").strip().upper())) / 100.0)
        for r in candidatos
    ]
    melhor, score = max(scored, key=lambda x: x[1])
    if score < threshold:
        return None
    return {
        "cod_produto": str(melhor[0]).strip(),
        "descricao": (melhor[1] or "").strip(),
        "score": round(score, 2),
    }


def _buscar_ultimas(cur, cods_produto, cod_cadastro=None, cod_filial=None):
    """Roda a query de última compra com os filtros dados (fornecedor e/ou
    filial opcionais). Retorna dict cod_produto -> linha bruta."""
    placeholders = ",".join("?" * len(cods_produto))
    filtros = ["i.Cod_produto IN (" + placeholders + ")"]
    params = list(cods_produto)
    if cod_cadastro is not None:
        filtros.append("e.Cod_cli_for = ?")
        params.append(cod_cadastro)
    if cod_filial is not None:
        filtros.append("LTRIM(RTRIM(e.Cod_filial)) = ?")
        params.append(cod_filial)
    where = " AND ".join(filtros)
    cur.execute(
        f"""
        ;WITH ultimas AS (
            SELECT i.Cod_produto, e.Data_movto, e.Num_docto, i.Valor_unitario,
                   ROW_NUMBER() OVER (
                       PARTITION BY i.Cod_produto
                       ORDER BY e.Data_movto DESC, CASE WHEN e.Cod_docto = 'EC' THEN 0 ELSE 1 END
                   ) AS rn
            FROM tbentradasitem i WITH (NOLOCK)
            INNER JOIN tbentradas e WITH (NOLOCK) ON e.Chave_fato = i.Chave_fato
            WHERE {where}
        )
        SELECT Cod_produto, Data_movto, Num_docto, Valor_unitario
        FROM ultimas WHERE rn <= 1
        """,
        params,
    )
    return {
        str(cod_produto).strip(): {
            "data_movto": data_movto.strftime("%d/%m/%Y"),
            "num_docto": int(num_docto),
            "valor_unitario": float(valor_unitario),
        }
        for cod_produto, data_movto, num_docto, valor_unitario in cur.fetchall()
    }


def ultima_compra(cod_cadastro: int, cods_produto: list[str], cod_filial: str | None = None) -> dict[str, dict]:
    """Última compra por produto, em 3 níveis de confiança (qualquer Cod_docto
    de entrada — não só 'EC': achamos caso real de produto cuja única entrada
    estava registrada como 'AJE', e ficava de fora):
    1. mesmo fornecedor + mesma filial (ideal — preço que ESSA filial pagou)
    2. mesmo fornecedor, qualquer filial (fallback — filial nunca comprou dele)
    3. qualquer fornecedor, qualquer filial (fallback — produto nunca comprado
       desse fornecedor)
    Cada resultado marca mesmo_fornecedor/mesma_filial pro frontend avisar
    quando a comparação não é 1-pra-1."""
    if not cods_produto:
        return {}
    with _connect() as conn:
        cur = conn.cursor()
        out: dict[str, dict] = {}

        if cod_filial:
            nivel1 = _buscar_ultimas(cur, cods_produto, cod_cadastro=cod_cadastro, cod_filial=cod_filial)
            for cod_produto, linha in nivel1.items():
                out[cod_produto] = {**linha, "mesmo_fornecedor": True, "mesma_filial": True}

        faltantes = [c for c in cods_produto if c not in out]
        if faltantes:
            nivel2 = _buscar_ultimas(cur, faltantes, cod_cadastro=cod_cadastro)
            for cod_produto, linha in nivel2.items():
                out[cod_produto] = {**linha, "mesmo_fornecedor": True, "mesma_filial": not cod_filial}

        faltantes = [c for c in cods_produto if c not in out]
        if faltantes:
            nivel3 = _buscar_ultimas(cur, faltantes)
            for cod_produto, linha in nivel3.items():
                out[cod_produto] = {**linha, "mesmo_fornecedor": False, "mesma_filial": False}

        return out


def descricao_produto(cods_produto: list[str]) -> dict[str, str]:
    if not cods_produto:
        return {}
    with _connect() as conn:
        cur = conn.cursor()
        placeholders = ",".join("?" * len(cods_produto))
        cur.execute(
            f"SELECT Cod_produto, Desc_produto_est FROM tbproduto WITH (NOLOCK) "
            f"WHERE Cod_produto IN ({placeholders})",
            *cods_produto,
        )
        return {str(r[0]).strip(): (r[1] or "").strip() for r in cur.fetchall()}
