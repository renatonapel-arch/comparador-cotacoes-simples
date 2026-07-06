# Comparador de Cotações — Versão Simples — CONTINUAR AQUI

> **STATUS (06/07/2026): app funcional e validado ponta a ponta com o PDF real da
> Alisul/Supra (seção 4) — resultado bateu 100% com a tabela esperada, nos dois
> modos (arquivo e texto colado). Rodando em `http://127.0.0.1:9100`.**
>
> Decisões que mudaram durante a implementação (não estavam previstas neste doc):
> - **Extração por Gemini, não Claude/Anthropic** — `ANTHROPIC_API_KEY` está
>   revogada desde 06/2026 (vazamento). Padrão Napel atual pra visão é Gemini
>   (`gemini-2.5-flash`, REST puro via `httpx`, igual ao
>   `backend/app/integrations/gemini.py` do Compras do Caixa). PDF é lido nativo
>   (não precisou rasterizar/PyMuPDF) — Gemini enxergou a logo "Supra" embutida
>   na imagem sem ajuda.
> - **Identificação do fornecedor por CÓDIGO de item, não por nome** — o nome que
>   a IA extrai pode ser a MARCA ("Supra"), não a razão social cadastrada
>   ("ALISUL ALIMENTOS SA"). `find_fornecedor_por_codigos()` casa os códigos
>   cotados contra `tbProdutoFornecedor` de TODOS os fornecedores e pega o
>   `Cod_cadastro` que mais bate — só cai no fuzzy por nome se nenhum código bater.
> - **`pyodbc` funcionou direto** nesta sessão (driver "SQL Server" testado e
>   validado com query real) — não foi preciso o fallback PowerShell/.NET.
> - Stack final: **FastAPI** (não `http.server` puro) por causa do multipart de
>   upload — Opção A da seção 7, como já era esperado.
>
> Arquivos: `app.py` (rotas + orquestração), `extracao.py` (Gemini), `satlbase.py`
> (SATLBASE), `static/index.html` (frontend, igual ao mockup + fetch real).
> `requirements.txt` criado. Pra rodar: `python app.py` (porta 9100, registrada
> em `portas-demos.json`).
>
> Pendente (fora de escopo, ver seção 9 original): persistência/histórico, deploy
> em VPS. Rodar local por enquanto.

---


> Handoff completo pra uma sessão nova continuar do zero, sem perguntar nada que já
> está resolvido aqui. Leia inteiro antes de tocar em código.

---

## 1. Objetivo (o que o Renato pediu, literal)

Renato pediu o **mockup mais simples possível** de uma ferramenta onde ele:

1. Fornece um PDF, planilha, foto/print, **qualquer tipo de arquivo**, OU cola texto solto
   num campo de texto longo (e-mail/WhatsApp do fornecedor com a cotação).
2. Clica um botão.
3. Recebe uma **tabela comparativa**: cada item da cotação, preço atual por unidade,
   preço da última compra por unidade, variação % (▼ mais barato · = igual · ▲ mais caro).

O mockup visual (não-funcional) já foi validado e está pronto. **Esta sessão nova deve
transformá-lo num app 100% funcional, rodando local no PC do Renato.**

## 2. Onde está tudo

| O quê | Caminho |
|---|---|
| **Mockup validado** (referência de layout/fluxo/design — NÃO mexer no visual sem necessidade) | `C:\Users\Renato\Downloads\comparador-cotacoes-simples-mockup.html` |
| Logo usado no mockup (copiar pra pasta do app novo) | `C:\Users\Renato\Downloads\logo-mark-t.svg` (original em `C:\Users\Renato\agents\napel-mockup\assets\logo-mark-t.svg`) |
| Playbook de design (Napel navy/sky, Archivo/Inter, zero emoji) | `C:\Users\Renato\scripts-clavis\PLAYBOOK-UI-UX-SENIOR.md` |
| Playbook de **demo funcional** (stack zero-build, gotchas de servidor local) | `C:\Users\Renato\scripts-clavis\PLAYBOOK-DEMO-FUNCIONAL.md` — **leia antes de escrever o backend** |
| CSS tokens prontos (copiar pro `<style>`) | `C:\Users\Renato\scripts-clavis\ui-ux-snippets\tokens-components.css` |
| Sprite de ícones SVG (zero emoji) | `C:\Users\Renato\scripts-clavis\ui-ux-snippets\icons-sprite.html` |
| Pasta deste projeto novo (código vai aqui) | `C:\Users\Renato\scripts-clavis\comparador-cotacoes-simples\` |

## 3. Já existe uma versão — mais complexa e específica (NÃO é este projeto)

Existe hoje em produção `https://comparador-cotacoes.demos.napel.com.br` — um comparador
**só de peças automotivas**, com 3 templates de PDF fixos (NAPEL 305, NCP, Marcparts/Usilux),
matching por código de peça, cálculo de ST/IPI/frete rateado, login multiusuário, histórico
persistido em Postgres, sync do SATLBASE a cada 6h via túnel. Documentado inteiro em
`C:\Users\Renato\Downloads\comparador-cotacoes-como-funciona.md` (leia a seção 6, 7 e 8 —
o algoritmo de matching e a fórmula de preço real são reaproveitáveis como **inspiração**,
mas não copie a arquitetura toda).

