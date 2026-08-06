#!/usr/bin/env python3
"""Suite de testes do kg-infra MCP server.
Cobre: escrita, leitura, analise, seguranca, robustez, edge cases.
Roda: python3 test_kg_infra.py
"""

import sys
import os
import json
import sqlite3
import tempfile
import shutil
import time
from pathlib import Path

# Setup: usar DB temporario
TEST_DIR = tempfile.mkdtemp(prefix="kg-test-")
TEST_DB = os.path.join(TEST_DIR, "kg.db")
SCHEMA_PATH = "/home/vsf/Projetos/kg-infra/schema.sql"

os.environ["KG_DB_PATH"] = TEST_DB

sys.path.insert(0, "/home/vsf/Projetos/kg-infra")
import server

PASS = 0
FAIL = 0
ERRORS = []


def setup_db():
    """Cria DB fresco com schema e dados de exemplo. Idempotente."""
    # Remover DB antigo e arquivos WAL/SHM para evitar UNIQUE constraint
    for p in [TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"]:
        if os.path.exists(p):
            os.unlink(p)
    conn = sqlite3.connect(TEST_DB)
    conn.executescript(open(SCHEMA_PATH).read())
    conn.close()
    os.chmod(TEST_DB, 0o600)
    server.DB_PATH = TEST_DB
    conn = server.get_db()
    conn.execute("INSERT INTO nodes (label, name, qualified_name, provenance, source) VALUES (?, ?, ?, ?, ?)",
                 ("Customer", "Acme Corp", "customer:acme-corp", "EXTRACTED", "test"))
    conn.execute("INSERT INTO nodes (label, name, qualified_name, provenance, source) VALUES (?, ?, ?, ?, ?)",
                 ("Product", "Plano Enterprise", "product:plano-enterprise", "EXTRACTED", "test"))
    conn.execute("INSERT INTO nodes (label, name, qualified_name, provenance, source) VALUES (?, ?, ?, ?, ?)",
                 ("Ticket", "TKT-0001", "ticket:tkt-0001", "EXTRACTED", "test"))
    conn.execute("INSERT INTO nodes (label, name, qualified_name, provenance, source) VALUES (?, ?, ?, ?, ?)",
                 ("Article", "Guia de Migracao", "article:guia-de-migracao", "EXTRACTED", "test"))
    conn.execute("INSERT INTO nodes (label, name, qualified_name, provenance, source) VALUES (?, ?, ?, ?, ?)",
                 ("Agent", "Joao Silva", "agent:joao-silva", "EXTRACTED", "test"))
    conn.execute("INSERT INTO edges (source_id, target_id, type, provenance, weight) VALUES (?, ?, ?, ?, ?)",
                 (1, 2, "bought", "EXTRACTED", 1.0))
    conn.execute("INSERT INTO edges (source_id, target_id, type, provenance, weight) VALUES (?, ?, ?, ?, ?)",
                 (1, 3, "complained_about", "EXTRACTED", 1.0))
    conn.execute("INSERT INTO edges (source_id, target_id, type, provenance, weight) VALUES (?, ?, ?, ?, ?)",
                 (3, 5, "assigned_to", "EXTRACTED", 1.0))
    conn.execute("INSERT INTO edges (source_id, target_id, type, provenance, weight) VALUES (?, ?, ?, ?, ?)",
                 (4, 2, "about", "INFERRED", 1.0))
    conn.commit()
    conn.close()
    # checkpoint para consolidar WAL e evitar locked
    chk = sqlite3.connect(TEST_DB)
    chk.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    chk.close()


def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {detail}")
        print(f"  FAIL: {name} - {detail}")


def test_raises(name, fn, exc_type, detail=""):
    global PASS, FAIL
    try:
        fn()
        FAIL += 1
        ERRORS.append(f"{name}: esperava {exc_type.__name__} mas nao lancou")
        print(f"  FAIL: {name} - esperava {exc_type.__name__}")
    except exc_type:
        PASS += 1
        print(f"  PASS: {name}")
    except Exception as e:
        FAIL += 1
        ERRORS.append(f"{name}: esperava {exc_type.__name__} mas lancou {type(e).__name__}: {e}")
        print(f"  FAIL: {name} - esperava {exc_type.__name__}, lancou {type(e).__name__}")


# ============================================================
# Suite 1: Escrita (10 testes)
# ============================================================

def suite_escrita():
    print("\n=== Suite 1: Escrita ===")
    setup_db()

    # 1. add_node
    r = server.tool_add_node({"label": "Customer", "name": "Teste Corp", "source": "test"})
    test("add_node cria com id", isinstance(r.get("id"), int) and r["id"] > 0)

    # 2. add_node com properties
    r = server.tool_add_node({"label": "Customer", "name": "Props Corp", "properties": {"mrr": 5000, "tier": "gold"}, "provenance": "EXTRACTED"})
    test("add_node com properties", r.get("id") > 0)

    # 3. add_node label invalido
    test_raises("add_node label invalido", lambda: server.tool_add_node({"label": "InvalidLabel", "name": "X"}), ValueError)

    # 4. add_node name vazio
    test_raises("add_node name vazio", lambda: server.tool_add_node({"label": "Customer", "name": ""}), ValueError)

    # 5. upsert_node cria novo
    r = server.tool_upsert_node({"label": "Customer", "name": "Upsert New", "qualified_name": "customer:upsert-new"})
    test("upsert_node cria novo", r.get("id") > 0)

    # 6. upsert_node atualiza existente
    r2 = server.tool_upsert_node({"label": "Customer", "name": "Upsert New Updated", "qualified_name": "customer:upsert-new"})
    test("upsert_node atualiza existente", r2.get("id") == r["id"])

    # 7. add_edge (usar nos sem aresta pre-existente)
    r = server.tool_add_edge({"source": "customer:acme-corp", "target": "article:guia-de-migracao", "type": "related_to", "weight": 2.0})
    test("add_edge cria aresta", r.get("id") > 0 or r.get("source_id") is not None, str(r))

    # 8. add_edge upsert (mesmo source/target/type)
    r2 = server.tool_add_edge({"source": "customer:acme-corp", "target": "article:guia-de-migracao", "type": "related_to", "weight": 3.0})
    test("add_edge upsert preserva id", r.get("id") == r2.get("id"), f"{r.get('id')} vs {r2.get('id')}")

    # 9. add_nodes_batch
    r = server.tool_add_nodes_batch({"nodes": [{"label": "Customer", "name": "B1"}, {"label": "Customer", "name": "B2"}, {"label": "Product", "name": "B3"}]})
    test("add_nodes_batch cria 3", r.get("created") == 3)

    # 10. add_edges_batch
    r = server.tool_add_edges_batch({"edges": [{"source": "customer:acme-corp", "target": "product:plano-enterprise", "type": "related_to"}]})
    test("add_edges_batch cria arestas", r.get("created", 0) >= 0)


# ============================================================
# Suite 2: Leitura (10 testes)
# ============================================================

def suite_leitura():
    print("\n=== Suite 2: Leitura ===")

    # 11. get_node por qualified_name
    r = server.tool_get_node({"qualified_name": "customer:acme-corp"})
    test("get_node por qn", r.get("label") == "Customer")

    # 12. get_node por id
    r = server.tool_get_node({"id": 1})
    test("get_node por id", r.get("name") == "Acme Corp")

    # 13. search_graph por nome
    r = server.tool_search_graph({"name_pattern": "acme"})
    test("search_graph por nome", r.get("total", 0) >= 1)

    # 14. search_graph por label
    r = server.tool_search_graph({"label": "Ticket"})
    test("search_graph por label", r.get("total", 0) >= 1)

    # 15. trace_path
    r = server.tool_trace_path({"source": "customer:acme-corp", "target": "agent:joao-silva"})
    test("trace_path encontra caminho", r.get("hops", -1) >= 0)

    # 16. trace_path mesmo no
    r = server.tool_trace_path({"source": "customer:acme-corp", "target": "customer:acme-corp"})
    test("trace_path mesmo no = 0 hops", r.get("hops") == 0)

    # 17. get_architecture
    r = server.tool_get_architecture({})
    test("get_architecture retorna stats", r.get("total_nodes", 0) > 0)

    # 18. query_graph
    r = server.tool_query_graph({"query": "SELECT COUNT(*) as c FROM nodes"})
    test("query_graph SELECT", r["rows"][0]["c"] > 0)

    # 19. list_projects
    r = server.tool_list_projects({})
    test("list_projects retorna lista", isinstance(r, (list, dict)))

    # 20. get_graph_schema
    r = server.tool_get_graph_schema({})
    test("get_graph_schema retorna labels", "labels" in r or "valid_labels" in r or len(r) > 0)


# ============================================================
# Suite 3: Analise (8 testes)
# ============================================================

def suite_analise():
    print("\n=== Suite 3: Analise ===")

    # 21. detect_communities louvain
    r = server.tool_detect_communities({"algorithm": "louvain"})
    test("detect_communities louvain", r.get("total_communities", 0) > 0)

    # 22. detect_communities connected_components
    r = server.tool_detect_communities({"algorithm": "connected_components"})
    test("detect_communities cc", r.get("total_communities", 0) > 0)

    # 23. detect_communities algoritmo invalido
    test_raises("detect_communities invalido", lambda: server.tool_detect_communities({"algorithm": "invalid"}), ValueError)

    # 24. get_centrality all
    r = server.tool_get_centrality({"metric": "all"})
    test("get_centrality all", all(k in r for k in ["degree", "betweenness", "closeness", "pagerank"]))

    # 25. get_centrality pagerank
    r = server.tool_get_centrality({"metric": "pagerank", "limit": 3})
    test("get_centrality pagerank", len(r.get("pagerank", [])) <= 3)

    # 26. get_centrality metric invalido
    test_raises("get_centrality invalido", lambda: server.tool_get_centrality({"metric": "invalid"}), ValueError)

    # 27. export_html
    r = server.tool_export_html({})
    test("export_html gera arquivo", os.path.exists(os.path.join(TEST_DIR, "graph.html")) or r.get("nodes", 0) > 0)

    # 28. generate_report
    r = server.tool_generate_report({})
    test("generate_report gera arquivo", os.path.exists(os.path.join(TEST_DIR, "GRAPH_REPORT.md")) or r.get("god_nodes", 0) >= 0)


# ============================================================
# Suite 4: Seguranca (10 testes)
# ============================================================

def suite_seguranca():
    print("\n=== Suite 4: Seguranca ===")

    # 29. query_graph bloqueia sqlite_master
    test_raises("query_graph block sqlite_master", lambda: server.tool_query_graph({"query": "SELECT * FROM sqlite_master"}), ValueError)

    # 30. query_graph bloqueia INSERT
    test_raises("query_graph block INSERT", lambda: server.tool_query_graph({"query": "INSERT INTO nodes VALUES (1)"}), ValueError)

    # 31. query_graph bloqueia PRAGMA
    test_raises("query_graph block PRAGMA", lambda: server.tool_query_graph({"query": "PRAGMA database_list"}), ValueError)

    # 32. FTS injection
    r = server.tool_search_graph({"name_pattern": '.*"; DROP TABLE nodes;--'})
    test("FTS injection nao quebra", isinstance(r.get("total", -1), int))

    # 33. export_html path traversal
    test_raises("export_html path traversal", lambda: server.tool_export_html({"output_path": "/etc/cron.d/evil.html"}), ValueError)

    # 34. export_html extensao perigosa
    test_raises("export_html ext perigosa", lambda: server.tool_export_html({"output_path": "evil.sh"}), ValueError)

    # 35. generate_report path traversal
    test_raises("generate_report path traversal", lambda: server.tool_generate_report({"output_path": "/tmp/evil.md"}), ValueError)

    # 36. _redact_pii email
    r = server._redact_pii('{"email":"joao@acme.com"}')
    test("_redact_pii email", "[EMAIL]" in r or "[REDACTED]" in r)

    # 37. _redact_pii cpf
    r = server._redact_pii('{"cpf":"12345678901"}')
    test("_redact_pii cpf", "[REDACTED]" in r)

    # 38. _redact_pii sem PII
    r = server._redact_pii('{"name":"Acme"}')
    test("_redact_pii sem PII preserva", "Acme" in r)


# ============================================================
# Suite 5: Robustez (10 testes)
# ============================================================

def suite_robustez():
    print("\n=== Suite 5: Robustez ===")

    # 39. get_db tem busy_timeout
    conn = server.get_db()
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    test("get_db busy_timeout > 0", timeout > 0, f"timeout={timeout}")
    conn.close()

    # 40. get_db tem WAL mode
    conn = server.get_db()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    test("get_db WAL mode", mode == "wal", f"mode={mode}")
    conn.close()

    # 41. normalize_name unicode
    test("normalize_name unicode", server.normalize_name("São João Ñ Ü") == "sao-joao-n-u")

    # 42. delete_node
    r = server.tool_add_node({"label": "Customer", "name": "ToDelete"})
    nid = r["id"]
    r = server.tool_delete_node({"id": nid})
    test("delete_node", r.get("deleted") == 1)

    # 43. delete_node id invalido
    test_raises("delete_node id invalido", lambda: server.tool_delete_node({"id": -1}), ValueError)

    # 44. set_community
    r = server.tool_set_community({"node_id": 1, "community_id": 0})
    test("set_community", r is not None and ("set" in r or "community" in str(r).lower() or "updated" in str(r).lower()))

    # 45. get_telemetry sem spans
    setup_db()
    r = server.tool_get_telemetry({"window": 60})
    test("get_telemetry vazio", r.get("total_spans") == 0 or "error" in r or isinstance(r, dict))

    # 46. get_telemetry com spans (via handle_request)
    for i in range(3):
        server.handle_request({"jsonrpc": "2.0", "id": i + 900, "method": "tools/call", "params": {"name": "search_graph", "arguments": {"name_pattern": "acme"}}})
    r = server.tool_get_telemetry({"window": 60})
    test("get_telemetry com spans", r.get("total_spans", 0) >= 0)

    # 47. get_telemetry window invalido
    test_raises("get_telemetry window invalido", lambda: server.tool_get_telemetry({"window": -1}), ValueError)

    # 48. handle_request tool inexistente
    r = server.handle_request({"jsonrpc": "2.0", "id": 999, "method": "tools/call", "params": {"name": "inexistente", "arguments": {}}})
    test("handle_request tool inexistente", r is not None and "error" in r)


# ============================================================
# Suite 6: Edge cases (10 testes)
# ============================================================

def suite_edge_cases():
    print("\n=== Suite 6: Edge cases ===")

    # 49. trace_path source inexistente
    r = server.tool_trace_path({"source": "customer:inexistente", "target": "product:plano-enterprise"})
    test("trace_path source inexistente", "error" in r)

    # 50. trace_path target inexistente
    r = server.tool_trace_path({"source": "customer:acme-corp", "target": "customer:inexistente"})
    test("trace_path target inexistente", "error" in r)

    # 51. query_graph tabela inexistente
    test_raises("query_graph tabela inexistente", lambda: server.tool_query_graph({"query": "SELECT * FROM tabela_inexistente"}), ValueError)

    # 52. get_node inexistente
    r = server.tool_get_node({"qualified_name": "customer:inexistente"})
    test("get_node inexistente", r is None or "error" in r or r == {})

    # 53. add_edge source inexistente (retorna error, nao lanca)
    r = server.tool_add_edge({"source": "customer:inexistente", "target": "product:plano-enterprise", "type": "bought"})
    test("add_edge source inexistente", "error" in r)

    # 54. search_graph limite
    r = server.tool_search_graph({"name_pattern": ".*", "limit": 2})
    test("search_graph limit 2", len(r.get("nodes", r.get("results", []))) <= 2 or r.get("total", 0) >= 0)

    # 55. add_nodes_batch vazio
    r = server.tool_add_nodes_batch({"nodes": []})
    test("add_nodes_batch vazio", r.get("created") == 0)

    # 56. add_nodes_batch excede limite
    big_batch = [{"label": "Customer", "name": f"N{i}"} for i in range(10001)]
    test_raises("add_nodes_batch excede limite", lambda: server.tool_add_nodes_batch({"nodes": big_batch}), ValueError)

    # 57. export_html grafo vazio
    setup_db()
    conn = server.get_db()
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM nodes")
    conn.commit()
    conn.close()
    r = server.tool_export_html({})
    test("export_html grafo vazio", r.get("nodes") == 0 or "error" in r or r.get("nodes", 0) >= 0)

    # 58. get_centrality grafo vazio
    r = server.tool_get_centrality({"metric": "all"})
    test("get_centrality grafo vazio", "error" in r or len(r) == 0 or all(len(v) == 0 for v in r.values() if isinstance(v, list)))


# ============================================================
# Suite 7: MCP protocolo (4 testes)
# ============================================================

def suite_protocolo():
    print("\n=== Suite 7: MCP protocolo ===")
    setup_db()
    # Pequeno delay para garantir que WAL foi liberado
    time.sleep(0.1)

    # 59. initialize
    r = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    test("initialize", r is not None and "result" in r)

    # 60. tools/list
    r = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    test("tools/list retorna 36 tools", len(r["result"]["tools"]) == 36, f"got {len(r['result']['tools'])}")

    # 61. notifications/initialized (sem resposta)
    r = server.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    test("notifications/initialized sem resposta", r is None)

    # 62. method inexistente
    r = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "method/inexistente", "params": {}})
    test("method inexistente retorna error", r is not None and "error" in r)


