# Knowledge Graph MCP Server

> Servidor MCP (Model Context Protocol) com 36 tools para knowledge graph de negócio. Python stdlib puro, zero dependências, 100% local (LGPD-safe). SQLite como armazenamento, vis.js para visualização interativa.

## Stack

| Camada | Tecnologia | Por quê |
|--------|-----------|---------|
| Linguagem | Python 3.10+ (stdlib puro) | Zero dependências, máxima portabilidade |
| Armazenamento | SQLite (WAL mode) | Local, sem servidor, ACID, LGPD-safe |
| Protocolo | MCP (JSON-RPC sobre stdio) | Padrão para comunicação com LLMs |
| Visualização | vis.js (client-side) | Grafo interativo com dark mode, clustering, painel |
| Testes | Framework custom | Testa segurança (path traversal, SQL injection, batch limits) |

## O que aprendi

- **Zero dependências é possível**: construí um servidor MCP completo com 36 tools usando só Python stdlib. Sem pip install, sem conflitos de versão, sem supply chain risk. Isso força a entender como as coisas funcionam por baixo.
- **Segurança em consultas SQL**: o `query_graph` tool usa allowlist de tabelas, bloqueia `sqlite_master`, `INSERT`, `PRAGMA`, `UNION` e subqueries. Validação por allowlist, nunca denylist.
- **Path traversal defense**: `export_html` e `generate_report` validam o path de saída contra traversal (`/etc/cron.d/evil.html` é rejeitado) e extensões perigosas (`.sh` é rejeitado).
- **Algoritmos de grafo em Python puro**: implementei Louvain (detecção de comunidades), PageRank, betweenness e closeness centralidade sem NetworkX. Entender o algoritmo é diferente de chamar uma função.
- **MCP como protocolo**: JSON-RPC sobre stdio é elegante para comunicação com LLMs. Sem HTTP, sem WebSocket, sem framework. Só stdin/stdout.
- **LGPD por design**: 100% local, nenhum dado sai do host. SQLite com WAL mode para concorrência segura. Backup via `VACUUM INTO`.

## Funcionalidades

- **36 tools MCP** em 5 categorias:
  - CRUD: `add_node`, `add_edge`, `delete_node`, `add_nodes_batch`, `add_edges_batch`
  - Busca: `search_graph`, `query_graph`, `trace_path`, `get_node`, `get_edge`
  - Análise: `get_centrality`, `detect_communities`, `shortest_path`, `neighbors`
  - Export: `export_html`, `export_json`, `export_all`, `generate_report`
  - Manutenção: `health_check`, `integrity_check`, `backup`, `get_telemetry`
- **Visualização interativa**: grafo HTML com vis.js, dark mode, clustering automático, painel de detalhes, busca por node.
- **Detecção de comunidades**: algoritmo Louvain implementado em Python puro.
- **Métricas de centralidade**: degree, betweenness, closeness, PageRank.
- **Telemetria**: latência, error rate, throughput por tool.
- **Backup**: `VACUUM INTO` para backup consistente sem bloquear escrita.

## Como rodar

```bash
# Sem instalação. Python 3.10+ nativo.
python3 server.py  # inicia servidor MCP stdio

# Popular com dados de exemplo
python3 seed.py

# Rodar testes
python3 test_kg_infra.py

# Exportar visualização
python3 export.py --html grafo.html
```

## Arquitetura

```
server.py          # Servidor MCP (JSON-RPC stdio), 3.340 linhas, 36 tools
schema.sql         # Schema SQLite (nodes, edges, telemetry, metadata)
seed.py            # Dados de exemplo
seed_full.py       # Dataset completo para demo
export.py          # CLI para export HTML/JSON
cli.py             # CLI para queries diretas
test_kg_infra.py   # Testes de segurança e funcionalais
```

## Testes

Testes focados em segurança e edge cases:

- Path traversal em `export_html` e `generate_report`
- SQL injection em `query_graph` (UNION, subquery, PRAGMA, sqlite_master)
- Batch limits em `add_nodes_batch` (excede limite = erro)
- Validação de labels e names (vazio, inválido)
- Window inválido em `get_telemetry`
- Delete com ID inválido

```bash
python3 test_kg_infra.py
```

## Segurança

- **Query graph**: allowlist de tabelas, bloqueia `sqlite_master`, `INSERT`, `PRAGMA`, `UNION`, subqueries.
- **Export**: path traversal bloqueado, extensões perigosas bloqueadas (`.sh`, `.py`, `.exe`).
- **Batch**: limite máximo de nodes/edges por batch para prevenir DoS.
- **LGPD**: 100% local, nenhum dado sai do host.
- **Threat model**: ver `THREAT-MODEL.md` para modelagem STRIDE completa.

## Licença

[MIT](LICENSE)
