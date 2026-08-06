# Referencia Rapida: kg-infra MCP + Skill

## O que e o que

| Componente | Tipo | Localizacao |
|---|---|---|
| kg-infra MCP server | MCP server (stdio JSON-RPC) | `/home/vsf/Projetos/kg-infra/server.py` |
| kg-infra skill | Skill (guia de uso) | `~/.config/devin/skills/kg-infra/SKILL.md` |
| Banco SQLite | Dados | `/home/vsf/Projetos/kg-infra/kg.db` (chmod 600) |
| Schema | DDL | `/home/vsf/Projetos/kg-infra/schema.sql` |
| CLI | Teste manual | `/home/vsf/Projetos/kg-infra/cli.py` |
| Export JSON | Obsidian/D3.js | `/home/vsf/Projetos/kg-infra/export.py` |
| Seed | Dados de exemplo | `/home/vsf/Projetos/kg-infra/seed.py` |
| README | Documentacao | `/home/vsf/Projetos/kg-infra/README.md` |
| Threat Model | Seguranca | `/home/vsf/Projetos/kg-infra/THREAT-MODEL.md` |
| Este arquivo | Referencia rapida | `/home/vsf/Projetos/kg-infra/REFERENCIA.md` |

## Configuracao

### Devin (`~/.config/devin/mcp_config.json`)
```json
{
  "mcpServers": {
    "kg-infra": {
      "command": "python3",
      "args": ["/home/vsf/Projetos/kg-infra/server.py"],
      "transport": "stdio"
    }
  }
}
```

### Antigravity (`~/.gemini/config/mcp_config.json`)
```json
{
  "mcpServers": {
    "kg-infra": {
      "command": "python3",
      "args": ["/home/vsf/Projetos/kg-infra/server.py"],
      "env": {
        "KG_DB_PATH": "/home/vsf/Projetos/kg-infra/kg.db"
      }
    }
  }
}
```

### Skill (`~/.config/devin/skills/kg-infra/SKILL.md`)
Auto-descoberta pelo Devin. Nao precisa de configuracao adicional.

## 36 tools MCP em 1 pagina

### Escrita (7)
```
add_node          Criar entidade          {label, name, properties?, provenance?, source?}
upsert_node       Criar ou atualizar      {label, name, qualified_name?, properties?}
add_edge          Criar relacao           {source, target, type, provenance?, weight?}
add_nodes_batch   Bulk criar nos          {nodes: [{label, name, ...}]}
add_edges_batch   Bulk criar arestas      {edges: [{source, target, type, ...}]}
delete_node       Remover entidade        {id ou qualified_name}
set_community     Atribuir comunidade     {node_id, community_id, algorithm?}
```

### Leitura (8)
```
list_projects     Estatisticas gerais     {}
get_graph_schema  Labels e tipos validos  {}
search_graph      Buscar por nome/label   {name_pattern?, label?, limit?}
get_node          Detalhes + vizinhos     {id ou qualified_name}
trace_path        Caminho mais curto      {source, target, max_hops?}
get_architecture  Visao geral             {}
query_graph       SQL read-only           {query, limit?}
export_json       Exportar JSON           {}
```

### Analise (9)
```
export_html         Visualizacao vis.js    {output_path?}
detect_communities  Louvain ou CC          {algorithm?, resolution?}
get_centrality      PageRank etc           {metric?, limit?}
get_telemetry       Latencia e erros       {window?, limit?}
generate_report     GRAPH_REPORT.md        {output_path?}
export_all          Todos artefatos        {output_dir?}
health_check        Status do server       {}
integrity_check     Integridade do banco   {}
backup              Backup via VACUUM INTO {output_path?}
```

### Analise avancada (7)
```
get_impact          Blast radius de um no          {node, max_depth?, direction?}
trace_paths         Multiplos caminhos A->B        {source, target, max_paths?, max_hops?}
explain_node        Subgrafo + contexto de um no   {node, depth?, limit_neighbors?}
what_if_remove      Simula remocao de um no        {node}
replay_trace        Reconstrui fluxo de execucao   {trace_id, limit?}
get_impact_summary  Resume impacto de edge_type    {edge_type, limit?}
find_orphans        Nos isolados e arestas AMBIG.  {limit?}
```