**Este projeto novo é deliberadamente mais simples e mais genérico:**

| | Comparador antigo (peças, VPS) | Este projeto (qualquer categoria, local) |
|---|---|---|
| Entrada | só PDF, só 3 templates fixos | qualquer arquivo OU texto colado, sem template fixo |
| Categoria | só autopeça | qualquer (testado com ração da Alisul/Supra) |
| Onde roda | VPS/Coolify, sem acesso direto ao SATLBASE | **PC do Renato — acesso direto e ao vivo ao SATLBASE** |
| Telas | login, lista, KPIs, split vs consolidado, histórico | 1 tela: entrada → botão → tabela |
| Extração | `pdfplumber` com regras por template (código Python fixo) | **IA (Claude) lendo o conteúdo e devolvendo JSON estruturado** — não dá pra fixar template pois aceita "qualquer arquivo" |

## 4. Exemplo real já validado nesta sessão (use pra testar)

Fornecedor: **ALISUL ALIMENTOS SA** (marca Supra), ração animal — cotação real de 06/07/2026
recebida em `C:\Users\Renato\Downloads\confirmacao-pedido-orcamento-060720261120.pdf`.

Esse PDF **não tem nome do fornecedor no texto** — só aparece via **logo embutida como imagem**
(teve que extrair a imagem com PyMuPDF e olhar visualmente pra identificar "SUPRA — DESDE 1979").
Isso já prova que o parsing genérico **não pode depender só de regex no texto** — precisa lidar
com fornecedor identificável só pela cotação em si (nome do produto, código, ou pedir a IA pra
inferir pelo padrão dos itens).

### Mapeamento validado (código do fornecedor → produto interno Napel)

Tabela `tbProdutoFornecedor` já tem o cadastro — **não precisa recriar isso, só consultar**:

| Cod_produto_forn (código no PDF do fornecedor) | Cod_produto (interno SIGE) | Descrição | Fardo/caixa → unidade |
|---|---|---|---|
| `667E12*0.45` | 108903 | BENEFIT CLASSIC 0,450 GRAMAS | CX de 12×0,45kg → ÷12 |
| `095C5*5` | 108873 | GALO DE OURO 5.0 KG | FD de 5×5kg → ÷5 |
| `1645E4*2.5` | 108870 | FROST SENSITIVE SKIN MINI & SMALL 2.5KG | FD de 4×2,5kg → ÷4 |
| `074T5*5` | 108875 | SUPRA CODORNA POSTURA 5.0 KG | FD de 5×5kg → ÷5 |
| `668E12*0.45` | 108900 | BENEFIT ALLFA 0,450 GRAMAS | CX de 12×0,45kg → ÷12 |
| `700P6` | 108901 | BLOKUS 60 BK 6KG SAL EQUINO | já é unidade única, sem conversão |

Todos os 6 produtos são do fornecedor `Cod_cadastro = 46309` (ALISUL ALIMENTOS SA).
Todos têm `Cod_unidade_pri = 'UN'` em `tbproduto` — ou seja, **o SIGE sempre registra a
compra por unidade individual**, nunca por fardo/caixa. Todo item vendido em fardo/caixa
pelo fornecedor precisa ser convertido pra unidade antes de comparar.

### Resultado esperado (pra bater no teste)

| Produto | Cotação atual (R$/un) | Última compra (R$/un) | Variação |
|---|---|---|---|
| Benefit Classic 0,45kg | 5,54 | 5,54 (EC 4780, 01/07) | 0% |
| Frost Sensitive Skin 2,5kg | 54,96 | 54,97 (EC 4734, 16/06) | 0% |
| Galo de Ouro 5kg | 26,68 | 26,66 (EC 4780, 01/07) | 0% |
| Benefit Allfa 0,45kg | 5,67 | 5,59 (EC 4676, 26/05) | 1% |
| Blokus 60 Sal Equino 6kg | 35,19 | 33,20 (EC 4733, 16/06) | +6% |
| Supra Codorna Postura 5kg | 14,79 | 13,88 (EC 4472, 09/03 — 4 meses atrás) | +7% |

## 5. SQL validado (copiar direto, já testado nesta sessão)

### Conexão SATLBASE

Senha em `C:\Users\Renato\.claude\.env`, chave `AZURE_SQL_PASSWORD`. Servidor `SRV-BD`,
banco `SATLBASE`, usuário `clavis`, **somente leitura** (`SELECT` + `WITH (NOLOCK)`,
nunca `INSERT/UPDATE/DELETE`).

