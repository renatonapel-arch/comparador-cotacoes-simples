"""Sync SATLBASE -> Comparador de Cotações (simples), produção.

Roda no PC do Renato (só ele tem rede até o SATLBASE). Lê tudo que o app em
produção precisa pra funcionar sem tocar o SATLBASE ao vivo, e manda pro
endpoint /admin/sync-historico do app deployado (que grava no Postgres
compartilhado do Clavis, schema comparador_simples).

Uso: pythonw.exe sync_comparador_simples.py   (Task Scheduler, a cada 6h)
Log: scripts-clavis\\comparador-cotacoes-simples\\logs\\sync.log
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

import pyodbc
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, "sync.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("sync_comparador_simples")

ENV_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".env")


def envvar(name: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=\s*(.*)$")
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line)
            if m:
                return m.group(1).strip()
    return ""


def _connect_satlbase():
    pwd = envvar("AZURE_SQL_PASSWORD")
    cs = (
        "DRIVER={SQL Server};SERVER=SRV-BD;DATABASE=SATLBASE;"
        f"UID=clavis;PWD={pwd};TrustServerCertificate=yes;Connection Timeout=30;"
    )
    return pyodbc.connect(cs, timeout=30)


def coletar_compras_historico(cur, meses: int = 24) -> list[dict]:
    """Última compra por (produto, fornecedor) nos últimos N meses.
    Prioriza Cod_docto='EC' em empate de data (canônico), mas aceita
    QUALQUER tipo de entrada quando não há 'EC' — produto pode ter sido
    registrado só como AJE/PCR/NFX/AVC (ver CONTINUAR-AQUI.md, caso 108680)."""
    cur.execute(
        f"""
        ;WITH ultimas AS (
            SELECT i.Cod_produto, e.Cod_cli_for, e.Data_movto, e.Num_docto, i.Valor_unitario,
                   ROW_NUMBER() OVER (
                       PARTITION BY i.Cod_produto, e.Cod_cli_for
                       ORDER BY e.Data_movto DESC, CASE WHEN e.Cod_docto = 'EC' THEN 0 ELSE 1 END
                   ) AS rn
            FROM tbentradasitem i WITH (NOLOCK)
            INNER JOIN tbentradas e WITH (NOLOCK) ON e.Chave_fato = i.Chave_fato
            WHERE e.Data_movto >= DATEADD(month, -{meses}, GETDATE())
              AND i.Cod_produto IS NOT NULL AND LTRIM(RTRIM(i.Cod_produto)) <> ''
              AND i.Valor_unitario IS NOT NULL AND i.Valor_unitario > 0
        )
        SELECT LTRIM(RTRIM(Cod_produto)), Cod_cli_for, Data_movto, Num_docto, Valor_unitario
        FROM ultimas WHERE rn = 1
        """
    )
    out = []
    for cod_produto, cod_cli_for, data_movto, num_docto, valor_unitario in cur.fetchall():
        out.append({
            "cod_produto": cod_produto,
            "cod_cadastro": int(cod_cli_for),
            "data_movto": data_movto.strftime("%Y-%m-%d"),
            "num_docto": int(num_docto) if num_docto is not None else None,
            "valor_unitario": float(valor_unitario),
        })
    return out


def coletar_produto_fornecedor(cur) -> list[dict]:
    cur.execute(
        "SELECT LTRIM(RTRIM(Cod_produto)), Cod_cadastro, LTRIM(RTRIM(Cod_produto_forn)) "
        "FROM tbProdutoFornecedor WITH (NOLOCK) WHERE Cod_produto_forn IS NOT NULL"
    )
    return [
        {"cod_produto": p, "cod_cadastro": int(c), "cod_produto_forn": f}
        for p, c, f in cur.fetchall() if f
    ]


def coletar_produtos(cur) -> list[dict]:
    cur.execute("SELECT LTRIM(RTRIM(Cod_produto)), Desc_produto_est FROM tbproduto WITH (NOLOCK)")
    return [{"cod_produto": p, "descricao": (d or "").strip()} for p, d in cur.fetchall()]


def coletar_fornecedores(cur, cods_cadastro: set[int]) -> list[dict]:
    if not cods_cadastro:
        return []
    placeholders = ",".join("?" * len(cods_cadastro))
    cur.execute(
        f"SELECT Cod_cadastro, Nome_cadastro FROM tbCadastroGeral WITH (NOLOCK) "
        f"WHERE Cod_cadastro IN ({placeholders})",
        list(cods_cadastro),
    )
    return [{"cod_cadastro": int(c), "nome": (n or "").strip()} for c, n in cur.fetchall()]


def main():
    started = datetime.now()
    logger.info("sync iniciado")
    try:
        conn = _connect_satlbase()
        cur = conn.cursor()

        compras_historico = coletar_compras_historico(cur)
        produto_fornecedor = coletar_produto_fornecedor(cur)
        produtos = coletar_produtos(cur)
        cods_cadastro = {c["cod_cadastro"] for c in compras_historico} | {c["cod_cadastro"] for c in produto_fornecedor}
        fornecedores = coletar_fornecedores(cur, cods_cadastro)

        logger.info(
            "coletado: %d compras_historico, %d produto_fornecedor, %d produtos, %d fornecedores",
            len(compras_historico), len(produto_fornecedor), len(produtos), len(fornecedores),
        )

        payload = {
            "source": "sync_comparador_simples.py @ RENATO-PC",
            "fornecedores": fornecedores,
            "produtos": produtos,
            "produto_fornecedor": produto_fornecedor,
            "compras_historico": compras_historico,
        }

        url = envvar("COMPARADOR_SIMPLES_URL").rstrip("/") + "/admin/sync-historico"
        token = envvar("COMPARADOR_SIMPLES_ADMIN_TOKEN")
        resp = requests.post(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
            timeout=120,
        )
        resp.raise_for_status()
        logger.info("sync ok: %s", resp.json())
        print("sync ok:", resp.json())
    except Exception:
        logger.exception("sync falhou")
        raise
    finally:
        logger.info("sync terminado em %.1fs", (datetime.now() - started).total_seconds())


if __name__ == "__main__":
    main()
