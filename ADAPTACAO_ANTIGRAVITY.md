# Adaptacao do kg-infra para Antigravity

> Documento para o Antigravity adaptar o kg-infra (knowledge graph MCP) para sua arquitetura.
> Repo oficial: https://github.com/70gurupia/kg-infra

---

## O que e o kg-infra

MCP server (Model Context Protocol) que implementa um knowledge graph de negocio + codigo
em SQLite, com 36 tools, zero dependencias externas (Python stdlib only), 100% local.

- Linguagem: Python 3.10+ (usa ast.unparse, disponivel desde 3.9)
- DB: SQLite com WAL mode, foreign keys, PRAGMA optimizations
- Protocolo: JSON-RPC 2.0 over stdio (MCP 2024-11-05)
- Dependencias: ZERO (so stdlib: sqlite3, json, ast, os, re, time, hashlib, pathlib)
- Tamanho: 3340 linhas (server.py) + 129 linhas (schema.sql) + 507 linhas (testes)
- Testes: 73 testes, 0 falhas

---

## Estrutura dos arquivos

```
kg-infra/
  server.py          # MCP server com 36 tools + helpers + protocolo JSON-RPC
  schema.sql         # Schema do SQLite (nodes, edges, communities, telemetry_spans, audit_log)
  test_kg_infra.py   # 73 testes (setup DB temporario, roda todas as tools, valida retorno)
  cli.py             # CLI wrapper para testar tools via linha de comando
  .gitignore         # Protege kg.db, backups, grafo-out/, __pycache__/
  README.md          # Documentacao completa
  REFERENCIA.md      # Referencia rapida das 36 tools
```

---

## Schema do banco (schema.sql)

