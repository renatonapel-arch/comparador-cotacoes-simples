"""Comparador de Cotações — versão simples.

Fluxo: recebe arquivo OU texto colado -> Gemini extrai itens -> acha fornecedor
+ última compra -> compara preço por unidade -> tabela.

Dois modos de backend de dados (ver CONTINUAR-AQUI.md):
- COMPARADOR_DB=satlbase (padrão, uso local no PC do Renato) -> consulta o
  SATLBASE ao vivo via satlbase.py.
- COMPARADOR_DB=postgres (produção/VPS, sem acesso à rede do SATLBASE) ->
  consulta o Postgres compartilhado do Clavis via postgres_backend.py,
  alimentado pelo sync_comparador_simples.py (roda no PC, a cada 6h).

Em produção (COMPARADOR_DB=postgres), embutido via iframe no Clavis (SSO):
GET / é público (só HTML/JS estático, sem dado sensível) — a página checa
window.top e redireciona pro Clavis se acessada direto, fora do iframe. A
rota que importa (/comparar) exige um JWT do Clavis (Authorization: Bearer,
mesma CLAVIS_SECRET_KEY/HS256) OU Basic Auth como fallback legado. Local
(satlbase) roda sem login, como sempre.
"""
from __future__ import annotations

import io
import os
import re
import secrets

import pandas as pd
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt as jose_jwt

import extracao

DB_BACKEND = os.environ.get("COMPARADOR_DB", "satlbase")
if DB_BACKEND == "postgres":
    import postgres_backend as db
else:
    import satlbase as db

APP_VERSION = "v2"

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")

app = FastAPI(title="Comparador de Cotações — simples")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

TOLERANCIA_PCT = 2  # mesma faixa do comparador em produção

_basic = HTTPBasic(auto_error=False)


def _load_basic_auth_users() -> dict[str, str]:
    users = {}
    user = os.environ.get("BASIC_AUTH_USER")
    pwd = os.environ.get("BASIC_AUTH_PASS")
    if user and pwd:
        users[user] = pwd
    for pair in os.environ.get("BASIC_AUTH_EXTRA", "").split(","):
        if ":" in pair:
            u, p = pair.split(":", 1)
            users[u.strip()] = p.strip()
    return users


def _verify_clavis_jwt(token: str) -> str | None:
    """Valida o JWT emitido pelo Clavis (mesma SECRET_KEY, HS256). Retorna o
    email do usuário se válido, senão None."""
    secret = os.environ.get("CLAVIS_SECRET_KEY")
    if not secret:
        return None
    try:
        payload = jose_jwt.decode(token, secret, algorithms=["HS256"])
        return payload.get("email")
    except JWTError:
        return None