### Tools de codigo (5)
```
scan_codebase       Mapear codigo Python (ast)     {path, max_files?, exclude?}
get_call_graph      Grafo de chamadas              {project?, direction?, limit?}
get_import_graph    Grafo de imports               {project?, limit?}
find_circular_imports  Imports circulares (DFS)    {project?, max_depth?}
get_code_impact     Blast radius de funcao         {function, max_depth?}
```

#### Detalhe das tools de analise avancada

**get_impact** - Blast radius de um no (nos afetados agrupados por distancia).
- Args: `node` (id ou qualified_name, obrigatorio), `max_depth` (default 3, max 5), `direction` (outgoing/incoming/both, default both)
- Retorna: nos afetados agrupados por distancia (depth 1, 2, 3, ...)
- Exemplo: `get_impact {"node": "customer:acme-corp", "max_depth": 3, "direction": "both"}`

**trace_paths** - Multiplos caminhos entre dois nos.
- Args: `source`, `target` (obrigatorios), `max_paths` (default 3, max 10), `max_hops` (default 8, max 15)
- Retorna: lista de caminhos (cada caminho e uma lista de nos)
- Exemplo: `trace_paths {"source": "customer:acme-corp", "target": "article:guia-de-migracao", "max_paths": 5}`

**explain_node** - Subgrafo ao redor de um no com contexto.
- Args: `node` (obrigatorio), `depth` (default 1, max 2), `limit_neighbors` (default 20, max 50)
- Retorna: no, vizinhos, arestas e summary
- Exemplo: `explain_node {"node": "product:plano-enterprise", "depth": 1, "limit_neighbors": 20}`

**what_if_remove** - Simula remocao de um no.
- Args: `node` (obrigatorio)
- Retorna: edges_lost, nodes_isolated, communities_affected, isolation_risk
- Exemplo: `what_if_remove {"node": "customer:acme-corp"}`

**replay_trace** - Reconstrui fluxo de execucao de um trace.
- Args: `trace_id` (obrigatorio), `limit` (default 100, max 500)
- Retorna: spans ordenados, duracao total, call_tree
- Exemplo: `replay_trace {"trace_id": "trace-001", "limit": 100}`

**get_impact_summary** - Resume impacto de um tipo de aresta.
- Args: `edge_type` (obrigatorio), `limit` (default 20)
- Retorna: total_edges, source_labels, target_labels, affected_nodes
- Exemplo: `get_impact_summary {"edge_type": "bought", "limit": 20}`

**find_orphans** - Encontra nos isolados e arestas AMBIGUOUS.
- Args: `limit` (default 50, max 200)
- Retorna: orphan_nodes, ambiguous_edges
- Exemplo: `find_orphans {"limit": 50}`

#### Detalhe das tools de codigo

**scan_codebase** - Mapeia diretorio de codigo Python via ast module (zero deps, sem LLM, sem tree-sitter). Extrai functions, classes, imports, calls e adiciona como nos/arestas no grafo.
- Args: `path` (diretorio raiz, obrigatorio), `max_files` (default 200, max 1000), `exclude` (lista de dirs para ignorar)
- Retorna: {project, project_id, stats: {files, functions, classes, imports, calls, errors}, files_scanned}
- Exemplo: `scan_codebase {"path": "/home/vsf/Projetos/meu-app", "max_files": 500, "exclude": ["venv", "__pycache__"]}`

**get_call_graph** - Grafo de chamadas: quem chama quem.
- Args: `project` (qualified_name do projeto, opcional), `direction` (outgoing/incoming/both, default both), `limit` (default 100, max 500)
- Retorna: {outgoing: [{name, calls: [...]}], incoming: [{name, called_by: [...]}], total_outgoing, total_incoming}
- Exemplo: `get_call_graph {"direction": "incoming", "limit": 50}`

