# kg-infra: Knowledge Graph de Infraestrutura

Knowledge graph leve e seguro para dados de negocio (vendas, atendimento, conteudo).
Python puro + SQLite, zero dependencias, 100% local (LGPD-safe).

## Skill e MCP

O kg-infra tem 2 componentes:

1. **MCP server** (`server.py`): servidor stdio JSON-RPC com 24 tools. Configurado em
   `~/.config/devin/mcp_config.json` (Devin) e `~/.gemini/config/mcp_config.json` (Antigravity).
2. **Skill** (`~/.config/devin/skills/kg-infra/SKILL.md`): guia de uso que ensina o agente
   a traduzir perguntas em linguagem natural para chamadas das tools MCP corretas.
   Auto-descoberta pelo Devin.

Para referencia rapida de todas as tools, args e padroes de uso, ver
<ref_file file="/home/vsf/Projetos/kg-infra/REFERENCIA.md" />.

## Saida padrao: grafo-out/ e ~/Outputs/

Toda saida gerada segue uma convencao unifica para todos os agentes (Devin, Antigravity, Gemini):

```
<projeto>/grafo-out/     # Exports do MCP kg-infra (sempre ao lado do DB)
  graph.html             # Visualizacao interativa (vis.js) com dark mode, painel, clustering
  graph.json             # Grafo completo em JSON
  GRAPH_REPORT.md        # Relatorio (god nodes, surprising connections)
  HEALTH.json            # Health check (status, latencia, error_rate)
  COMMUNITIES.json       # Comunidades detectadas (Louvain)
  CENTRALITY.json        # Centralidade (degree, betweenness, closeness, pagerank)
  backups/               # Backups do banco (VACUUM INTO)

~/Outputs/sites/         # Sites gerados (landing pages, sites de vendas)
~/Outputs/relatorios/    # Relatorios (auditorias, analises, pesquisas)
~/Outputs/dashboards/    # Dashboards interativos
~/Outputs/grafos/        # Grafos avulsos (nao do MCP)
```

Para gerar tudo de uma vez: `export_all {}` (tool MCP).

## Skills relacionadas

| Skill | O que faz | Path |
|---|---|---|
| `kg-infra` | Guia de uso das 24 tools MCP (NL -> tool) | `~/.config/devin/skills/kg-infra/SKILL.md` |
| `robustness-audit` | Audita robustez de implementacao (8 categorias) | `~/.config/devin/skills/robustness-audit/SKILL.md` |
| `design-viz` | Design de visualizacoes interativas (grafos, charts) | `~/.config/devin/skills/design-viz/SKILL.md` |
| `data-storytelling` | Narrativa de dados (tese, causality, stakes, change) | `~/.config/devin/skills/data-storytelling/SKILL.md` |

## Comparativo com graphify

| Capacidade | graphify | kg-infra |
|---|---|---|
| Parsing de codigo (AST) | tree-sitter, 40+ linguagens | Nao (feito para negocio, nao codigo) |
| Deteccao de comunidades | Leiden (C++, leidenalg) | Louvain em Python puro (sem deps) |
| Visualizacao interativa | graph.html (vis.js) | graph.html (vis.js CDN, SRI, CSS inline) |
| Relatorio automatico | GRAPH_REPORT.md | GRAPH_REPORT.md (god nodes, surprising connections) |
| Metricas de centralidade | Implicito (god nodes) | PageRank, betweenness, closeness, degree (Python puro) |
| Telemetria/tracing | Nao | telemetry_spans (latencia p50/p90, top tools, erros) |
| Query semantica (NL) | graphify query "pergunta" | Prompt template (LLM traduz para search_graph/trace_path) |
| Provenance | EXTRACTED/INFERRED | EXTRACTED/INFERRED/AMBIGUOUS (mais granular) |
| Audit log | Nao | audit_log (C9 OWASP) |
| SQL query interface | Nao | query_graph (allowlist de tabelas) |
| Dependencias | Muitas (tree-sitter, leiden, etc.) | Zero (Python stdlib) |
| Supply chain | Falhou em 13 criterios | Passa em todos (zero deps) |

