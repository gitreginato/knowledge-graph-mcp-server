# AGENTS.md: Regras para Agentes de IA neste Projeto

## Contexto do projeto

Servidor MCP (Model Context Protocol) com 36 tools para knowledge graph de negocio.
Python stdlib puro, zero dependencias, 100% local (LGPD-safe). SQLite como
armazenamento, vis.js para visualizacao interativa.

## Regras absolutas

### 1. Zero dependencias externas
- O projeto usa APENAS Python stdlib. Nao adicionar numpy, pandas, networkx, etc.
- Se precisar de um algoritmo (Louvain, PageRank, betweenness), implementar em Python puro.
- Motivo: sem supply chain risk, sem conflitos de versao, maxima portabilidade.

### 2. Seguranca em SQL
- Toda query SQL deve ser parameterized (never concatenate com f-string)
- `query_graph` tool usa allowlist de tabelas, bloqueia `sqlite_master`, `INSERT`, `PRAGMA`, `UNION`
- Validacao por allowlist, nunca denylist

### 3. Path traversal defense
- `export_html` e `generate_report` validam path de saida contra traversal
- Rejeitar `/etc/cron.d/evil.html`, `../../etc/passwd`, etc.
- Rejeitar extensoes perigosas (`.sh`, `.py`, `.bash`)

### 4. LGPD por design
- 100% local, nenhum dado sai do host
- Nenhum dado pessoal deve ser logado
- SQLite com WAL mode para concorrencia segura
- Backup via `VACUUM INTO`

## Padroes de codigo

- Python 3.10+ (type hints)
- Indentacao: 4 espacos
- Linha maxima: 120 caracteres
- snake_case para funcoes/variaveis, PascalCase para classes
- Funcoes: maximo 30 linhas (ideal < 15)
- Commits em portugues, Conventional Commits

## Comandos do projeto

```bash
# Rodar servidor MCP
python3 server.py

# Popular com dados de exemplo
python3 seed.py

# Rodar testes
python3 test_kg_infra.py

# Exportar visualizacao
python3 export.py --html grafo.html
```

## Estrutura de arquivos

```
server.py          # Servidor MCP (JSON-RPC stdio), 36 tools
schema.sql         # Schema SQLite (nodes, edges, telemetry, metadata)
seed.py            # Dados de exemplo
seed_full.py       # Dataset completo para demo
export.py          # Export HTML com vis.js
test_kg_infra.py   # 73 testes (seguranca, CRUD, analise, export)
```
