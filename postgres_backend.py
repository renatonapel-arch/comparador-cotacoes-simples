"""Acesso ao Postgres compartilhado do Clavis (schema `comparador_simples`) —
usado em PRODUÇÃO (a VPS não alcança o SATLBASE ao vivo). Mesma interface de
satlbase.py (find_fornecedor_por_codigos, find_fornecedor, match_produtos,
match_produto_fuzzy, ultima_compra, descricao_produto) pra app.py trocar de
backend sem mudar a orquestração.

Os dados são alimentados pelo sync_comparador_simples.py, que roda no PC do
Renato e faz POST em /admin/sync-historico (ver CONTINUAR-AQUI.md).
"""
from __future__ import annotations

import os
import unicodedata

import psycopg2
import psycopg2.extras
from rapidfuzz import fuzz

_schema_ready = False


def _sem_acento(s: str) -> str:
    """SATLBASE guarda descrição/nome sem acentuação de forma inconsistente
    (algumas linhas têm, outras não) — a IA extrai texto do documento COM
    acento. Sem normalizar, o ILIKE/comparação de score falha mesmo quando o
    produto/fornecedor existe (bug real, provado com CORDÃO AJUSTÁVEL/Marine
    Sports)."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _dsn() -> str:
    dsn = os.environ.get("COMPARADOR_DATABASE_URL")
    if not dsn:
        raise RuntimeError("COMPARADOR_DATABASE_URL não configurada (env do Coolify)")
    return dsn


def _connect():
    return psycopg2.connect(_dsn(), connect_timeout=10)


def _ensure_schema() -> None:
    """Cria schema/tabelas se ainda não existirem (idempotente, lazy — não
    conecta no Postgres no startup do processo, só na primeira chamada real,
    pra não derrubar o container se o Postgres estiver momentaneamente
    saturado)."""
    global _schema_ready
    if _schema_ready:
        return
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE SCHEMA IF NOT EXISTS comparador_simples;

            CREATE TABLE IF NOT EXISTS comparador_simples.fornecedores (
                cod_cadastro INTEGER PRIMARY KEY,
                nome TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS comparador_simples.produtos (
                cod_produto TEXT PRIMARY KEY,
                descricao TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS comparador_simples.produto_fornecedor (
                cod_cadastro INTEGER NOT NULL,
                cod_produto_forn TEXT NOT NULL,
                cod_produto TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (cod_cadastro, cod_produto_forn)
            );
            CREATE INDEX IF NOT EXISTS idx_produto_fornecedor_produto
                ON comparador_simples.produto_fornecedor (cod_produto);

            CREATE TABLE IF NOT EXISTS comparador_simples.compras_historico (
                cod_produto TEXT NOT NULL,
                cod_cadastro INTEGER NOT NULL,
                cod_filial TEXT NOT NULL,
                data_movto DATE NOT NULL,
                num_docto INTEGER,
                valor_unitario NUMERIC(14,4) NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (cod_produto, cod_cadastro, cod_filial)
            );
            CREATE INDEX IF NOT EXISTS idx_compras_historico_produto
                ON comparador_simples.compras_historico (cod_produto, data_movto DESC);

            CREATE TABLE IF NOT EXISTS comparador_simples.sync_log (
                id SERIAL PRIMARY KEY,
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                source TEXT,
                rows_received INTEGER,
                rows_upserted INTEGER,
                status TEXT,
                error TEXT
            );
            """
        )
        conn.commit()
    _schema_ready = True