## Arquitetura

```
Dados de negocio (CSV, JSON, tickets, propostas, PDFs)
        |
        v
[1] Antigravity extrai entidades/relacoes (LLM)
        |
        v
[2] MCP server (server.py) recebe add_node/add_edge
        |
        v
[3] SQLite (kg.db) armazena grafo com provenance + audit_log + telemetry
        |
        v
[4] Devin/Antigravity consultam via 24 tools MCP
        |
        v
[5] Export: graph.html (vis.js) + GRAPH_REPORT.md + graph.json
```

## Arquivos

- `schema.sql` - Schema SQLite (nodes, edges, FTS5, communities, metadata, audit_log, telemetry_spans)
- `server.py` - MCP server (JSON-RPC 2.0 over stdio, 24 tools, 1484 linhas)
- `cli.py` - CLI para testes manuais
- `export.py` - Export para graph.json
- `seed.py` - Dados sinteticos de exemplo (23 nos, 28 arestas)
- `kg.db` - Banco SQLite (chmod 600, nao commitar)
- `graph.html` - Visualizacao interativa (gerado por export_html)
- `GRAPH_REPORT.md` - Relatorio automatico (gerado por generate_report)
- `THREAT-MODEL.md` - Threat model (STRIDE + Zero Trust + OWASP LLM)
- `.gitignore` - Protege kg.db, graph.json, graph.html

## Ferramentas MCP (24)

### Escrita (para Antigravity popular)
- `add_node` - Cria um no (label, name, properties, provenance)
- `upsert_node` - Cria ou atualiza (busca por qualified_name)
- `add_edge` - Cria aresta (source, target, type, provenance)
- `add_nodes_batch` - Bulk insert de nos (1 conexao)
- `add_edges_batch` - Bulk insert de arestas (1 conexao)
- `delete_node` - Deleta no e arestas (CASCADE)
- `set_community` - Atribui no a comunidade

### Leitura (para Devin e Antigravity consultarem)
- `list_projects` - Lista grafos e estatisticas
- `get_graph_schema` - Labels e tipos de aresta validos
- `search_graph` - Busca nos por nome/label (FTS5, sanitizado)
- `get_node` - Detalhes de um no com vizinhos
- `trace_path` - Caminho mais curto entre dois nos (BFS em memoria)
- `get_architecture` - Visao geral do grafo (otimizado)
- `query_graph` - Query SQL read-only (allowlist de tabelas)
- `export_json` - Export grafo completo como JSON

### Analise (novas features, paridade com graphify)
- `export_html` - Gera graph.html interativo (vis.js CDN, SRI, CSS inline, cores por label, shapes por provenance, filter, tooltips, toggle physics)
- `detect_communities` - Detecta comunidades automaticamente (Louvain ou connected_components, Python puro)
- `get_centrality` - Calcula centralidade: degree, betweenness, closeness, pagerank (Python puro, O(n^2) para <1000 nos)
- `get_telemetry` - Metricas de telemetria: latencia p50/p90, top tools, erros (spans em SQLite)
- `generate_report` - Gera GRAPH_REPORT.md: god nodes, surprising connections, suggested questions
- `export_all` - Gera todos os artefatos em grafo-out/: graph.html, graph.json, GRAPH_REPORT.md, HEALTH.json, COMMUNITIES.json, CENTRALITY.json
- `health_check` - Health check: status (ok/degraded/critical), db_size, node_count, latency_p50, error_rate, integrity
- `integrity_check` - Verifica integridade do banco SQLite (PRAGMA integrity_check + arestas orfas)
- `backup` - Cria backup manual do banco via VACUUM INTO (rotacao 30 dias)

## Query semantica (natural language)

