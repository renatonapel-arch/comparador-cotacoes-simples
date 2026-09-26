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
import logging
import os
import re
import secrets
import unicodedata

import pandas as pd
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt as jose_jwt
from rapidfuzz import fuzz

import extracao
import matching

logger = logging.getLogger(__name__)

DB_BACKEND = os.environ.get("COMPARADOR_DB", "satlbase")
if DB_BACKEND == "postgres":
    import postgres_backend as db
else:
    import satlbase as db

APP_VERSION = "v3"

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


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _eh_napel(nome: str) -> bool:
    """A própria Napel nunca é fornecedor — ela é o destinatário da cotação.
    Caso real (3F, proposta 1020): o fornecedor só aparecia na logo, a IA
    devolveu o nome do CLIENTE, e o app identificou a Napel como fornecedor."""
    return "NAPEL" in _sem_acento(nome or "").upper()


def _candidatos_fornecedor_por_itens(descricoes: list[str], top_k: int = 10, min_score: float = 0.45) -> list[int]:
    """Votação: pra cada item, top-K produtos parecidos no catálogo; depois quem
    já VENDEU (compra EC) esses produtos pra Napel. Fornecedores votados por
    mais itens (e maior score) primeiro. É só um gerador de CANDIDATOS — a
    confirmação é da IA contra o histórico do candidato (mapear_itens_por_ia)."""
    try:
        catalogo = db.catalogo_produtos()
    except Exception as exc:  # noqa: BLE001
        logger.warning("catalogo_produtos falhou: %s", exc)
        return []
    if not catalogo:
        return []
    catalogo_norm = [(cod, _sem_acento(desc.upper())) for cod, desc in catalogo]

    candidatos_por_item: list[list[tuple[str, float]]] = []
    todos: set[str] = set()
    for desc in descricoes:
        d = _sem_acento((desc or "").upper())
        if not d:
            candidatos_por_item.append([])
            continue
        scored = sorted(
            ((cod, fuzz.token_set_ratio(d, cdesc) / 100.0) for cod, cdesc in catalogo_norm),
            key=lambda x: -x[1],
        )[:top_k]
        scored = [s for s in scored if s[1] >= min_score]
        candidatos_por_item.append(scored)
        todos.update(cod for cod, _ in scored)

    vendedores = db.fornecedores_de_produtos(list(todos))
    votos: dict[int, list] = {}
    for scored in candidatos_por_item:
        por_forn: dict[int, float] = {}
        for cod, sc in scored:
            for f in vendedores.get(cod, ()):
                por_forn[f] = max(por_forn.get(f, 0.0), sc)
        for f, sc in por_forn.items():
            v = votos.setdefault(f, [0, 0.0])
            v[0] += 1
            v[1] += sc

    min_itens = 1 if len(descricoes) == 1 else 2
    ranking = sorted(votos.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
    return [f for f, (n, _) in ranking if n >= min_itens][:3]


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
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


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
    itens_para_ia = [
        {"codigo": str(it.get("codigo") or "").strip(), "descricao": (it.get("descricao") or "").strip()}
        for it in itens_extraidos
    ]

    # ------------------- FORNECEDOR (evidência forte primeiro) -------------------
    # 1. código em tbProdutoFornecedor · 2. vínculo aprendido de cotação anterior ·
    # 3. votação pelos itens (quem já vendeu produtos parecidos, EC) confirmada
    #    por IA contra o histórico · 4. nome (bloqueando NAPEL, que é o cliente).
    fornecedor = db.find_fornecedor_por_codigos(codigos_forn)
    mapeamentos_ia: dict[str, dict] = {}
    if not fornecedor:
        fornecedor = db.fornecedor_por_vinculos_aprendidos(codigos_forn)
    if not fornecedor:
        descricoes = [i["descricao"] for i in itens_para_ia]
        for cand in _candidatos_fornecedor_por_itens(descricoes):
            historico_cand = db.produtos_historico_fornecedor(cand)
            m = matching.mapear_itens_por_ia(itens_para_ia, historico_cand, api_key)
            altas = sum(1 for v in m.values() if v["confianca"] == "alta")
            if altas >= max(1, (len(itens_para_ia) + 1) // 2):
                fornecedor = {"cod_cadastro": cand, "nome_cadastro": db.nome_cadastro(cand) or f"cadastro {cand}"}
                mapeamentos_ia = m
                break
    if not fornecedor and fornecedor_nome_extraido and not _eh_napel(fornecedor_nome_extraido):
        f = db.find_fornecedor(fornecedor_nome_extraido)
        if f and not _eh_napel(f["nome_cadastro"]):
            fornecedor = f

    linhas = []
    cod_cadastro = fornecedor["cod_cadastro"] if fornecedor else None

    # ------------------- PRODUTOS (por item, em camadas) -------------------
    mapa_exato = db.match_produtos(cod_cadastro, [c for c in codigos_forn if c]) if cod_cadastro else {}
    mapa_aprendido = db.vinculos_aprendidos_get(cod_cadastro, [c for c in codigos_forn if c]) if cod_cadastro else {}

    # IA sobre o histórico do fornecedor — só pros itens que código não resolveu
    if cod_cadastro and not mapeamentos_ia:
        pendentes = [
            i for i in itens_para_ia
            if i["codigo"] not in mapa_exato and i["codigo"] not in mapa_aprendido
        ]
        if pendentes:
            historico = db.produtos_historico_fornecedor(cod_cadastro)
            mapeamentos_ia = matching.mapear_itens_por_ia(pendentes, historico, api_key)

    cods_produto_ok = list(
        set(mapa_exato.values())
        | set(mapa_aprendido.values())
        | {v["cod_produto"] for v in mapeamentos_ia.values()}
    )
    ultimas = db.ultima_compra(cod_cadastro, cods_produto_ok, filial) if cod_cadastro and cods_produto_ok else {}
    descricoes_sige = db.descricao_produto(cods_produto_ok) if cods_produto_ok else {}

    aprender: list[tuple[str, str, str]] = []

    for item in itens_extraidos:
        codigo = str(item.get("codigo") or "").strip()
        descricao_forn = item.get("descricao") or "(sem descrição)"
        valor_doc = item.get("valor_unitario_documento")
        unid_embalagem = item.get("unidades_por_embalagem") or 1

        linha = {
            "produto": descricao_forn,
            "codigo_fornecedor": codigo,
            "match_status": "sem_fornecedor",
            "revisar": False,
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

        cod_produto = None
        if cod_cadastro:
            ia = mapeamentos_ia.get(codigo)
            if codigo in mapa_exato:
                cod_produto = mapa_exato[codigo]
                linha["match_status"] = "exato"
            elif codigo in mapa_aprendido:
                cod_produto = mapa_aprendido[codigo]
                linha["match_status"] = "aprendido"
            elif ia:
                cod_produto = ia["cod_produto"]
                linha["match_status"] = "ia_historico"
                linha["revisar"] = ia["confianca"] != "alta"
                if codigo and ia["confianca"] == "alta":
                    aprender.append((codigo, cod_produto, "ia_historico"))
            else:
                # último recurso: fuzzy no catálogo inteiro — vocabulários
                # diferentes tornam isso pouco confiável (caso 3F: casou
                # PICK-UP CORSA pra um item da COURIER), então threshold alto
                # e SEMPRE marcado pra revisão. Nunca persiste.
                fuzzy = db.match_produto_fuzzy(descricao_forn, threshold=0.80)
                if fuzzy:
                    cod_produto = fuzzy["cod_produto"]
                    linha["match_status"] = "fuzzy"
                    linha["revisar"] = True

        if cod_produto is not None:
            if descricoes_sige.get(cod_produto):
                linha["produto"] = descricoes_sige[cod_produto]
            else:
                descr = db.descricao_produto([cod_produto]).get(cod_produto)
                if descr:
                    linha["produto"] = descr
            ultima = ultimas.get(cod_produto)
            if ultima is None:
                ultima = db.ultima_compra(cod_cadastro, [cod_produto], filial).get(cod_produto)
            if ultima:
                linha["ultima"] = ultima["valor_unitario"]
                linha["doc_referencia"] = _doc_referencia(ultima)

        if not cod_cadastro:
            linha["match_status"] = "sem_fornecedor"
        elif cod_produto is None:
            linha["match_status"] = "sem_match"

        if linha["ultima"]:
            pct = (linha["atual"] - linha["ultima"]) / linha["ultima"] * 100
            linha["variacao_pct"] = round(pct, 1)
            linha["verdict"] = _verdict(pct)

        linhas.append(linha)

    # persiste vínculos com evidência forte (IA alta) — próxima cotação do
    # mesmo fornecedor resolve por código, instantâneo e sem IA
    if cod_cadastro and aprender:
        try:
            db.vinculos_aprendidos_put(cod_cadastro, aprender)
        except Exception as exc:  # noqa: BLE001
            logger.warning("falha ao gravar vínculos aprendidos: %s", exc)

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