def find_fornecedor_por_codigos(codigos_forn: list[str]) -> dict | None:
    codigos_forn = [c for c in codigos_forn if c]
    if not codigos_forn:
        return None
    _ensure_schema()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pf.cod_cadastro, f.nome, COUNT(DISTINCT pf.cod_produto_forn) AS acertos
            FROM comparador_simples.produto_fornecedor pf
            LEFT JOIN comparador_simples.fornecedores f ON f.cod_cadastro = pf.cod_cadastro
            WHERE pf.cod_produto_forn = ANY(%s)
            GROUP BY pf.cod_cadastro, f.nome
            ORDER BY acertos DESC
            """,
            (codigos_forn,),
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


def find_fornecedor(nome: str) -> dict | None:
    nome = (nome or "").strip().upper()
    if not nome:
        return None
    nome_sa = _sem_acento(nome)
    termo = max(nome_sa.split(), key=len) if nome_sa.split() else nome_sa
    if len(termo) < 4:
        termo = nome_sa
    _ensure_schema()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cod_cadastro, nome FROM comparador_simples.fornecedores WHERE nome ILIKE %s LIMIT 20",
            (f"%{termo}%",),
        )
        candidatos = cur.fetchall()
    if not candidatos:
        return None
    melhor = max(candidatos, key=lambda r: fuzz.token_set_ratio(nome_sa, _sem_acento((r[1] or "").strip().upper())))
    return {"cod_cadastro": melhor[0], "nome_cadastro": (melhor[1] or "").strip()}


def match_produtos(cod_cadastro: int, codigos_forn: list[str]) -> dict[str, str]:
    if not codigos_forn:
        return {}
    _ensure_schema()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT cod_produto, cod_produto_forn FROM comparador_simples.produto_fornecedor
            WHERE cod_cadastro = %s AND cod_produto_forn = ANY(%s)
            """,
            (cod_cadastro, codigos_forn),
        )
        return {(forn or "").strip(): (prod or "").strip() for prod, forn in cur.fetchall()}


def match_produto_fuzzy(descricao: str, threshold: float = 0.55) -> dict | None:
    descricao = (descricao or "").strip().upper()
    if not descricao:
        return None
    descricao_sa = _sem_acento(descricao)
    termo = max(descricao_sa.split(), key=len) if descricao_sa.split() else descricao_sa
    _ensure_schema()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cod_produto, descricao FROM comparador_simples.produtos WHERE descricao ILIKE %s LIMIT 50",
            (f"%{termo}%",),
        )
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
    return {"cod_produto": (melhor[0] or "").strip(), "descricao": (melhor[1] or "").strip(), "score": round(score, 2)}


def ultima_compra(cod_cadastro: int, cods_produto: list[str], cod_filial: str | None = None) -> dict[str, dict]:
    """Última compra por produto, em 3 níveis (mesma lógica de satlbase.py):
    1. mesmo fornecedor + mesma filial (ideal)
    2. mesmo fornecedor, qualquer filial (fallback)
    3. qualquer fornecedor, qualquer filial (fallback)"""
    if not cods_produto:
        return {}
    _ensure_schema()
    with _connect() as conn, conn.cursor() as cur:
        out: dict[str, dict] = {}

        if cod_filial:
            cur.execute(
                """
                SELECT cod_produto, data_movto, num_docto, valor_unitario
                FROM comparador_simples.compras_historico
                WHERE cod_cadastro = %s AND cod_filial = %s AND cod_produto = ANY(%s)
                """,
                (cod_cadastro, cod_filial, cods_produto),
            )
            for cod_produto, data_movto, num_docto, valor_unitario in cur.fetchall():
                out[cod_produto] = {
                    "data_movto": data_movto.strftime("%d/%m/%Y"),
                    "num_docto": num_docto,
                    "valor_unitario": float(valor_unitario),
                    "mesmo_fornecedor": True,
                    "mesma_filial": True,
                }

        faltantes = [c for c in cods_produto if c not in out]
        if faltantes:
            cur.execute(
                """
                SELECT DISTINCT ON (cod_produto) cod_produto, data_movto, num_docto, valor_unitario
                FROM comparador_simples.compras_historico
                WHERE cod_cadastro = %s AND cod_produto = ANY(%s)
                ORDER BY cod_produto, data_movto DESC
                """,
                (cod_cadastro, faltantes),
            )
            for cod_produto, data_movto, num_docto, valor_unitario in cur.fetchall():
                out[cod_produto] = {
                    "data_movto": data_movto.strftime("%d/%m/%Y"),
                    "num_docto": num_docto,
                    "valor_unitario": float(valor_unitario),
                    "mesmo_fornecedor": True,
                    "mesma_filial": not cod_filial,
                }

        faltantes = [c for c in cods_produto if c not in out]
        if faltantes:
            cur.execute(
                """
                SELECT DISTINCT ON (cod_produto) cod_produto, data_movto, num_docto, valor_unitario
                FROM comparador_simples.compras_historico
                WHERE cod_produto = ANY(%s)
                ORDER BY cod_produto, data_movto DESC
                """,
                (faltantes,),
            )
            for cod_produto, data_movto, num_docto, valor_unitario in cur.fetchall():
                out[cod_produto] = {
                    "data_movto": data_movto.strftime("%d/%m/%Y"),
                    "num_docto": num_docto,
                    "valor_unitario": float(valor_unitario),
                    "mesmo_fornecedor": False,
                    "mesma_filial": False,
                }
        return out


