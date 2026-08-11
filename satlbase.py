"""Acesso SATLBASE (somente leitura) para o Comparador de Cotações — versão simples.

pyodbc testado e funcional no PC do Renato para esta sessão (driver 'SQL Server').
Mantido em módulo isolado para trocar por PowerShell/.NET SqlClient se um dia
o pyodbc voltar a ficar instável (ver CONTINUAR-AQUI.md seção 5).
"""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata

import pyodbc
from rapidfuzz import fuzz

_ENV_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".env")
_VINCULOS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vinculos_aprendidos.db")


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


def _variantes_codigo(codigos_forn: list[str]) -> tuple[set[str], dict[str, list[str]]]:
    """A IA às vezes gruda texto de coluna adjacente no código extraído (ex:
    documento tem '302010070' e uma coluna 'MA' logo depois — a IA devolve
    '302010070 MA'). Isso quebra o match exato mesmo quando o SIGE já tem o
    vínculo certo, e joga pro fuzzy — que erra quando existem produtos
    parecidos na mesma família (provado com 'papel foto': 5 variantes de
    grama/acabamento, matou o match certo). Fix determinístico (não depende
    de prompt de IA acertar sempre): tenta o código como veio E, se tiver
    espaço, também tenta só o 1º token — sem substituir (códigos legítimos
    às vezes TÊM espaço, ex: '991 BLACK NOIR')."""
    variantes: set[str] = set()
    por_original: dict[str, list[str]] = {}
    for c in codigos_forn:
        tentativas = [c]
        if " " in c:
            tentativas.append(c.split()[0])
        por_original[c] = tentativas
        variantes.update(tentativas)
    return variantes, por_original


def find_fornecedor_por_codigos(codigos_forn: list[str]) -> dict | None:
    """Identifica o fornecedor pelos códigos de item cotados (mais confiável que
    nome fuzzy — o nome extraído pode ser a MARCA, não a razão social cadastrada
    no SIGE, ex: marca "Supra" = razão social "ALISUL ALIMENTOS SA"). Conta, entre
    todos os cadastros que têm algum desses códigos em tbProdutoFornecedor, qual
    Cod_cadastro cobre mais itens da cotação."""
    codigos_forn = [c for c in codigos_forn if c]
    if not codigos_forn:
        return None
    variantes, _ = _variantes_codigo(codigos_forn)
    with _connect() as conn:
        cur = conn.cursor()
        placeholders = ",".join("?" * len(variantes))
        cur.execute(
            f"""
            SELECT pf.Cod_cadastro, cg.Nome_cadastro, COUNT(DISTINCT pf.Cod_produto_forn) AS acertos
            FROM tbProdutoFornecedor pf WITH (NOLOCK)
            LEFT JOIN tbCadastroGeral cg WITH (NOLOCK) ON cg.Cod_cadastro = pf.Cod_cadastro
            WHERE pf.Cod_produto_forn IN ({placeholders})
            GROUP BY pf.Cod_cadastro, cg.Nome_cadastro
            ORDER BY acertos DESC
            """,
            list(variantes),
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
    """Casa Cod_produto_forn -> Cod_produto via tbProdutoFornecedor (match exato,
    com fallback de variante — ver _variantes_codigo)."""
    if not codigos_forn:
        return {}
    variantes, por_original = _variantes_codigo(codigos_forn)
    with _connect() as conn:
        cur = conn.cursor()
        placeholders = ",".join("?" * len(variantes))
        cur.execute(
            f"SELECT Cod_produto, Cod_produto_forn FROM tbProdutoFornecedor WITH (NOLOCK) "
            f"WHERE Cod_cadastro = ? AND Cod_produto_forn IN ({placeholders})",
            cod_cadastro,
            *variantes,
        )
        achados = {(r[1] or "").strip(): str(r[0]).strip() for r in cur.fetchall()}

    out = {}
    for original, tentativas in por_original.items():
        for tentativa in tentativas:
            if tentativa in achados:
                out[original] = achados[tentativa]
                break
    return out


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


def catalogo_produtos() -> list[tuple[str, str]]:
    """Catálogo completo (cod, descricao) — ~9k linhas, usado pra votação de
    fornecedor e fuzzy local."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT Cod_produto, Desc_produto_est FROM tbproduto WITH (NOLOCK)")
        return [(str(r[0]).strip(), (r[1] or "").strip()) for r in cur.fetchall()]


def nome_cadastro(cod_cadastro: int) -> str:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT Nome_cadastro FROM tbCadastroGeral WITH (NOLOCK) WHERE Cod_cadastro = ?", cod_cadastro)
        r = cur.fetchone()
        return (str(r[0]).strip() if r and r[0] else "")


def produtos_historico_fornecedor(cod_cadastro: int, limite: int = 200) -> list[dict]:
    """Produtos que a Napel JÁ COMPROU desse fornecedor (qualquer tipo de doc de
    entrada — mesma razão do caso 108680/AJE). São os candidatos do matching por
    IA: lista pequena e de alta precisão, no vocabulário do catálogo."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP (?) i.Cod_produto, MAX(p.Desc_produto_est) AS descricao
            FROM tbentradasitem i WITH (NOLOCK)
            INNER JOIN tbentradas e WITH (NOLOCK) ON e.Chave_fato = i.Chave_fato
            LEFT JOIN tbproduto p WITH (NOLOCK) ON p.Cod_produto = i.Cod_produto
            WHERE e.Cod_cli_for = ?
            GROUP BY i.Cod_produto
            """,
            limite,
            cod_cadastro,
        )
        return [
            {"cod_produto": str(r[0]).strip(), "descricao": (r[1] or "").strip()}
            for r in cur.fetchall()
            if (r[1] or "").strip()
        ]


def fornecedores_de_produtos(cods_produto: list[str]) -> dict[str, set[int]]:
    """cod_produto -> fornecedores que VENDERAM esse produto pra Napel — só
    Cod_docto='EC' (compra canônica). Sem esse filtro, ajustes internos (AJE
    lançados por funcionário, DBADMIN etc.) entram como 'fornecedor' e poluem
    a votação (visto em teste real: DBADMIN/Hudson venciam a 3F)."""
    if not cods_produto:
        return {}
    with _connect() as conn:
        cur = conn.cursor()
        placeholders = ",".join("?" * len(cods_produto))
        cur.execute(
            f"""
            SELECT DISTINCT i.Cod_produto, e.Cod_cli_for
            FROM tbentradasitem i WITH (NOLOCK)
            INNER JOIN tbentradas e WITH (NOLOCK) ON e.Chave_fato = i.Chave_fato
            WHERE e.Cod_docto = 'EC' AND i.Cod_produto IN ({placeholders})
            """,
            *cods_produto,
        )
        out: dict[str, set[int]] = {}
        for cod_produto, cod_cli_for in cur.fetchall():
            out.setdefault(str(cod_produto).strip(), set()).add(int(cod_cli_for))
        return out


# ---------------------------------------------------------------------------
# Vínculos aprendidos (local: SQLite no diretório do projeto)
# Matches confirmados por evidência forte (IA sobre histórico do fornecedor,
# confiança alta) viram vínculo persistente — a próxima cotação do mesmo
# fornecedor resolve por código, instantâneo e sem IA. Fuzzy NUNCA é gravado
# (não perpetua erro).
# ---------------------------------------------------------------------------

def _vinculos_conn():
    conn = sqlite3.connect(_VINCULOS_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vinculos_aprendidos (
            cod_cadastro INTEGER NOT NULL,
            cod_produto_forn TEXT NOT NULL,
            cod_produto TEXT NOT NULL,
            fonte TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (cod_cadastro, cod_produto_forn)
        )
        """
    )
    return conn


