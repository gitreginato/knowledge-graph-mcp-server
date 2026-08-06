# Copilot Instructions: kg-infra

## Visao geral
MCP server de knowledge graph em Python (stdlib only, zero deps externas).
Expoe 36 tools para gerenciar grafo de negocio + codigo em SQLite.

## Stack
- Python 3.10+ (stdlib only, sem dependencias externas)
- SQLite com WAL, PRAGMAs otimizados, composite indexes
- MCP protocol (JSON-RPC over stdio)
- Testes: pytest, 73 testes em test_kg_infra.py

## Convencoes
- Todo input externo validado com allowlist
- SQL sempre parameterized (never concatenate)
- Path traversal protection em todo file operation
- PII filter antes de logar
- Circuit breaker em operacoes que podem falhar
- Commits em portugues, Conventional Commits

## NAO faca
- Nao adicione dependencias externas
- Nao use f-string em SQL
- Nao logue dados sensiveis
- Nao remova testes existentes
- Nao modifique hooks de seguranca