**Gotcha conhecido (já documentado em `reference_satlbase_acesso` / memória do Renato):**
no PC do Renato o `pyodbc` não é confiável — outra automação (`sync_warehouse.py` do
comparador antigo) usa **PowerShell + .NET `SqlClient`** em vez de pyodbc por esse motivo.
Se o backend novo for Python puro, teste `pyodbc` primeiro num script isolado antes de
depender dele — se falhar, use o padrão PowerShell/.NET já validado (subprocess chamando
um `.ps1`, ou `pythonnet`).

```powershell
$EnvPath = 'C:\Users\Renato\.claude\.env'
$pwd = ((Get-Content $EnvPath -Encoding UTF8 | Where-Object { $_ -match '^\s*AZURE_SQL_PASSWORD\s*=' } | Select-Object -First 1) -replace '^\s*AZURE_SQL_PASSWORD\s*=\s*','').Trim()
$cs = "Server=SRV-BD;Database=SATLBASE;User Id=clavis;Password=$pwd;TrustServerCertificate=True;Connect Timeout=15;"
$conn = New-Object System.Data.SqlClient.SqlConnection $cs
$conn.Open()
```

### Achar o fornecedor pelo nome (fuzzy, sem CNPJ)

```sql
SELECT Cod_cadastro, Nome_cadastro FROM tbCadastroGeral WITH (NOLOCK)
WHERE Nome_cadastro LIKE '%ALISUL%'   -- trocar pelo nome que a IA extraiu
```
Cuidado: `LIKE` com padrão curto/genérico demais (ex: `%RA%O%`) explode em milhares de
falso-positivo — teste sempre o termo mais específico possível primeiro (nome completo ou
maior trecho contíguo).

### Casar código do fornecedor → produto interno

```sql
SELECT Cod_produto, Cod_cadastro, Cod_produto_forn
FROM tbProdutoFornecedor WITH (NOLOCK)
WHERE Cod_produto_forn IN ('667E12*0.45','095C5*5', ...)   -- códigos extraídos do documento
  AND Cod_cadastro = 46309                                  -- fornecedor já identificado
```
Se não achar por código (fornecedor novo, código não cadastrado), fallback é comparar
`Desc_produto_est` de `tbproduto` por similaridade de texto (fuzzy) contra a descrição do
item na cotação — o comparador antigo já tem esse algoritmo pronto em 3 níveis (código
exato → substring → fuzzy de descrição), ver seção 6 do
`comparador-cotacoes-como-funciona.md`.

### Unidade cadastrada do produto (pra saber se precisa converter)

```sql
SELECT Cod_produto, Desc_produto_est, Cod_unidade_pri, Cod_unidade_aux
FROM tbproduto WITH (NOLOCK)
WHERE Cod_produto IN (108900,108901,108903,108870,108873,108875)
```

### Última(s) compra(s) por produto+fornecedor (a query-chave, já testada)

```sql
;WITH ultimas AS (
    SELECT i.Cod_produto, e.Data_movto, e.Num_docto, i.Qtde_pri, i.Valor_unitario, i.Valor_total,
           ROW_NUMBER() OVER (PARTITION BY i.Cod_produto ORDER BY e.Data_movto DESC) AS rn
    FROM tbentradasitem i WITH (NOLOCK)
    INNER JOIN tbentradas e WITH (NOLOCK) ON e.Chave_fato = i.Chave_fato
    WHERE e.Cod_cli_for = 46309              -- Cod_cadastro do fornecedor
      AND e.Cod_docto = 'EC'                 -- EC = Entrada de Compra (evita duplicar PCR/NFX/AVC da mesma compra)
      AND i.Cod_produto IN ('108900','108901','108903','108870','108873','108875')
)
SELECT * FROM ultimas WHERE rn <= 1 ORDER BY Cod_produto
```
**Por que `Cod_docto='EC'`:** a mesma compra gera várias linhas (PCR, EC, NFX, AVC) com
data/qtde/valor idênticos — sem esse filtro os resultados duplicam 4x. `EC` é o registro
canônico de "entrada de compra".

**Gotcha de nome de coluna:** sempre confira antes com
`SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='...'` —
nesta sessão já caímos em erro de coluna inexistente várias vezes (ex: `Serie_docto` não
existe em `tbentradas`, é `Serie_seq`; `tbCadastroGeral` usa `Cod_cadastro`, não `Codigo`).

## 6. Decisões de arquitetura já tomadas (não re-decidir)

1. **Stack:** seguir `PLAYBOOK-DEMO-FUNCIONAL.md` — zero Docker/Node, um `app.py` Python
   com `http.server` (ou Flask/FastAPI se o upload multipart pedir — ver gotcha abaixo),
   frontend é o mockup existente com o JS trocado de simulado pra `fetch()` real.