O kg-infra nao tem um LLM embutido, mas o Devin/Antigravity podem traduzir
perguntas em linguagem natural para chamadas das tools. Use este prompt template:

```
Para responder perguntas sobre o knowledge graph de negocio, use as tools do kg-infra:

1. "Quem sao os clientes que X?" -> search_graph {name_pattern: "X", label: "Customer"}
2. "Como X conecta com Y?" -> trace_path {source: "X", target: "Y"}
3. "Quais os nos mais influentes?" -> get_centrality {metric: "pagerank"}
4. "Quais comunidades existem?" -> detect_communities {algorithm: "louvain"}
5. "Como esta o grafo?" -> get_architecture {}
6. "Gere um relatorio" -> generate_report {}
7. "Visualize o grafo" -> export_html {}
8. "Quais tickets abertos?" -> search_graph {name_pattern: "TKT", label: "Ticket"}
   depois query_graph {query: "SELECT * FROM nodes WHERE label='Ticket' AND json_extract(properties, '$.status')='open'"}
9. "Performance do server?" -> get_telemetry {window: 60}
10. "Query customizada" -> query_graph {query: "SELECT label, COUNT(*) FROM nodes GROUP BY label"}
```

## Como usar

### Popular o grafo (Antigravity)

```
"Antigravity, leia este PDF de proposta e extraia entidades/relacoes para o kg-infra.
Use get_graph_schema para ver os labels e tipos validos, depois add_node e add_edge."
```

### Consultar (Devin ou Antigravity)

```
"Devin, quem sao os clientes que compraram Plano Enterprise e abriram tickets?"
"Trace o caminho entre o cliente Acme Corp e o artigo sobre migracao cloud"
"Gere um relatorio do grafo de negocio"
"Visualize o grafo em HTML"
"Detecte comunidades automaticamente"
"Quais os nos mais influentes por PageRank?"
```

### CLI manual

```bash
python3 cli.py get_architecture '{}'
python3 cli.py search_graph '{"name_pattern":"acme"}'
python3 cli.py get_node '{"qualified_name":"customer:acme-corp"}'
python3 cli.py trace_path '{"source":"customer:acme-corp","target":"article:guia-de-migracao-para-cloud"}'
python3 cli.py detect_communities '{"algorithm":"louvain"}'
python3 cli.py get_centrality '{"metric":"pagerank","limit":10}'
python3 cli.py export_html '{}'
python3 cli.py generate_report '{}'
python3 cli.py get_telemetry '{"window":60}'
python3 export.py graph.json
```

### Visualizar

```bash
python3 cli.py export_html '{}'
# Abre graph.html no browser. Features:
# - Cores por label (29 cores distintas)
# - Shapes por provenance (circulo=EXTRACTED, triangulo=INFERRED, diamante=AMBIGUOUS)
# - Arestas tracejadas por provenance (solido=EXTRACTED, tracejado=INFERRED, pontilhado=AMBIGUOUS)
# - Tooltips com propriedades ao passar o mouse
# - Filter por nome, label, provenance
# - Toggle physics (liga/desliga layout automatico)
# - Fit (ajusta zoom ao grafo)
# - Navegacao por teclado e botoes
# - Legenda com labels e shapes
```

## Seguranca

