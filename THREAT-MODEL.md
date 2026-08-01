# Threat Model: kg-infra (Knowledge Graph de Negocio)

**Data:** 2026-08-01 (retroativo, pos-implementacao)
**Sistema:** MCP server Python + SQLite, 100% local, populado por Devin/Antigravity, consultado por Devin/Antigravity
**Escopo:** server.py, schema.sql, kg.db, configuracao MCP em ~/.gemini/ e ~/.config/devin/

## Assets

1. **Dados de clientes (PII LGPD-sensivel)** - nome, email, industria, MRR, historico de tickets. Valor: regulatorio (LGPD), competitivo (base de clientes), financeiro (MRR).
2. **Grafo de negocio (IP)** - relacoes entre clientes, produtos, deals, tickets, campanhas. Valor: competitivo (visao 360), estrategico (cross-sell, churn prediction).
3. **Integridade do grafo** - nos/arestas nao podem ser corrompidos ou deletados sem auditoria. Valor: confianca nas queries.
4. **Disponibilidade do MCP server** - se cai, Devin/Antigravity nao consultam o grafo. Valor: produtividade.
5. **Filesystem do host** - o server roda no usuario vsf, tem acesso a ~/.local/bin/, ~/.gemini/, ~/.config/devin/. Valor: todo o sistema.

## Adversarios

1. **Atacante externo com acesso ao filesystem** (malware, acesso fisico, SSH comprometido) - motivacao: exfiltrar dados de clientes. Capacidade: ler kg.db se tiver acesso ao arquivo.
2. **LLM como vetor (prompt injection)** - conteudo ingerido pelo Antigravity (PDF de proposta, email de cliente, pagina web) pode conter instrucoes maliciosas. O Antigravity pode ser induzido a criar nos/arestas maliciosas ou exfiltrar dados via queries. Capacidade: alta (o LLM e quem popula o grafo).
3. **Insider negligente** (vsf) - deleta no errado, sobrescreve grafo, expoe kg.db em backup publico. Capacidade: total (acesso local).
4. **Atacante de supply chain** - se Python ou SQLite forem comprometidos. Capacidade: baixa (stdlib, sem deps externas).
5. **Atacante via MCP client malicioso** - se um MCP client (nao Devin/Antigravity) se conectar ao server. Capacidade: media (server nao auth).

## STRIDE

### Spoofing
- **MCP client malicioso se conecta ao server** -> O server escuta em stdio, nao em rede. So processos locais com acesso ao stdin do processo podem se conectar. Mitigacao: stdio-only, sem TCP/HTTP. Risco residual: baixo.
- **Antigravity comprometido envia comandos maliciosos** -> O server nao autentica quem chama. Qualquer MCP tool pode ser chamada. Mitigacao: allowlist de labels/edges, validacao de input, audit log. Risco residual: medio (confianca no MCP client).

### Tampering
- **SQL injection em query_graph** -> Mitigado: allowlist de tabelas (nao denylist), so SELECT, bloqueia sqlite_master/pragma, parameterized queries em todo o resto. Risco residual: muito baixo.
- **FTS injection em search_graph** -> Mitigado: sanitiza padrao FTS5 (remove regex chars, FTS5 operators AND/OR/NOT/NEAR, escapa aspas). Risco residual: baixo.
- **Path traversal em export_html/generate_report** -> Mitigado: _validate_output_path verifica que o path esta dentro do diretorio do DB, so permite extensoes seguras (.html, .json, .md, .csv, .txt). Risco residual: muito baixo.
- **Antigravity cria nos/arestas com dados maliciosos (prompt injection)** -> O LLM pode ser induzido a criar nos com labels/edges validos mas conteudo malicioso (ex: Customer "IGNORE ALL INSTRUCTIONS"). Mitigacao: allowlist de labels/edges limita o que pode ser criado, provenance tags (EXTRACTED/INFERRED) permitem distinguir, audit log registra. Risco residual: medio (nao ha validacao semantica do conteudo).
- **kg.db corrompido por falha de disco** -> Mitigado: SQLite WAL mode e resiliente, mas sem backup automatico. Risco residual: medio (sem backup). **Recomendacao: backup periodico de kg.db.**
- **Concorrencia: dois MCP clients escrevem ao mesmo tempo** -> SQLite WAL suporta concorrencia de leitura + 1 escritor. Se 2 escrevem, um espera (timeout default 5s). Risco residual: baixo.