### Tabela: nodes
```sql
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,              -- tipo do no (Customer, Function, File, etc)
    name TEXT NOT NULL,               -- nome legivel
    qualified_name TEXT UNIQUE NOT NULL, -- identificador unico (ex: func:proj:file:nome)
    properties TEXT DEFAULT '{}',     -- JSON com propriedades arbitrarias
    provenance TEXT DEFAULT 'EXTRACTED', -- EXTRACTED, INFERRED ou AMBIGUOUS
    source TEXT,                      -- quem criou (tool name, agent name)
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

### Tabela: edges
```sql
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    type TEXT NOT NULL,               -- tipo da aresta (CALLS_FUNC, bought, etc)
    properties TEXT DEFAULT '{}',
    provenance TEXT DEFAULT 'EXTRACTED',
    weight REAL DEFAULT 1.0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source_id, target_id, type)
);
```

### Tabela: communities
```sql
CREATE TABLE IF NOT EXISTS communities (
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    community_id INTEGER NOT NULL,
    algorithm TEXT DEFAULT 'connected_components',
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (node_id, algorithm)
);
```

### Tabela: telemetry_spans
```sql
CREATE TABLE IF NOT EXISTS telemetry_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_id TEXT,
    tool TEXT NOT NULL,
    args_summary TEXT,                -- JSON redacted (PII removido)
    result_size INTEGER,
    duration_ms REAL,
    error TEXT,
    timestamp TEXT DEFAULT (datetime('now')),
    -- Colunas adicionadas via migracao em main():
    agent_id TEXT,                    -- qual agente executou
    cost_usd REAL,                    -- custo estimado em USD
    checkpoint TEXT                   -- estado serializado para replay
);
```

### Tabela: audit_log
```sql
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,              -- node_create, node_update, node_delete, edge_create, etc
    entity_type TEXT NOT NULL,        -- node ou edge
    entity_id INTEGER,
    label TEXT,
    qualified_name TEXT,
    source TEXT,
    timestamp TEXT DEFAULT (datetime('now'))
);
```

### Indexes
```sql
CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);
CREATE INDEX IF NOT EXISTS idx_nodes_qualified_name ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
CREATE INDEX IF NOT EXISTS idx_telemetry_trace ON telemetry_spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_tool ON telemetry_spans(tool);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
```

---

## As 36 tools MCP (categorias)

### Categoria 1: Escrita (6 tools)
| Tool | Args | O que faz |
|---|---|---|
| add_node | label, name, qualified_name, properties?, provenance?, source? | Cria um no |
| upsert_node | label, name, qualified_name, properties?, provenance?, source? | Cria ou atualiza um no |
| add_edge | source, target, type, properties?, provenance?, weight? | Cria uma aresta |
| add_nodes_batch | nodes: [{label, name, qualified_name, properties?}] | Cria varios nos (max 10000) |
| add_edges_batch | edges: [{source, target, type, properties?}] | Cria varias arestas (max 10000) |
| delete_node | id ou qualified_name | Remove no e arestas em cascade |

### Categoria 2: Leitura basica (5 tools)
| Tool | Args | O que faz |
|---|---|---|
| get_node | id ou qualified_name | Detalhes de um no com arestas |
| search_graph | query, label?, limit? | Busca full-text em nos |
| trace_path | source, target, max_depth? | Um caminho entre 2 nos (BFS) |
| get_architecture | project? | Visao geral: labels, edge types, stats |
| get_graph_schema | nenhum | Lista labels e edge types validos |

### Categoria 3: Query SQL (1 tool)
| Tool | Args | O que faz |
|---|---|---|
| query_graph | sql, limit? | Executa SQL read-only (bloqueia PRAGMA, INSERT, UPDATE, DELETE) |

### Categoria 4: Listagem e export (3 tools)
| Tool | Args | O que faz |
|---|---|---|
| list_projects | nenhum | Lista projetos no grafo com stats |
| export_json | output_path? | Exporta grafo como JSON |
| export_html | output_path? | Exporta grafo como HTML interativo (vis.js inline) |

### Categoria 5: Analise de grafo (3 tools)
| Tool | Args | O que faz |
|---|---|---|
| detect_communities | algorithm? (connected_components/louvain) | Detecta comunidades |
| get_centrality | algorithm? (betweenness/closeness/degree/eigenvector) | Calcula centralidade |
| set_community | node, community_id | Atribui no a comunidade manualmente |

### Categoria 6: Telemetria e infra (5 tools)
| Tool | Args | O que faz |
|---|---|---|
| get_telemetry | trace_id?, tool?, limit? | Query em telemetry_spans |
| generate_report | output_path? | Gera GRAPH_REPORT.md (god nodes, surprising connections) |
| export_all | output_dir? | Exporta tudo: HTML, JSON, report, health, communities, centrality |
| health_check | nenhum | Status: ok/degraded/critical, db_size, latency, error_rate |
| integrity_check | nenhum | Verifica integridade do SQLite |
| backup | output_path? | VACUUM INTO com rotacao de 30 dias |

### Categoria 7: Analise de impacto (7 tools)
| Tool | Args | O que faz |
|---|---|---|
| get_impact | node, max_depth?, direction? | Blast radius de um no (BFS por profundidade) |
| trace_paths | source, target, max_paths?, max_hops? | Multiplos caminhos (DFS com poda) |
| explain_node | node, depth?, limit_neighbors? | Subgrafo ao redor de um no |
| what_if_remove | node | Simula remocao (arestas perdidas, risco) |
| replay_trace | trace_id, limit? | Reconstrui fluxo de execucao |
| get_impact_summary | edge_type, limit? | Resume impacto de tipo de aresta |
| find_orphans | limit? | Nos isolados e arestas AMBIGUOUS |

### Categoria 8: Codigo AST (5 tools)
| Tool | Args | O que faz |
|---|---|---|
| scan_codebase | path, max_files?, exclude? | Mapeia dir Python via ast module |
| get_call_graph | project?, direction?, limit? | Grafo de chamadas |
| get_import_graph | project?, limit? | Grafo de imports |
| find_circular_imports | project?, max_depth? | Detecta imports circulares |
| get_code_impact | function, max_depth? | Blast radius de funcao |

---

## Labels validos (34)

### Negocio (24)
Customer, Company, Contact, Product, Deal, Ticket, Issue, Agent, Channel,
Article, Topic, Keyword, Campaign, Audience, Interaction, Proposal, Feedback,
Lead, Opportunity, Contract, Service, Department, Event, Document, Note,
Tag, Person, Organization

### Infraestrutura (5)
Project, Module, Config, Folder, File

### Codigo AST (5)
Function, Class, Import, Variable, Decorator

## Edge types validos (31)

### Negocio (26)
works_at, bought, interested_in, contacted_via, opened_ticket, complained_about,
resolved_by, mentioned_in, about, targets, published_in, links_to, proposed_to,
signed, renewed, churned, referred_by, manages, belongs_to, part_of, related_to,
converted_to, assigned_to, escalated_to, responded_to, follows_up, authored,
reviewed, approved, attended, registered_for

### Infraestrutura (12)
CONTAINS, USES, DOCUMENTS, IMPLEMENTS, TESTS, DEFINES, EXPOSES_MCP,
CONFIGURES, RUNS, MONITORS, INTEGRATES_WITH, DEPENDS_ON, IMPORTS, CALLS, HAS_MODULE

### Codigo AST (8)
DEFINES_FUNC, DEFINES_CLASS, DECORATES, INHERITS_FROM, IMPORTS_FROM, CALLS_FUNC,
READS_VAR, WRITES_VAR

---

## Helpers internos (reusar, nao reescrever)

| Helper | Linha* | O que faz |
|---|---|---|
| `get_db()` | 66 | Conecta SQLite com PRAGMAs otimizados |
| `audit_log(conn, event, ...)` | 80 | Registra evento no audit_log |
| `normalize_name(s)` | ~100 | Normaliza string para qualified_name |
| `_resolve_node(conn, ref)` | ~712 | Resolve int (id) ou str (qualified_name) para node_id |
| `_filter_sensitive_props(props)` | ~441 | Remove PII de propriedades (email, cpf, password, etc) |
| `_redact_pii(s)` | ~1732 | Redact PII em string JSON (regex) |
| `validate_edge_type(t)` | ~107 | Valida edge type contra allowlist |
| `_parse_python_file(filepath)` | ~2484 | Parse AST de arquivo Python |
| `_safe_json_loads_list(raw)` | ~2473 | json.loads com fallback para lista vazia |

*Linhas aproximadas, podem mudar.

---

## Protocolo MCP (JSON-RPC 2.0)

O server.py implementa o protocolo MCP sobre stdio:

1. `initialize` -> retorna protocolVersion, capabilities, serverInfo
2. `tools/list` -> retorna lista de 36 tools com nome e descricao
3. `tools/call` -> executa tool com args, retorna resultado ou erro
4. `notifications/initialized` -> sem resposta (acknowledge)
5. `shutdown` -> graceful shutdown

Cada tool call:
- Valida args (tipo, tamanho, limites)
- Abre conexao SQLite (try/finally para fechar)
- Executa logica
- Registra telemetry_span (com duration_ms, args redacted, result_size)
- Retorna dict (JSON serializado)

Tratamento de erro em 4 camadas:
1. ValueError/KeyError -> args invalidos (400)
2. sqlite3.Error -> erro de DB (500 com hint)
3. Exception -> erro inesperado (500 generico)
4. Circuit breaker -> bloqueia tool que falha 5+ vezes em 60s

---

## Como adaptar para Antigravity

### Passo 1: Entender o contrato
O kg-infra e um MCP server que recebe JSON-RPC over stdio e responde JSON.
O Antigravity precisa:
1. Spawnar o processo `python3 server.py`
2. Enviar JSON-RPC via stdin
3. Ler respostas via stdout
4. Usar as 36 tools para manipular o knowledge graph

### Passo 2: Configurar como MCP server
No config do Antigravity, adicionar:
```json
{
  "mcpServers": {
    "kg-infra": {
      "command": "python3",
      "args": ["/caminho/para/kg-infra/server.py"],
      "env": {
        "KG_DB_PATH": "/caminho/para/kg.db"
      }
    }
  }
}
```

### Passo 3: Adaptar a linguagem (se Antigravity nao for Python)
Se o Antigravity usa outra linguagem (TypeScript, Go, Rust), portar:

1. **Schema SQL**: copiar schema.sql direto (SQLite e agnostico)
2. **Protocolo JSON-RPC**: implementar leitura/escrita em stdio
3. **Tools**: portar cada `tool_*` function, mantendo:
   - Validacao de input (tipo, tamanho, limites)
   - Parameterized queries (nunca concatenar SQL)
   - PII filtering em output e telemetry
   - Audit log em operacoes de escrita
   - Telemetry spans em todas as chamadas
   - Circuit breaker
4. **Helpers**: portar get_db, _resolve_node, _filter_sensitive_props, etc
5. **AST parsing**: se nao tiver ast module, usar tree-sitter ou equivalente

### Passo 4: Testar
Portar os 73 testes de test_kg_infra.py. Cada teste:
1. Cria DB temporario
2. Popula com dados de teste
3. Chama uma tool
4. Valida o retorno
5. Limpa DB temporario

### Passo 5: Integrar com skills
O kg-infra tem uma skill em `~/.config/devin/skills/kg-infra/SKILL.md` que ensina
o agente a traduzir perguntas naturais em chamadas MCP. Adaptar para o Antigravity:
- Manter a tabela de traducao (pergunta -> tool + args)
- Manter os exemplos de uso
- Ajustar a sintaxe de chamada se necessario

---

## Seguranca (nao pular)

1. **PII filter**: _filter_sensitive_props remove email, phone, cpf, cnpj, password,
   token, secret, api_key, credit_card de qualquer output
2. **Redact em telemetry**: _redact_pii aplica regex em args_summary antes de salvar
3. **Path traversal**: scan_codebase so aceita paths dentro de /home/
4. **SQL injection**: todas as queries usam parameterized queries (?)
5. **Query read-only**: query_graph bloqueia PRAGMA, INSERT, UPDATE, DELETE, ATTACH
6. **Allowlist de labels/edges**: VALID_LABELS e VALID_EDGE_TYPES sao allowlists
7. **Audit log**: toda escrita registra no audit_log (quem, o que, quando)
8. **Circuit breaker**: tool que falha 5+ vezes em 60s e bloqueada
9. **Timeout**: 30s max por tool call
10. **Idle timeout**: 30min sem input -> self-terminate

---

## Performance

- SQLite com WAL mode, busy_timeout=5000, synchronous=NORMAL
- PRAGMA cache_size=64MB, mmap_size=256MB, temp_store=MEMORY
- Composite indexes em nodes(label), edges(source_id), edges(target_id), edges(type)
- Guarda de tamanho: MAX_GRAPH_NODES_FOR_ALGO=500 (betweenness/closeness skip acima)
- Limite de placeholders SQLite: 900 params max em queries IN(...)
- Batch: max 10000 itens ou 50MB por batch
- Backup: VACUUM INTO com rotacao automatica de 30 dias

---

## O que NAO existe (e talvez precise)

1. **Multi-linguagem AST**: so suporta Python (ast module). Para JS/TS/Go/Rust,
   precisaria tree-sitter ou equivalente
2. **Vector search**: nao tem embedding/semantic search. search_graph e full-text LIKE
3. **Auth/RBAC**: nao tem autenticacao (MCP local, confianca no processo pai)
4. **Multi-tenant**: um DB por instancia (KG_DB_PATH env var)
5. **Real-time updates**: nao tem WebSocket/SSE, e request-response sobre stdio
6. **Schema migration versioning**: migracao ad-hoc em main(), sem sistema de versions

---

## Contato

Repo: https://github.com/70gurupia/kg-infra
Autor: vsf (70gurupia)
