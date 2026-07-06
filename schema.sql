-- Schema Postgres do Comparador de Cotações (versão simples) — produção.
-- Vive no MESMO Postgres compartilhado do Clavis (schema isolado, não mexe
-- em nada do Clavis). Aplicado uma vez; idempotente (IF NOT EXISTS).

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
CREATE INDEX IF NOT EXISTS idx_produto_fornecedor_produto ON comparador_simples.produto_fornecedor (cod_produto);

-- Última compra por (produto, fornecedor) — já resolvida no sync (prioridade
-- pro Cod_docto='EC' em empate de data, senão qualquer tipo de entrada, ver
-- CONTINUAR-AQUI.md sobre o caso do produto 108680/AJE).
CREATE TABLE IF NOT EXISTS comparador_simples.compras_historico (
    cod_produto TEXT NOT NULL,
    cod_cadastro INTEGER NOT NULL,
    data_movto DATE NOT NULL,
    num_docto INTEGER,
    valor_unitario NUMERIC(14,4) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cod_produto, cod_cadastro)
);
CREATE INDEX IF NOT EXISTS idx_compras_historico_produto ON comparador_simples.compras_historico (cod_produto, data_movto DESC);

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