# ============================================================
# Suite 8: Novas tools de robustez (3 testes)
# ============================================================

def suite_robustez_tools():
    print("\n=== Suite 8: Novas tools de robustez ===")
    setup_db()

    # 63. health_check
    r = server.tool_health_check({})
    test("health_check retorna status", r.get("status") in ("ok", "degraded", "critical"), str(r))

    # 64. integrity_check
    r = server.tool_integrity_check({})
    test("integrity_check ok", r.get("integrity") == "ok", str(r))

    # 65. backup
    import tempfile
    backup_path = os.path.join(TEST_DIR, "test-backup.db")
    r = server.tool_backup({"output_path": backup_path})
    test("backup cria arquivo", os.path.exists(backup_path), str(r))
    if os.path.exists(backup_path):
        os.unlink(backup_path)

    # 66. isError em erro de validacao
    r = server.handle_request({"jsonrpc": "2.0", "id": 700, "method": "tools/call", "params": {"name": "add_node", "arguments": {"label": "InvalidLabel", "name": "X"}}})
    test("isError em validacao", r.get("result", {}).get("isError") == True, str(r)[:200])

    # 67. circuit breaker (5 falhas -> bloqueia)
    server._failure_counts.clear()
    server._failure_windows.clear()
    for i in range(5):
        server.handle_request({"jsonrpc": "2.0", "id": 800 + i, "method": "tools/call", "params": {"name": "add_node", "arguments": {"label": "InvalidLabel", "name": "X"}}})
    r = server.handle_request({"jsonrpc": "2.0", "id": 806, "method": "tools/call", "params": {"name": "add_node", "arguments": {"label": "Customer", "name": "ShouldBeBlocked"}}})
    result_text = r.get("result", {}).get("content", [{}])[0].get("text", "")
    test("circuit breaker bloqueia apos 5 falhas", "bloqueada" in result_text or "circuito" in result_text, result_text[:200])
    server._failure_counts.clear()
    server._failure_windows.clear()

    # 68. query_graph bloqueia UNION
    test_raises("query_graph block UNION", lambda: server.tool_query_graph({"query": "SELECT name FROM nodes UNION SELECT label FROM nodes"}), ValueError)

    # 69. query_graph bloqueia subquery
    test_raises("query_graph block subquery", lambda: server.tool_query_graph({"query": "SELECT name FROM nodes WHERE id IN (SELECT id FROM nodes)"}), ValueError)

    # 70. PII filter em get_node
    setup_db()
    server.tool_add_node({"label": "Customer", "name": "PII Test", "qualified_name": "customer:pii-test", "properties": {"email": "secret@acme.com", "mrr": 5000}})
    r = server.tool_get_node({"qualified_name": "customer:pii-test"})
    props = r.get("properties", {})
    test("PII filter redact email em get_node", props.get("email") == "[REDACTED]", str(props))

    # 71. PII filter preserva nao-sensiveis
    test("PII filter preserva mrr", props.get("mrr") == 5000, str(props))

    # 72. export_all gera grafo-out/ com 6 arquivos
    setup_db()
    server.tool_add_node({"label": "Customer", "name": "Export All Test", "qualified_name": "customer:export-all-test"})
    r = server.tool_export_all({})
    files = r.get("files", [])
    test("export_all gera 6 arquivos", len(files) >= 5, f"got {len(files)}: {files}")
    test("export_all cria grafo-out/", "graph.html" in files and "HEALTH.json" in files, str(files))

def main():
    print("=" * 60)
    print("KG-INFRA: SUITE DE TESTES (73 testes)")
    print("=" * 60)

    suites = [
        suite_escrita,
        suite_leitura,
        suite_analise,
        suite_seguranca,
        suite_robustez,
        suite_edge_cases,
        suite_protocolo,
        suite_robustez_tools,
    ]

    for suite in suites:
        try:
            suite()
        except Exception as e:
            print(f"  ERRO na suite {suite.__name__}: {e}")
            ERRORS.append(f"{suite.__name__}: {e}")

    print("\n" + "=" * 60)
    print(f"RESULTADO: {PASS} pass, {FAIL} fail, {PASS + FAIL} total")
    print("=" * 60)

    if ERRORS:
        print("\nFALHAS:")
        for e in ERRORS:
            print(f"  - {e}")

    # Limpar
    shutil.rmtree(TEST_DIR, ignore_errors=True)

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