def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
):
    """Ordem: (1) JWT do Clavis via Authorization: Bearer (SSO, iframe) —
    (2) Basic Auth, se configurado (fallback legado / uso direto) — (3) sem
    gate nenhum, se nada estiver configurado (dev local)."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        user = _verify_clavis_jwt(auth_header[7:])
        if user:
            return user
        raise HTTPException(status_code=401, detail="token Clavis inválido")

    users = _load_basic_auth_users()
    if not users:
        if os.environ.get("CLAVIS_SECRET_KEY"):
            raise HTTPException(status_code=401, detail="autenticação necessária")
        return None
    if credentials is None:
        raise HTTPException(status_code=401, detail="login necessário", headers={"WWW-Authenticate": "Basic"})
    esperado = users.get(credentials.username)
    if not esperado or not secrets.compare_digest(credentials.password, esperado):
        raise HTTPException(status_code=401, detail="usuário ou senha inválidos", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def _api_key() -> str:
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        return env_key
    pattern = re.compile(r"^\s*GEMINI_API_KEY\s*=\s*(.*)$")
    env_path = os.path.join(os.path.expanduser("~"), ".claude", ".env")
    if not os.path.exists(env_path):
        return ""
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line)
            if m:
                return m.group(1).strip()
    return ""


def _planilha_para_texto(conteudo: bytes, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "csv":
        df = pd.read_csv(io.BytesIO(conteudo))
    else:
        df = pd.read_excel(io.BytesIO(conteudo))
    return df.to_csv(index=False)


def _doc_referencia(ultima: dict) -> str:
    ref = f"doc {ultima['num_docto']} · {ultima['data_movto']}"
    avisos = []
    if not ultima.get("mesmo_fornecedor", True):
        avisos.append("outro fornecedor")
    elif not ultima.get("mesma_filial", True):
        avisos.append("outra filial")
    if avisos:
        ref += f" ({', '.join(avisos)})"
    return ref


def _verdict(pct: float) -> str:
    if pct < -TOLERANCIA_PCT:
        return "down"
    if pct > TOLERANCIA_PCT:
        return "up"
    return "flat"


@app.get("/health")
def health():
    """Raso, sem tocar banco — pro Coolify decidir se o container tá saudável."""
    return {"status": "ok", "version": APP_VERSION, "db_backend": DB_BACKEND}


@app.get("/version")
def version():
    return {"version": APP_VERSION, "db_backend": DB_BACKEND}


@app.get("/")
def index():
    # Público de propósito — só HTML/JS estático, sem dado sensível. A própria
    # página checa window.top e redireciona pro Clavis se acessada direto
    # (fora do iframe). O dado real fica atrás de /comparar (Depends(require_auth)).
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.post("/comparar")
async def comparar(
    modo: str = Form(...),
    texto: str = Form(""),
    filial: str = Form("100"),
    arquivo: UploadFile | None = None,
    _user: str | None = Depends(require_auth),
):
    api_key = _api_key()
    if not api_key:
        return JSONResponse({"erro": "GEMINI_API_KEY não configurada"}, status_code=500)

    if modo == "arquivo":
        if arquivo is None:
            return JSONResponse({"erro": "nenhum arquivo enviado"}, status_code=400)
        conteudo = await arquivo.read()
        ext = (arquivo.filename or "").rsplit(".", 1)[-1].lower()
        if ext in ("xlsx", "xls", "csv"):
            try:
                texto_tabular = _planilha_para_texto(conteudo, arquivo.filename)
            except Exception as exc:
                return JSONResponse({"erro": f"falha ao ler planilha: {exc}"}, status_code=400)
            extraido = extracao.extrair_de_planilha(texto_tabular, api_key)
        else:
            extraido = extracao.extrair_de_arquivo(conteudo, arquivo.filename or "arquivo", api_key)
    elif modo == "texto":
        if not texto.strip():
            return JSONResponse({"erro": "texto vazio"}, status_code=400)
        extraido = extracao.extrair_de_texto(texto, api_key)
    else:
        return JSONResponse({"erro": f"modo inválido: {modo}"}, status_code=400)

    if extraido.get("_erro"):
        return JSONResponse({"erro": f"extração falhou: {extraido['_erro']}"}, status_code=502)

    itens_extraidos = extraido.get("itens") or []
    if not itens_extraidos:
        return JSONResponse({"erro": "nenhum item identificado na cotação"}, status_code=422)

    fornecedor_nome_extraido = extraido.get("fornecedor_nome") or ""
    codigos_forn = [str(it.get("codigo") or "").strip() for it in itens_extraidos]

    # Identificação por código de item é mais confiável que por nome — o nome
    # extraído pode ser a marca do produto, não a razão social cadastrada no
    # SIGE (ex: marca "Supra" -> razão social "ALISUL ALIMENTOS SA").
    fornecedor = db.find_fornecedor_por_codigos(codigos_forn)
    if not fornecedor:
        fornecedor = db.find_fornecedor(fornecedor_nome_extraido)

    linhas = []
    cod_cadastro = fornecedor["cod_cadastro"] if fornecedor else None

    mapa_codigo_para_produto = (
        db.match_produtos(cod_cadastro, [c for c in codigos_forn if c]) if cod_cadastro else {}
    )

    cods_produto_ok = [v for v in mapa_codigo_para_produto.values()]
    ultimas = db.ultima_compra(cod_cadastro, cods_produto_ok, filial) if cod_cadastro and cods_produto_ok else {}
    descricoes_sige = db.descricao_produto(cods_produto_ok) if cods_produto_ok else {}

    for item in itens_extraidos:
        codigo = str(item.get("codigo") or "").strip()
        descricao_forn = item.get("descricao") or "(sem descrição)"
        valor_doc = item.get("valor_unitario_documento")
        unid_embalagem = item.get("unidades_por_embalagem") or 1

        linha = {
            "produto": descricao_forn,
            "codigo_fornecedor": codigo,
            "match_status": "sem_fornecedor",
            "atual": None,
            "ultima": None,
            "variacao_pct": None,
            "verdict": "neutro",
            "doc_referencia": None,
        }

        if valor_doc is None or not unid_embalagem:
            linhas.append(linha)
            continue

        try:
            atual = float(valor_doc) / float(unid_embalagem)
        except (TypeError, ZeroDivisionError, ValueError):
            linhas.append(linha)
            continue
        linha["atual"] = round(atual, 4)

        cod_produto = mapa_codigo_para_produto.get(codigo)
        if cod_produto is None and cod_cadastro:
            fuzzy = db.match_produto_fuzzy(descricao_forn)
            if fuzzy:
                cod_produto = fuzzy["cod_produto"]
                linha["match_status"] = "fuzzy"
                ultima = db.ultima_compra(cod_cadastro, [cod_produto], filial).get(cod_produto)
                descr = db.descricao_produto([cod_produto]).get(cod_produto)
                if descr:
                    linha["produto"] = descr
                if ultima:
                    linha["ultima"] = ultima["valor_unitario"]
                    linha["doc_referencia"] = _doc_referencia(ultima)
        elif cod_produto is not None:
            linha["match_status"] = "exato"
            if descricoes_sige.get(cod_produto):
                linha["produto"] = descricoes_sige[cod_produto]
            ultima = ultimas.get(cod_produto)
            if ultima:
                linha["ultima"] = ultima["valor_unitario"]
                linha["doc_referencia"] = _doc_referencia(ultima)

        if not cod_cadastro:
            linha["match_status"] = "sem_fornecedor"
        elif linha["ultima"] is None and linha["match_status"] != "sem_fornecedor":
            linha["match_status"] = linha["match_status"] if cod_produto else "sem_match"

        if linha["ultima"]:
            pct = (linha["atual"] - linha["ultima"]) / linha["ultima"] * 100
            linha["variacao_pct"] = round(pct, 1)
            linha["verdict"] = _verdict(pct)

        linhas.append(linha)

    return JSONResponse({
        "fornecedor_nome": fornecedor["nome_cadastro"] if fornecedor else (fornecedor_nome_extraido or "não identificado"),
        "fornecedor_encontrado": fornecedor is not None,
        "itens_count": len(linhas),
        "linhas": linhas,
    })


@app.post("/admin/sync-historico")
async def sync_historico(request: Request):
    """Recebe o payload do sync_comparador_simples.py (roda no PC do Renato)
    e faz upsert em massa no Postgres. Só existe utilidade em COMPARADOR_DB=postgres."""
    if DB_BACKEND != "postgres":
        return JSONResponse({"erro": "sync só se aplica com COMPARADOR_DB=postgres"}, status_code=400)

    token = os.environ.get("ADMIN_SYNC_TOKEN")
    auth = request.headers.get("authorization", "")
    if not token or auth != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="token inválido")

    payload = await request.json()
    resultado = db.sync_upsert(payload)
    return JSONResponse(resultado)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9100)