def descricao_produto(cods_produto: list[str]) -> dict[str, str]:
    if not cods_produto:
        return {}
    _ensure_schema()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cod_produto, descricao FROM comparador_simples.produtos WHERE cod_produto = ANY(%s)",
            (cods_produto,),
        )
        return {r[0]: (r[1] or "").strip() for r in cur.fetchall()}


def sync_upsert(payload: dict) -> dict:
    """Recebe o payload do sync_comparador_simples.py e faz upsert em massa.
    payload = {source, fornecedores, produtos, produto_fornecedor, compras_historico}
    """
    _ensure_schema()
    rows_upserted = 0
    with _connect() as conn, conn.cursor() as cur:
        fornecedores = payload.get("fornecedores") or []
        if fornecedores:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO comparador_simples.fornecedores (cod_cadastro, nome, updated_at)
                VALUES %s
                ON CONFLICT (cod_cadastro) DO UPDATE SET nome = EXCLUDED.nome, updated_at = now()
                """,
                [(f["cod_cadastro"], f["nome"]) for f in fornecedores],
                template="(%s, %s, now())",
            )
            rows_upserted += len(fornecedores)

        produtos = payload.get("produtos") or []
        if produtos:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO comparador_simples.produtos (cod_produto, descricao, updated_at)
                VALUES %s
                ON CONFLICT (cod_produto) DO UPDATE SET descricao = EXCLUDED.descricao, updated_at = now()
                """,
                [(p["cod_produto"], p["descricao"]) for p in produtos],
                template="(%s, %s, now())",
            )
            rows_upserted += len(produtos)

        produto_fornecedor = payload.get("produto_fornecedor") or []
        if produto_fornecedor:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO comparador_simples.produto_fornecedor (cod_cadastro, cod_produto_forn, cod_produto, updated_at)
                VALUES %s
                ON CONFLICT (cod_cadastro, cod_produto_forn) DO UPDATE SET
                    cod_produto = EXCLUDED.cod_produto, updated_at = now()
                """,
                [(pf["cod_cadastro"], pf["cod_produto_forn"], pf["cod_produto"]) for pf in produto_fornecedor],
                template="(%s, %s, %s, now())",
            )
            rows_upserted += len(produto_fornecedor)

        compras_historico = payload.get("compras_historico") or []
        if compras_historico:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO comparador_simples.compras_historico
                    (cod_produto, cod_cadastro, cod_filial, data_movto, num_docto, valor_unitario, updated_at)
                VALUES %s
                ON CONFLICT (cod_produto, cod_cadastro, cod_filial) DO UPDATE SET
                    data_movto = EXCLUDED.data_movto,
                    num_docto = EXCLUDED.num_docto,
                    valor_unitario = EXCLUDED.valor_unitario,
                    updated_at = now()
                WHERE EXCLUDED.data_movto >= comparador_simples.compras_historico.data_movto
                """,
                [
                    (c["cod_produto"], c["cod_cadastro"], c["cod_filial"], c["data_movto"], c["num_docto"], c["valor_unitario"])
                    for c in compras_historico
                ],
                template="(%s, %s, %s, %s, %s, %s, now())",
            )
            rows_upserted += len(compras_historico)

        cur.execute(
            """
            INSERT INTO comparador_simples.sync_log
                (started_at, finished_at, source, rows_received, rows_upserted, status)
            VALUES (now(), now(), %s, %s, %s, 'ok')
            """,
            (
                payload.get("source", "desconhecido"),
                len(fornecedores) + len(produtos) + len(produto_fornecedor) + len(compras_historico),
                rows_upserted,
            ),
        )
        conn.commit()

    return {"ok": True, "rows_upserted": rows_upserted}