- **100% local**: SQLite no disco, nada sai da maquina (LGPD-safe)
- **Zero dependencias**: Python 3 stdlib apenas (sqlite3, json, sys, unicodedata, math, collections, re, time, uuid)
- **Input validation**: Allowlist de labels e tipos de aresta (nao denylist)
- **SQL injection**: Parameterized queries em todas as operacoes
- **Query read-only**: `query_graph` so permite SELECT com allowlist de tabelas, bloqueia UNION, subqueries, INSERT/UPDATE/DELETE/CREATE/DROP/ALTER, `sqlite_master`, `pragma`
- **FTS injection**: `search_graph` sanitiza padrao FTS5 (remove regex chars, FTS5 operators, escapa aspas)
- **Path traversal**: `export_html`, `generate_report`, `backup` validam output_path (deve estar dentro do dir do DB, extensoes seguras apenas)
- **VACUUM INTO escape**: aspas simples escapadas em backup (defesa em profundidade)
- **PII filter**: `get_node` redacta propriedades sensiveis (email, phone, cpf, cnpj, password, token, secret, api_key, credit_card) antes de retornar
- **Provenance**: Cada no e aresta taggeado (EXTRACTED/INFERRED/AMBIGUOUS)
- **Auditoria**: Cada operacao de escrita registrada em `audit_log` (C9 OWASP). Consultavel via `query_graph`.
- **Telemetria**: Cada chamada de tool registrada em `telemetry_spans` com latencia, args (truncados, sem PII), erros.
- **Type validation**: `delete_node` e `set_community` validam tipos de input (inteiro positivo, string length)
- **Edge upsert**: `ON CONFLICT DO UPDATE` preserva o id (nao `INSERT OR REPLACE` que deleta e recria)
- **SRI (Subresource Integrity)**: vis.js CDN carregado com hash SHA-384, bloqueia CDN comprometido
- **chmod 600 kg.db**: protege contra leitura por outro usuario do sistema
- **.gitignore**: protege kg.db, graph.json, graph.html, __pycache__ de commit acidental

## Robustez

- **SQLite WAL + busy_timeout=5000**: concorrencia entre processos sem lock contention
- **BEGIN IMMEDIATE**: evita SQLITE_BUSY_SNAPSHOT em transacoes read->write
- **wal_autocheckpoint=500**: WAL nao cresce sem bound
- **Signal handling**: SIGTERM/SIGINT -> graceful shutdown com WAL checkpoint
- **stdin EOF detection**: cliente fechou -> server sai (sem orphan process)
- **Idle timeout**: 30min sem input -> self-terminate (sem processo esquecido)
- **Circuit breaker**: 5+ falhas em 60s -> tool bloqueada por 60s (sem loop de retries)
- **isError: true**: erros de aplicacao retornam isError (LLM se recupera) em vez de JSON-RPC error
- **Catch-all Exception**: MemoryError, OSError, etc capturados sem crashar server
- **Guardas de tamanho**: betweenness skip >500 nos, louvain fallback >2000, export_html skip >5000
- **deque O(1)**: BFS com popleft() em vez de list.pop(0) O(n)
- **Louvain O(n*e)**: sigma_tot em dict O(1) em vez de scan O(n) por no
- **Backup automatico**: VACUUM INTO diario na inicializacao, rotacao 30 dias
- **Health check**: tool health_check retorna status (ok/degraded/critical), latency_p50, error_rate, integrity
- **Integrity check**: tool integrity_check verifica PRAGMA integrity_check + arestas orfas
- **73 testes**: suite completa em test_kg_infra.py (escrita, leitura, analise, seguranca, robustez, edge cases, protocolo, novas tools)

## Performance

- SQLite com WAL mode (concorrente, rapido)
- FTS5 para busca full-text (unicode61, suporta acentos)
- BFS em memoria para trace_path (carrega arestas 1x, nao N queries)
- Batch insert reusa 1 conexao (nao N conexoes)
- get_architecture: grau calculado em 2 queries agregadas O(n+e) (nao subquery O(n*e))
- Louvain: O(n*e) por iteracao, max 100 iteracoes
- Centralidade: O(n^2) para closeness, O(n^3) para betweenness (aceitavel para <500 nos)
- PageRank: power iteration, 100 iteracoes max, convergencia 1e-6
- DB de exemplo: 110KB (23 nos, 28 arestas)
- RAM: ~0 (SQLite e file-based, algoritmos carregam grafo em memoria apenas durante execucao)
- Benchmark 100 nos: todas as tools < 20ms (detect_communities 14ms, get_centrality all 19ms)
