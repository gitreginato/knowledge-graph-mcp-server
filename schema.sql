-- kg-infra: Knowledge Graph de negocio (vendas + atendimento + conteudo)
-- SQLite puro, zero dependencias. LGPD-safe (dados locais).

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Nós: entidades do negocio (Customer, Company, Product, Deal, Ticket, etc.)
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,                        -- Customer, Company, Product, Deal, Ticket, Article, Topic, etc.
    name TEXT NOT NULL,                         -- nome legivel (ex: "Acme Corp")
    qualified_name TEXT UNIQUE,                 -- nome unico normalizado (ex: "company:acme-corp")
    properties TEXT DEFAULT '{}',               -- JSON com propriedades extras
    provenance TEXT NOT NULL DEFAULT 'EXTRACTED', -- EXTRACTED | INFERRED | AMBIGUOUS
    source TEXT,                                -- de onde veio (arquivo, API, LLM, manual)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_qualified_name ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_label_qname ON nodes(label, qualified_name);

-- Arestas: relacoes entre nos (bought, complained_about, works_at, etc.)
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    type TEXT NOT NULL,                         -- bought, complained_about, works_at, interested_in, etc.
    properties TEXT DEFAULT '{}',               -- JSON com propriedades extras
    provenance TEXT NOT NULL DEFAULT 'EXTRACTED',
    weight REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES nodes(id) ON DELETE CASCADE,
    UNIQUE(source_id, target_id, type)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
CREATE INDEX IF NOT EXISTS idx_edges_source_type ON edges(source_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_target_type ON edges(target_id, type);

-- Busca full-text (FTS5 nativo do SQLite)
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name,
    qualified_name,
    properties,
    content='nodes',
    content_rowid='id',
    tokenize='unicode61'
);

-- Triggers para manter FTS sincronizado
CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, name, qualified_name, properties)
    VALUES (new.id, new.name, COALESCE(new.qualified_name, ''), new.properties);
END;
CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, qualified_name, properties)
    VALUES ('delete', old.id, old.name, COALESCE(old.qualified_name, ''), old.properties);
END;
CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, qualified_name, properties)
    VALUES ('delete', old.id, old.name, COALESCE(old.qualified_name, ''), old.properties);
    INSERT INTO nodes_fts(rowid, name, qualified_name, properties)
    VALUES (new.id, new.name, COALESCE(new.qualified_name, ''), new.properties);
END;

-- Comunidades (detectadas offline ou manuais)
CREATE TABLE IF NOT EXISTS communities (
    node_id INTEGER NOT NULL,
    community_id INTEGER NOT NULL,
    algorithm TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (node_id, algorithm),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);

-- Metadados do grafo
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO metadata (key, value) VALUES ('schema_version', '1.1');
INSERT OR IGNORE INTO metadata (key, value) VALUES ('created_at', datetime('now'));

-- Audit log (C9: Security Logging and Monitoring)
-- Registra eventos de escrita: create/update/delete de nos e arestas
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,                    -- node_create, node_update, node_delete, edge_create, edge_update, community_set
    entity_type TEXT NOT NULL,              -- node, edge, community
    entity_id INTEGER,
    label TEXT,                              -- label do no ou type da aresta
    qualified_name TEXT,                    -- qualified_name do no, se aplicavel
    source TEXT,                             -- de onde veio a operacao (MCP tool name, CLI, etc.)
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event);

-- Telemetria/tracing (Task 4: spans em SQLite, zero deps, inspirado no TraceLite)
-- Registra latencia e erros de cada chamada de tool MCP
CREATE TABLE IF NOT EXISTS telemetry_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,                  -- UUID por request JSON-RPC
    span_id TEXT NOT NULL,                   -- UUID por tool call
    parent_id TEXT,                          -- span pai (para chamadas aninhadas)
    tool TEXT NOT NULL,                      -- nome da tool MCP
    duration_ms REAL NOT NULL,               -- duracao em milissegundos
    error TEXT,                              -- mensagem de erro, se houver
    args_summary TEXT,                       -- resumo dos args (sem dados sensiveis)
    result_size INTEGER,                     -- tamanho do resultado em bytes
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Migracao segura para DBs existentes: o server.py faz PRAGMA table_info + ALTER TABLE
-- condicional (ver main()), nao rodamos ALTER direto aqui para evitar OperationalError
-- em DBs onde as colunas ja existem.

CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp ON telemetry_spans(timestamp);
CREATE INDEX IF NOT EXISTS idx_telemetry_tool ON telemetry_spans(tool);
CREATE INDEX IF NOT EXISTS idx_telemetry_trace ON telemetry_spans(trace_id);
