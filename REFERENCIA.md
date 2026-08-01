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

## 24 tools MCP em 1 pagina

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

## Labels validos (29)
```
Customer Company Contact Product Deal Ticket Issue Agent Channel
Article Topic Keyword Campaign Audience Interaction Proposal Feedback
Lead Opportunity Contract Service Department Event Document Note
Tag Person Organization
```

## Tipos de aresta validos (23)
```
bought complained assigned_to wrote covers mentions targets
interacts_with works_at belongs_to relates_to part_of depends_on
produced consumed reviewed requested approved rejected escalated
resolved closed opened
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
- Telemetria com PII redacted
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