**get_import_graph** - Grafo de imports: quais arquivos importam quais modulos.
- Args: `project` (opcional), `limit` (default 100, max 500)
- Retorna: {files: [{file, imports: [{module, names}]}], total_imports}
- Exemplo: `get_import_graph {"limit": 200}`

**find_circular_imports** - Detecta imports circulares via DFS no grafo de imports.
- Args: `project` (opcional), `max_depth` (default 10, max 20)
- Retorna: {cycles: [[file_info]], cycle_count}
- Exemplo: `find_circular_imports {"max_depth": 15}`

**get_code_impact** - Blast radius de uma funcao: se mudar esta funcao, quais outras sao afetadas. Faz BFS no grafo de CALLS_FUNC.
- Args: `function` (id ou qualified_name, obrigatorio), `max_depth` (default 3, max 5)
- Retorna: {function: {id, name}, affected_count, by_depth}
- Exemplo: `get_code_impact {"function": "func:meu-app:server:handle_request", "max_depth": 4}`

## Padroes de uso

### Popular o grafo a partir de um documento
```
1. get_graph_schema {}                           -> descobrir labels/tipos validos
2. add_nodes_batch {nodes: [..., provenance: "EXTRACTED"]}  -> criar entidades
3. add_edges_batch {edges: [..., provenance: "EXTRACTED"]}  -> criar relacoes
4. get_architecture {}                           -> confirmar
```

### Responder pergunta de negocio
```
"Quem sao os clientes que compraram Plano Enterprise?"
  -> search_graph {label: "Product", name_pattern: "Enterprise"}
  -> get_node {qualified_name: "product:plano-enterprise"}
  -> olhar edges com type="bought" -> listar source nodes
```

### Analisar o grafo
```
1. get_architecture {}              -> visao geral
2. detect_communities {}            -> agrupar
3. get_centrality {metric: "all"}   -> ranking de influencia
4. generate_report {}               -> relatorio automatico
5. export_html {}                   -> visualizar no browser
```

### Debug de performance
```
1. get_telemetry {window: 60}       -> latencia p50/p90, top tools, erros
2. query_graph {query: "SELECT tool, COUNT(*), AVG(duration_ms) FROM telemetry_spans GROUP BY tool"}
```

## Labels validos (34)
```
Customer Company Contact Product Deal Ticket Issue Agent Channel
Article Topic Keyword Campaign Audience Interaction Proposal Feedback
Lead Opportunity Contract Service Department Event Document Note
Tag Person Organization
Function Class Import Variable Decorator
```

## Tipos de aresta validos (31)
```
bought complained assigned_to wrote covers mentions targets
interacts_with works_at belongs_to relates_to part_of depends_on
produced consumed reviewed requested approved rejected escalated
resolved closed opened
DEFINES_FUNC DEFINES_CLASS DECORATES INHERITS_FROM
IMPORTS_FROM CALLS_FUNC READS_VAR WRITES_VAR
```

## Provenance (3)
```
EXTRACTED   -> extraido diretamente da fonte (PDF, CSV, email)
INFERRED    -> inferido pelo LLM (relacao implicita)
AMBIGUOUS   -> incerto, precisa revisao humana
```

## Seguranca
- 100% local (LGPD-safe)
- Audit log em todas as escritas
- Telemetria com PII redacted (colunas: agent_id, cost_usd, checkpoint)
- Path traversal bloqueado em export_html/generate_report
- SQL injection bloqueado em query_graph (allowlist de tabelas)
- SRI (SHA-384) no vis.js CDN do graph.html
- chmod 600 kg.db
- .gitignore em kg.db, graph.json, graph.html, GRAPH_REPORT.md

## Resetar o banco (CUIDADO)
```bash
rm /home/vsf/Projetos/kg-infra/kg.db
python3 -c "import sqlite3; c=sqlite3.connect('/home/vsf/Projetos/kg-infra/kg.db'); c.executescript(open('/home/vsf/Projetos/kg-infra/schema.sql').read()); c.close()"
chmod 600 /home/vsf/Projetos/kg-infra/kg.db
python3 /home/vsf/Projetos/kg-infra/seed.py  # dados de exemplo
```