2. **SATLBASE ao vivo, sem relay:** como roda no PC do Renato (não na VPS), consulta o
   SATLBASE diretamente a cada comparação — **não precisa** do padrão Postgres-sync-6h
   que o comparador antigo usa (aquele existe só porque a VPS não tem rede pra SATLBASE).
3. **Extração genérica via IA:** como aceita "qualquer arquivo, qualquer categoria", a
   extração de itens (código, descrição, quantidade, unidade, valor unitário) deve ser
   via **Claude (Anthropic API)** lendo o conteúdo bruto (texto do PDF/imagem/planilha ou
   o texto colado) e devolvendo JSON estruturado — não dá pra fixar parsing por template
   como o comparador antigo faz.
4. **Fornecedor pode só aparecer via logo/imagem** (caso real testado, ver seção 4) — a
   extração por IA deve conseguir ler imagem também (Claude aceita imagem direto), não só
   texto.
5. **Conversão fardo→unidade:** pedir pra própria extração de IA já devolver "quantas
   unidades tem dentro da embalagem cotada" (ex: "CX 12×0,45KG" → 12), em vez de tentar
   regex genérico em cima do texto livre do fornecedor (formato varia demais).

## 7. Gotcha crítico ainda não resolvido — `cgi` foi removido no Python 3.14

O Renato tem **Python 3.14.3** instalado (`C:\Users\Renato\AppData\Local\Python\pythoncore-3.14-64\python.exe`).
O módulo `cgi` (usado tradicionalmente pra parsear `multipart/form-data` em upload de
arquivo com `http.server` puro) **foi removido no Python 3.13** (PEP 594) — não existe
mais. Isso quebra o padrão "zero-build stdlib" do `PLAYBOOK-DEMO-FUNCIONAL.md` especificamente
para a rota de **upload de arquivo** (a rota de "colar texto" não tem esse problema, é só
JSON simples).

Duas saídas, escolher uma no início (não deixar pra descobrir na hora):
- **Opção A (recomendada):** usar **FastAPI** só pra essa rota de upload (tem parsing de
  multipart pronto via `python-multipart`) — foge um pouco do "zero-build" do playbook,
  mas evita reinventar parser de multipart na mão. É também o padrão backend default da
  Napel (`CLAUDE.md` raiz do Clavis).
- **Opção B:** escrever um parser de multipart mínimo à mão (não é complicado — é só
  separar por boundary), mantendo tudo em stdlib puro.

## 8. Passo a passo sugerido

1. Copiar `comparador-cotacoes-simples-mockup.html` pra
   `scripts-clavis\comparador-cotacoes-simples\static\index.html` (ou pasta equivalente).
2. Escrever `app.py` com 1 rota: `POST /comparar` — recebe `{modo: 'arquivo'|'texto', conteudo: ...}`.
3. Dentro da rota: chamar Claude (Anthropic API, ler chave em `~/.claude/.env`) com prompt
   pedindo JSON: `{fornecedor_nome, itens: [{codigo, descricao, qtde, unidade, valor_unitario, unidades_por_embalagem}]}`.
4. Achar `Cod_cadastro` do fornecedor no SATLBASE (seção 5).
5. Pra cada item: achar `Cod_produto` via `tbProdutoFornecedor` (fallback fuzzy se não achar).
6. Calcular preço por unidade da cotação (`valor_unitario / unidades_por_embalagem`).
7. Consultar última compra (`EC` mais recente) desse `Cod_produto`+fornecedor.
8. Calcular variação % e verdict (▼ ≤-2% · = entre -2% e +2% · ▲ ≥+2% — mesma faixa de
   tolerância do comparador antigo, ver seção 9 do `.md` de referência).
9. Devolver JSON pro frontend, que troca o `setTimeout` fake do mockup por esse resultado real.
10. Testar com o PDF real da Alisul/Supra (seção 4) e conferir contra a tabela de resultado
    esperado.

## 9. O que este documento NÃO cobre (fora de escopo por enquanto)

- Persistência/histórico de cotações (o mockup não tem essa tela — decidir com o Renato
  se vale a pena antes de construir).
- Múltiplos usuários/login (demo local, 1 usuário).
- Deploy em VPS/Coolify — isso é outro passo, só depois de validado local (ver princípio
  "honestidade de escopo" do `PLAYBOOK-DEMO-FUNCIONAL.md` seção 8).

## 10. Como validar antes de dizer "pronto"

Seguir a seção 5 do `PLAYBOOK-DEMO-FUNCIONAL.md`: nunca pedir print ao Renato, usar
`agent-browser` (skill `usar-chrome-mcp`) pra abrir, screenshotar, ler o PNG e conferir
console limpo — igual foi feito com o mockup nesta sessão.