def vinculos_aprendidos_get(cod_cadastro: int, codigos_forn: list[str]) -> dict[str, str]:
    if not codigos_forn:
        return {}
    variantes, por_original = _variantes_codigo(codigos_forn)
    with _vinculos_conn() as conn:
        placeholders = ",".join("?" * len(variantes))
        rows = conn.execute(
            f"SELECT cod_produto_forn, cod_produto FROM vinculos_aprendidos "
            f"WHERE cod_cadastro = ? AND cod_produto_forn IN ({placeholders})",
            [cod_cadastro, *variantes],
        ).fetchall()
    achados = {r[0]: r[1] for r in rows}
    out = {}
    for original, tentativas in por_original.items():
        for tentativa in tentativas:
            if tentativa in achados:
                out[original] = achados[tentativa]
                break
    return out


def vinculos_aprendidos_put(cod_cadastro: int, mapeamentos: list[tuple[str, str, str]]) -> None:
    """mapeamentos: [(cod_produto_forn, cod_produto, fonte)]"""
    if not mapeamentos:
        return
    with _vinculos_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO vinculos_aprendidos "
            "(cod_cadastro, cod_produto_forn, cod_produto, fonte, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [(cod_cadastro, c, p, f) for c, p, f in mapeamentos],
        )


def fornecedor_por_vinculos_aprendidos(codigos_forn: list[str]) -> dict | None:
    """Mesma votação do find_fornecedor_por_codigos, mas sobre a tabela de
    vínculos aprendidos — cobre fornecedor cujos códigos não existem em
    tbProdutoFornecedor mas já foram aprendidos em cotação anterior."""
    codigos_forn = [c for c in codigos_forn if c]
    if not codigos_forn:
        return None
    variantes, _ = _variantes_codigo(codigos_forn)
    with _vinculos_conn() as conn:
        placeholders = ",".join("?" * len(variantes))
        rows = conn.execute(
            f"SELECT cod_cadastro, COUNT(DISTINCT cod_produto_forn) FROM vinculos_aprendidos "
            f"WHERE cod_produto_forn IN ({placeholders}) GROUP BY cod_cadastro "
            f"ORDER BY 2 DESC",
            list(variantes),
        ).fetchall()
    if not rows:
        return None
    cod_cadastro, acertos = rows[0]
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT Nome_cadastro FROM tbCadastroGeral WITH (NOLOCK) WHERE Cod_cadastro = ?", cod_cadastro)
        r = cur.fetchone()
    return {
        "cod_cadastro": int(cod_cadastro),
        "nome_cadastro": (str(r[0]).strip() if r else ""),
        "acertos": int(acertos),
        "total_itens": len(codigos_forn),
    }