### Repudiation
- **Operacao de escrita sem log** -> Mitigado: audit_log registra toda operacao (node_create, node_update, node_delete, edge_upsert, community_set) com entity_id, label, source, timestamp. Risco residual: baixo.
- **Audit log alterado** -> audit_log e tabela SQLite normal, pode ser deletada via query_graph? Nao: query_graph so permite SELECT. Mas via acesso direto ao arquivo kg.db, sim. Risco residual: medio (se filesystem comprometido). **Recomendacao: export periodico do audit_log para local read-only.**

### Information Disclosure
- **kg.db acessivel a outro usuario do sistema** -> Mitigado: arquivo em /home/vsf/Projetos/kg-infra/kg.db, permissoes 600 (chmod 600). Risco residual: baixo.
- **query_graph expoe metadados do SQLite** -> Mitigado: bloqueia sqlite_master, pragma, sqlite_. Risco residual: muito baixo.
- **export_json expoe todos os dados** -> Sim, por design. O server e local, o export e para Obsidian/D3.js. Risco: se o arquivo exportado for commitado em repo publico. Mitigado: .gitignore em graph.json, graph.html, GRAPH_REPORT.md. Risco residual: baixo.
- **graph.html expoe dados no browser** -> O HTML gerado por export_html contem todos os nos e arestas embutidos no JavaScript. Se aberto em browser compartilhado, dados ficam no cache. Mitigado: .gitignore em graph.html. Risco residual: baixo (uso local).
- **vis.js CDN comprometido (supply chain)** -> O HTML carrega vis.js de unpkg.com CDN. Se o CDN for comprometido, JavaScript malicioso roda no browser do usuario. Mitigado: SRI (Subresource Integrity) com hash SHA-384. O browser bloqueia se o hash nao bater. Risco residual: muito baixo.
- **Telemetria expoe PII em args_summary** -> telemetry_spans registra args das tools (truncados em 500 chars). Pode conter nomes de clientes (search_graph). Mitigado: args truncados, sem dados sensiveis explicitos (senhas, tokens). Risco residual: medio (nomes de clientes sao PII LGPD). **Recomendacao: sanitizar args_summary para remover PII antes de salvar.**
- **Erro expoe stack trace** -> Mitigado: erros sao capturados e retornados como JSON-RPC error com message (nao stack trace). Risco residual: baixo.
- **Logs de erro contem PII** -> O server nao loga para arquivo, so retorna via JSON-RPC. Risco residual: baixo.

### Denial of Service
- **Payload gigante em add_nodes_batch** -> Mitigado: limite de 10000 itens por batch, validacao de tamanho de name (512 chars) e properties (64KB). Risco residual: baixo.
- **trace_path com grafo gigante** -> Mitigado: max_hops 20, BFS em memoria (carrega todas as arestas uma vez). Para 1M arestas, usa ~50MB RAM. Risco residual: baixo para volume atual, medio se escalar para 10M+ arestas.
- **query_graph com query custosa** -> SELECT sem LIMIT pode retornar ate 10000 rows (limite default). Query complexa (cross join) pode consumir CPU. Risco residual: medio. **Recomendacao: timeout de query.**
- **MCP server travado (loop infinito)** -> Se o server trava, o MCP client (Devin/Antigravity) nao recebe resposta. Mitigacao: MCP clients tem timeout proprio. Risco residual: baixo.

### Elevation of Privilege
- **MCP tool chama funcao fora do escopo** -> Mitigado: cada tool e uma funcao explicita, nao ha eval/exec. Risco residual: muito baixo.
- **Path traversal via qualified_name** -> Mitigado: normalize_name remove caracteres nao-ASCII e nao-alfanumericos, qualified_name e usado como string em query (nao como path). Risco residual: muito baixo.
- **Acesso a arquivos fora do DB** -> O server so abre kg.db (path fixo ou env var). Nao le/escreve outros arquivos. Risco residual: muito baixo.

## Zero Trust (NIST SP 800-207)

- [x] **Toda conexao autenticada + autorizada?** PARCIAL. stdio-only (sem rede), mas sem auth entre MCP client e server. Confianca implicita no MCP client.
- [x] **Sem confianca implicita por rede?** SIM. Sem rede, stdio-only.
- [ ] **Acesso por sessao limitada?** NAO. Cada chamada MCP e stateless (abre/fecha conexao). Nao ha sessao.
- [ ] **Acesso adaptativo?** N/A. Sem auth, sem policy dinamica.
- [x] **Monitoramento de integridade?** SIM. audit_log registra toda escrita.
- [ ] **Auth/authz discretas antes de sessao?** NAO. Sem auth.
- [x] **Coleta de telemetria?** SIM. audit_log e a telemetria.

**Veredito Zero Trust:** o sistema e local-only (stdio), entao Zero Trust e parcialmente aplicavel. O principio de "nunca confie, sempre verifique" e mitigado por: allowlist de labels/edges, validacao de input, audit log, query read-only. Mas nao ha auth entre MCP client e server (confianca implicita no client).

## LLM (OWASP LLM Top 10)

- [x] **Contextos separados (system, user, tool)?** SIM. O MCP server e uma tool, nao um prompt. O LLM (Antigravity/Devin) separa system prompt de tool output.
- [x] **Ferramentas com least privilege?** SIM. O server so tem 15 tools explicitas, sem exec/eval. Allowlist de labels/edges limita o que pode ser criado.
- [ ] **Human-in-the-loop para acoes destrutivas?** NAO. delete_node nao pede confirmacao. **Recomendacao: adicionar confirmacao no MCP client (Devin/Antigravity) antes de chamar delete_node.**
- [ ] **Output filtering?** PARCIAL. O server retorna JSON, nao texto livre. Mas o LLM pode interpretar o JSON e agir. Nao ha guardrail de saida.
- [x] **Conteudo ingerido tratado como data?** SIM. O server recebe dados via add_node/add_edge, nao interpreta como instrucao. O LLM (Antigravity) e quem decide o que ingerir.
- [ ] **RAG poisoning mitigado?** PARCIAL. O grafo e o "RAG" do sistema. Se o Antigrativity ingerir PDF malicioso, pode criar nos/arestas com dados errados. Mitigacao: provenance tags (EXTRACTED vs INFERRED) permitem distinguir, mas nao ha validacao semantica. **Recomendacao: revisar nos com provenance INFERRED antes de confiar.**

## Prioridade de mitigacao

1. **ALTO: sanitizar args_summary na telemetria** - remover PII (nomes de clientes, emails) antes de salvar em telemetry_spans. Implementar funcao _redact_pii().
2. **MEDIO: backup periodico de kg.db** - protege contra corrupcao/falha de disco. Script cron.
3. **MEDIO: confirmacao no MCP client para delete_node** - evita delecao acidental. Config do Devin/Antigravity.
4. **MEDIO: export periodico do audit_log para read-only** - protege contra tampering do log. Script cron.
5. **BAIXO: timeout de query em query_graph** - evita DoS por query custosa. SQLite busy_timeout.
6. **BAIXO: validacao semantica de nos INFERRED** - revisar nos criados por LLM antes de confiar. Processo humano.
7. **BAIXO: CSP no graph.html** - adicionar Content-Security-Policy header no HTML gerado para bloquear scripts nao-vis.js.

## O que nao foi modelado

- **Ameacas ao Antigravity/Devin propriamente** - fora do escopo (sao ferramentas de terceiros).
- **Ameacas de rede** - o server e stdio-only, sem rede.
- **Ameacas ao filesystem do host** - assumido que o usuario (vsf) tem controle do seu home dir.
- **Ameacas de supply chain do Python/SQLite** - stdlib, sem deps externas. Coberto por /supply-chain se necessario.
- **LGPD compliance detalhado** - data residency (local, OK), consentimento, direito ao esquecimento (delete_node, OK), DPO (fora do escopo).

## Conclusao

O sistema e **razoavelmente seguro** para uso local single-user. As mitigacoes principais (allowlist, parameterized queries, audit log, FTS sanitization, query read-only) cobrem os vetores mais provaveis. Os riscos residuais sao:

1. **Confianca implicita no MCP client** (sem auth) - aceitavel para local single-user.
2. **Prompt injection via conteudo ingerido pelo Antigravity** - mitigado por provenance tags, mas nao ha validacao semantica.
3. **Sem backup automatico** - risco de perda de dados.
4. **Sem chmod 600 no kg.db** - risco de leitura por outro usuario.

As recomendacoes de prioridade ALTA (chmod 600, .gitignore) devem ser aplicadas imediatamente.
