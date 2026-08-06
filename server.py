#!/usr/bin/env python3
"""kg-infra MCP Server - Knowledge Graph de negocio sobre SQLite.
Python puro, zero dependencias. JSON-RPC 2.0 over stdio.
Ferramentas de escrita (para Antigravity popular) e leitura (para Devin/Antigravity consultar).
"""

import json
import math
import os
import re
import signal
from html import escape as _html_escape
import sqlite3
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

DB_PATH = os.environ.get(
    "KG_DB_PATH",
    str(Path.home() / "Projetos" / "kg-infra" / "kg.db"),
)

# Allowlist de labels validos (defensiva, nao denylist)
VALID_LABELS = {
    # Negocio (original)
    "Customer", "Company", "Contact", "Product", "Deal", "Ticket",
    "Issue", "Agent", "Channel", "Article", "Topic", "Keyword",
    "Campaign", "Audience", "Interaction", "Proposal", "Feedback",
    "Lead", "Opportunity", "Contract", "Service", "Department",
    "Event", "Document", "Note", "Tag", "Person", "Organization",
    # Infraestrutura/codigo (expandido para mapear codebase real)
    "Project", "Module", "Config", "Folder", "File",
    # AST / parsing de codigo (como graphify, mas com ast module do Python)
    "Function", "Class", "Import", "Variable", "Decorator",
}

# Allowlist de tipos de aresta
VALID_EDGE_TYPES = {
    # Negocio (original)
    "works_at", "bought", "interested_in", "contacted_via", "opened_ticket",
    "complained_about", "resolved_by", "mentioned_in", "about", "targets",
    "published_in", "links_to", "proposed_to", "signed", "renewed", "churned",
    "referred_by", "manages", "belongs_to", "part_of", "related_to",
    "converted_to", "assigned_to", "escalated_to", "responded_to", "follows_up",
    "authored", "reviewed", "approved", "attended", "registered_for",
    # Infraestrutura/codigo (expandido)
    "CONTAINS", "USES", "DOCUMENTS", "IMPLEMENTS", "TESTS", "DEFINES",
    "EXPOSES_MCP", "CONFIGURES", "RUNS", "MONITORS", "INTEGRATES_WITH",
    "DEPENDS_ON", "IMPORTS", "CALLS", "HAS_MODULE",
    # AST / parsing de codigo
    "DEFINES_FUNC", "DEFINES_CLASS", "DECORATES", "INHERITS_FROM",
    "IMPORTS_FROM", "CALLS_FUNC", "READS_VAR", "WRITES_VAR",
}

VALID_PROVENANCE = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}

# Configuracoes de robustez
TOOL_TIMEOUT_MS = 30000  # 30s max por tool call
IDLE_TIMEOUT_S = 1800  # 30min sem input -> self-terminate
MAX_GRAPH_NODES_FOR_ALGO = 500  # betweenness/closeness skip acima disso
MAX_GRAPH_NODES_FOR_LOUVAIN = 2000  # louvain fallback para CC acima disso
MAX_GRAPH_NODES_FOR_HTML = 10000  # export_html skip acima disso
MAX_BATCH_ITEMS = 10000  # max itens por batch
MAX_BATCH_MEM_BYTES = 50 * 1024 * 1024  # 50MB max por batch
BACKUP_RETENTION_DAYS = 30  # backups com mais de 30 dias sao removidos


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA wal_autocheckpoint=500")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA mmap_size=268435456")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def audit_log(conn, event, entity_type, entity_id=None, label=None, qualified_name=None, source=None):
    """Registra evento de escrita no audit log (C9: Security Logging)."""
    conn.execute(
        "INSERT INTO audit_log (event, entity_type, entity_id, label, qualified_name, source) VALUES (?, ?, ?, ?, ?, ?)",
        (event, entity_type, entity_id, label, qualified_name, source),
    )


def normalize_name(name):
    """Normaliza nome para qualified_name: lowercase, sem acento, hifens.
    Usa unicodedata para cobrir TODOS caracteres Unicode acentuados, nao so portugues."""
    import unicodedata
    s = name.lower().strip()
    # NFKD decompose + strip combining marks (acentos, diacriticos)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # Remove caracteres restantes nao-ASCII (ñ, ß, ð, þ, etc. viram n, ss, d, th)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "node"


def validate_label(label):
    if label not in VALID_LABELS:
        raise ValueError(f"Label invalido: {label}. Validos: {sorted(VALID_LABELS)}")


def validate_edge_type(etype):
    if etype not in VALID_EDGE_TYPES:
        raise ValueError(f"Tipo de aresta invalido: {etype}. Validos: {sorted(VALID_EDGE_TYPES)}")


def validate_provenance(prov):
    if prov not in VALID_PROVENANCE:
        raise ValueError(f"Provenance invalido: {prov}. Validos: {sorted(VALID_PROVENANCE)}")


def validate_properties(props):
    if props is None:
        return "{}"
    if not isinstance(props, dict):
        raise ValueError("properties deve ser um objeto JSON")
    # Serializa e valida tamanho (limite 64KB por propriedade)
    data = json.dumps(props, ensure_ascii=False)
    if len(data) > 65536:
        raise ValueError("properties excede 64KB")
    return data


# ============================================================
# Ferramentas de ESCRITA (para Antigravity popular o grafo)
# ============================================================

def tool_add_node(args, conn=None):
    """Cria um no. Retorna o id criado.
    conn: conexao opcional para reuso em batch (nao fecha se passada)."""
    label = args["label"]
    name = args["name"]
    validate_label(label)
    if not name or not isinstance(name, str):
        raise ValueError("name e obrigatorio e deve ser string")
    if len(name) > 512:
        raise ValueError("name excede 512 caracteres")

    qualified_name = args.get("qualified_name") or f"{label.lower()}:{normalize_name(name)}"
    properties = validate_properties(args.get("properties"))
    provenance = args.get("provenance", "EXTRACTED")
    validate_provenance(provenance)
    source = args.get("source", "")

    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO nodes (label, name, qualified_name, properties, provenance, source) VALUES (?, ?, ?, ?, ?, ?)",
            (label, name, qualified_name, properties, provenance, source),
        )
        node_id = cur.lastrowid
        audit_log(conn, "node_create", "node", node_id, label, qualified_name, source or "mcp")
        if own_conn:
            conn.commit()
        return {"id": node_id, "qualified_name": qualified_name}
    except sqlite3.IntegrityError:
        # qualified_name ja existe, retorna id existente
        row = conn.execute(
            "SELECT id FROM nodes WHERE qualified_name = ?", (qualified_name,)
        ).fetchone()
        return {"id": row["id"], "qualified_name": qualified_name, "already_exists": True}
    finally:
        if own_conn:
            conn.close()


def tool_upsert_node(args, conn=None):
    """Cria ou atualiza um no (busca por qualified_name ou label+name).
    conn: conexao opcional para reuso em batch (nao fecha se passada)."""
    label = args["label"]
    name = args["name"]
    validate_label(label)
    if not name or not isinstance(name, str):
        raise ValueError("name e obrigatorio")

    qualified_name = args.get("qualified_name") or f"{label.lower()}:{normalize_name(name)}"
    properties = validate_properties(args.get("properties"))
    provenance = args.get("provenance", "EXTRACTED")
    validate_provenance(provenance)
    source = args.get("source", "")

    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        # BEGIN IMMEDIATE: evita SQLITE_BUSY_SNAPSHOT em upgrade read->write
        if own_conn:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM nodes WHERE qualified_name = ?", (qualified_name,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE nodes SET label=?, name=?, properties=?, provenance=?, source=?, updated_at=datetime('now') WHERE id=?",
                (label, name, properties, provenance, source, row["id"]),
            )
            audit_log(conn, "node_update", "node", row["id"], label, qualified_name, source or "mcp")
            if own_conn:
                conn.commit()
            return {"id": row["id"], "qualified_name": qualified_name, "updated": True}
        cur = conn.execute(
            "INSERT INTO nodes (label, name, qualified_name, properties, provenance, source) VALUES (?, ?, ?, ?, ?, ?)",
            (label, name, qualified_name, properties, provenance, source),
        )
        node_id = cur.lastrowid
        audit_log(conn, "node_create", "node", node_id, label, qualified_name, source or "mcp")
        if own_conn:
            conn.commit()
        return {"id": node_id, "qualified_name": qualified_name, "created": True}
    finally:
        if own_conn:
            conn.close()


def tool_add_edge(args, conn=None):
    """Cria uma aresta. source e target podem ser id (int) ou qualified_name (str).
    conn: conexao opcional para reuso em batch (nao fecha se passada)."""
    source = args["source"]
    target = args["target"]
    etype = args["type"]
    validate_edge_type(etype)
    properties = validate_properties(args.get("properties"))
    provenance = args.get("provenance", "EXTRACTED")
    validate_provenance(provenance)
    weight = args.get("weight", 1.0)
    if not isinstance(weight, (int, float)) or weight < 0:
        raise ValueError("weight deve ser numero >= 0")
    source_arg = args.get("source_arg", "")

    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        # BEGIN IMMEDIATE: evita SQLITE_BUSY_SNAPSHOT (SELECT antes de INSERT)
        if own_conn:
            conn.execute("BEGIN IMMEDIATE")
        source_id = _resolve_node(conn, source)
        target_id = _resolve_node(conn, target)
        if source_id is None:
            return {"error": f"Source nao encontrado: {source}"}
        if target_id is None:
            return {"error": f"Target nao encontrado: {target}"}

        cur = conn.execute(
            "INSERT INTO edges (source_id, target_id, type, properties, provenance, weight) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source_id, target_id, type) DO UPDATE SET properties=excluded.properties, provenance=excluded.provenance, weight=excluded.weight",
            (source_id, target_id, etype, properties, provenance, weight),
        )
        # lastrowid e 0 em ON CONFLICT UPDATE; buscar id real
        edge_id = cur.lastrowid
        if edge_id == 0:
            row = conn.execute(
                "SELECT id FROM edges WHERE source_id=? AND target_id=? AND type=?",
                (source_id, target_id, etype),
            ).fetchone()
            edge_id = row["id"] if row else 0
        audit_log(conn, "edge_upsert", "edge", edge_id, etype, None, source_arg or "mcp")
        if own_conn:
            conn.commit()
        return {"id": edge_id, "source_id": source_id, "target_id": target_id}
    finally:
        if own_conn:
            conn.close()


def tool_add_nodes_batch(args):
    """Cria multiplos nos de uma vez. Reusa uma unica conexao (otimizado)."""
    nodes = args.get("nodes", [])
    if not isinstance(nodes, list) or len(nodes) > 10000:
        raise ValueError("nodes deve ser lista de ate 10000 itens")
    if not nodes:
        return {"created": 0, "results": []}

    conn = get_db()
    results = []
    try:
        for n in nodes:
            try:
                results.append(tool_add_node(n, conn))
            except (ValueError, sqlite3.Error) as e:
                results.append({"error": str(e), "input": n})
        conn.commit()
        return {"created": sum(1 for r in results if "error" not in r), "results": results}
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def tool_add_edges_batch(args):
    """Cria multiplas arestas de uma vez. Reusa uma unica conexao (otimizado)."""
    edges = args.get("edges", [])
    if not isinstance(edges, list) or len(edges) > 10000:
        raise ValueError("edges deve ser lista de ate 10000 itens")
    if not edges:
        return {"created": 0, "results": []}

    conn = get_db()
    results = []
    try:
        for e in edges:
            try:
                results.append(tool_add_edge(e, conn))
            except (ValueError, sqlite3.Error) as ex:
                results.append({"error": str(ex), "input": e})
        conn.commit()
        return {"created": sum(1 for r in results if "error" not in r), "results": results}
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def tool_delete_node(args):
    """Deleta um no e suas arestas (CASCADE)."""
    node_id = args.get("id")
    qualified_name = args.get("qualified_name")
    conn = get_db()
    try:
        if node_id is not None:
            if not isinstance(node_id, int) or node_id <= 0:
                raise ValueError("id deve ser inteiro positivo")
            cur = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        elif qualified_name:
            if not isinstance(qualified_name, str) or len(qualified_name) > 512:
                raise ValueError("qualified_name deve ser string de ate 512 chars")
            cur = conn.execute("DELETE FROM nodes WHERE qualified_name = ?", (qualified_name,))
        else:
            raise ValueError("Forneeca id ou qualified_name")
        deleted = cur.rowcount
        audit_log(conn, "node_delete", "node", node_id, None, qualified_name, "mcp")
        conn.commit()
        return {"deleted": deleted}
    finally:
        conn.close()


def tool_set_community(args):
    """Atribui um no a uma comunidade."""
    node_id = args["node_id"]
    community_id = args["community_id"]
    if not isinstance(node_id, int) or node_id <= 0:
        raise ValueError("node_id deve ser inteiro positivo")
    if not isinstance(community_id, int) or community_id < 0:
        raise ValueError("community_id deve ser inteiro >= 0")
    algorithm = args.get("algorithm", "manual")
    if not isinstance(algorithm, str) or len(algorithm) > 64:
        raise ValueError("algorithm deve ser string de ate 64 chars")
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO communities (node_id, community_id, algorithm) VALUES (?, ?, ?)",
            (node_id, community_id, algorithm),
        )
        audit_log(conn, "community_set", "community", node_id, None, None, "mcp")
        conn.commit()
        return {"node_id": node_id, "community_id": community_id}
    finally:
        conn.close()


# ============================================================
# Ferramentas de LEITURA (para Devin e Antigravity consultarem)
# ============================================================

def tool_search_graph(args):
    """Busca nos por padrao de nome (FTS5) e/ou label."""
    name_pattern = args.get("name_pattern", "")
    label = args.get("label")
    limit = min(args.get("limit", 50), 1000)

    conn = get_db()
    try:
        if label is not None and label not in VALID_LABELS:
            raise ValueError(f"Label invalido: {label}")

        # Sem padrao: listar todos (com filtro de label opcional)
        if not name_pattern or name_pattern == ".*":
            sql = "SELECT id, label, name, qualified_name, properties, provenance FROM nodes"
            params = []
            if label:
                sql += " WHERE label = ?"
                params.append(label)
            sql += " LIMIT ?"
            params.append(limit)
        else:
            # FTS5: sanitizar padrao para evitar syntax error/injection
            # Remove caracteres especiais de regex e FTS5 syntax
            # FTS5 chars especiais: " * ( ) - + : ^ AND OR NOT NEAR
            fts_query = name_pattern
            fts_query = re.sub(r'[.*^$()\-+:";\\\'/]', " ", fts_query)  # remove regex + FTS5 + SQL chars
            fts_query = re.sub(r'\b(AND|OR|NOT|NEAR)\b', ' ', fts_query, flags=re.IGNORECASE)  # remove FTS5 operators
            fts_query = " ".join(fts_query.split())  # normaliza espacos
            if not fts_query.strip():
                # Padrao so tinha regex chars, listar todos
                sql = "SELECT id, label, name, qualified_name, properties, provenance FROM nodes"
                params = []
                if label:
                    sql += " WHERE label = ?"
                    params.append(label)
                sql += " LIMIT ?"
                params.append(limit)
            else:
                sql = """SELECT n.id, n.label, n.name, n.qualified_name, n.properties, n.provenance
                         FROM nodes_fts f JOIN nodes n ON f.rowid = n.id
                         WHERE nodes_fts MATCH ?"""
                params = [fts_query]
                if label:
                    sql += " AND n.label = ?"
                    params.append(label)
                sql += " LIMIT ?"
                params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return {
            "total": len(rows),
            "results": [
                {
                    "id": r["id"], "label": r["label"], "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "properties": json.loads(r["properties"]) if r["properties"] else {},
                    "provenance": r["provenance"],
                }
                for r in rows
            ],
        }
    finally:
        conn.close()


SENSITIVE_PROP_KEYS = {"email", "phone", "cpf", "cnpj", "password", "token", "secret", "api_key", "credit_card"}

def _filter_sensitive_props(props):
    """Filtra propriedades sensiveis antes de retornar ao cliente (LGPD)."""
    if not isinstance(props, dict):
        return props
    return {k: ("[REDACTED]" if k.lower() in SENSITIVE_PROP_KEYS else v) for k, v in props.items()}


def tool_get_node(args):
    """Detalhes de um no, incluindo vizinhos."""
    node_id = args.get("id")
    qualified_name = args.get("qualified_name")
    conn = get_db()
    try:
        if node_id:
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        elif qualified_name:
            row = conn.execute("SELECT * FROM nodes WHERE qualified_name = ?", (qualified_name,)).fetchone()
        else:
            raise ValueError("Forneeca id ou qualified_name")
        if not row:
            return {"error": "No nao encontrado"}

        # Vizinhos (arestas de saida e entrada)
        out_edges = conn.execute(
            """SELECT e.type, e.provenance, e.weight, e.properties,
                      n.id as target_id, n.label as target_label, n.name as target_name, n.qualified_name as target_qualified_name
               FROM edges e JOIN nodes n ON e.target_id = n.id WHERE e.source_id = ?""",
            (row["id"],),
        ).fetchall()
        in_edges = conn.execute(
            """SELECT e.type, e.provenance, e.weight, e.properties,
                      n.id as source_id, n.label as source_label, n.name as source_name, n.qualified_name as source_qualified_name
               FROM edges e JOIN nodes n ON e.source_id = n.id WHERE e.target_id = ?""",
            (row["id"],),
        ).fetchall()

        return {
            "id": row["id"], "label": row["label"], "name": row["name"],
            "qualified_name": row["qualified_name"],
            "properties": _filter_sensitive_props(json.loads(row["properties"]) if row["properties"] else {}),
            "provenance": row["provenance"], "source": row["source"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "out_degree": len(out_edges), "in_degree": len(in_edges),
            "out_edges": [
                {"type": e["type"], "provenance": e["provenance"], "weight": e["weight"],
                 "target": {"id": e["target_id"], "label": e["target_label"],
                            "name": e["target_name"], "qualified_name": e["target_qualified_name"]}}
                for e in out_edges
            ],
            "in_edges": [
                {"type": e["type"], "provenance": e["provenance"], "weight": e["weight"],
                 "source": {"id": e["source_id"], "label": e["source_label"],
                            "name": e["source_name"], "qualified_name": e["source_qualified_name"]}}
                for e in in_edges
            ],
        }
    finally:
        conn.close()


def tool_trace_path(args):
    """Traca caminho mais curto entre dois nos (BFS).
    Otimizado: carrega todas as arestas uma vez em vez de N queries."""
    source = args["source"]
    target = args["target"]
    max_hops = min(args.get("max_hops", 10), 20)

    conn = get_db()
    try:
        source_id = _resolve_node(conn, source)
        target_id = _resolve_node(conn, target)
        if source_id is None:
            return {"error": f"Source nao encontrado: {source}"}
        if target_id is None:
            return {"error": f"Target nao encontrado: {target}"}
        if source_id == target_id:
            return {"path": [source_id], "hops": 0}

        # Carregar todas as arestas uma vez (grafo adjacencia bidirecional)
        rows = conn.execute("SELECT source_id, target_id FROM edges").fetchall()
        adj = {}
        for r in rows:
            adj.setdefault(r["source_id"], set()).add(r["target_id"])
            adj.setdefault(r["target_id"], set()).add(r["source_id"])

        # BFS em memoria
        queue = deque([(source_id, [source_id])])
        visited = {source_id}
        while queue:
            current, path = queue.popleft()
            if len(path) - 1 >= max_hops:
                continue
            for nid in adj.get(current, set()):
                if nid == target_id:
                    return {"path": path + [nid], "hops": len(path)}
                if nid not in visited:
                    visited.add(nid)
                    queue.append((nid, path + [nid]))
        return {"error": "Caminho nao encontrado", "max_hops": max_hops}
    finally:
        conn.close()


def tool_get_architecture(args):
    """Visao geral do grafo: labels, tipos de aresta, contagens.
    Otimizado: grau calculado em 2 queries agregadas em vez de subquery correlacionada O(n)."""
    conn = get_db()
    try:
        total_nodes = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
        total_edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]

        node_labels = conn.execute(
            "SELECT label, COUNT(*) as c FROM nodes GROUP BY label ORDER BY c DESC"
        ).fetchall()
        edge_types = conn.execute(
            "SELECT type, COUNT(*) as c FROM edges GROUP BY type ORDER BY c DESC"
        ).fetchall()
        provenance_stats = conn.execute(
            "SELECT provenance, COUNT(*) as c FROM nodes GROUP BY provenance"
        ).fetchall()

        # Grau por no: UNION de source_id e target_id, GROUP BY, ORDER BY, LIMIT 10
        # O(n + e) em vez de subquery correlacionada O(n * e)
        degree_sql = """
            SELECT node_id, SUM(cnt) as degree FROM (
                SELECT source_id as node_id, COUNT(*) as cnt FROM edges GROUP BY source_id
                UNION ALL
                SELECT target_id as node_id, COUNT(*) as cnt FROM edges GROUP BY target_id
            ) GROUP BY node_id ORDER BY degree DESC LIMIT 10
        """
        top_degrees = {r["node_id"]: r["degree"] for r in conn.execute(degree_sql).fetchall()}
        if top_degrees:
            placeholders = ",".join("?" * len(top_degrees))
            top_nodes = conn.execute(
                f"SELECT id, label, name, qualified_name FROM nodes WHERE id IN ({placeholders})",
                list(top_degrees.keys()),
            ).fetchall()
            # Reordenar por degree desc (sqlite nao garante ordem do IN)
            top_nodes.sort(key=lambda r: -top_degrees.get(r["id"], 0))
        else:
            top_nodes = []

        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "node_labels": [{"label": r["label"], "count": r["c"]} for r in node_labels],
            "edge_types": [{"type": r["type"], "count": r["c"]} for r in edge_types],
            "provenance": {"nodes": [{"provenance": r["provenance"], "count": r["c"]} for r in provenance_stats]},
            "top_connected_nodes": [
                {"id": r["id"], "label": r["label"], "name": r["name"],
                 "qualified_name": r["qualified_name"], "degree": top_degrees.get(r["id"], 0)}
                for r in top_nodes
            ],
        }
    finally:
        conn.close()


def tool_get_graph_schema(args):
    """Schema do grafo: labels e tipos de aresta disponiveis."""
    return {
        "node_labels": sorted(VALID_LABELS),
        "edge_types": sorted(VALID_EDGE_TYPES),
        "provenance_tags": sorted(VALID_PROVENANCE),
        "description": {
            "node_labels": "Tipos de no aceitos. Use estes labels ao criar nos.",
            "edge_types": "Tipos de aresta aceitos. Use estes tipos ao criar arestas.",
            "provenance_tags": "EXTRACTED=explicito na fonte, INFERRED=derivado por LLM, AMBIGUOUS=incerto.",
        },
    }


def tool_query_graph(args):
    """Executa query SQL read-only no grafo. Para queries complexas.
    Allowlist de tabelas permitidas (nao denylist de palavras)."""
    query = args.get("query", "")
    if not query:
        raise ValueError("query e obrigatorio")
    if not isinstance(query, str) or len(query) > 10000:
        raise ValueError("query deve ser string de ate 10000 chars")

    # Permitir apenas SELECT (defensivo)
    q_lower = query.strip().lower()
    if not q_lower.startswith("select"):
        raise ValueError("Apenas queries SELECT sao permitidas")

    # Allowlist de tabelas permitidas (nao denylist de palavras)
    ALLOWED_TABLES = {"nodes", "edges", "communities", "metadata", "nodes_fts", "audit_log", "telemetry_spans"}
    # Extrair nomes de tabela da query (FROM/JOIN)
    table_refs = re.findall(r'(?:from|join)\s+(\w+)', q_lower)
    for t in table_refs:
        if t not in ALLOWED_TABLES:
            raise ValueError(f"Tabela nao permitida: {t}. Permitidas: {sorted(ALLOWED_TABLES)}")

    # Bloquear acesso a metadados do SQLite (information disclosure)
    if "sqlite_master" in q_lower or "pragma" in q_lower or "sqlite_" in q_lower:
        raise ValueError("Acesso a metadados do SQLite nao permitido")

    # Bloquear UNION, subqueries e escrita (bypass da allowlist de tabelas)
    if "union" in q_lower or "insert" in q_lower or "update" in q_lower or "delete" in q_lower or "drop" in q_lower or "create" in q_lower or "alter" in q_lower or "replace" in q_lower:
        raise ValueError("Apenas queries SELECT simples sao permitidas (sem UNION, INSERT, UPDATE, DELETE, etc)")

    # Bloquear subqueries (SELECT dentro de SELECT) - pattern: (SELECT
    if "(select" in q_lower or "( select" in q_lower:
        raise ValueError("Subqueries nao sao permitidas. Use query_graph com queries simples.")

    limit = min(args.get("limit", 100), 10000)
    conn = get_db()
    try:
        cursor = conn.execute(query)
        rows = cursor.fetchmany(limit)
        cols = [d[0] for d in cursor.description] if cursor.description else []
        return {"columns": cols, "rows": [dict(r) for r in rows], "count": len(rows)}
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        conn.close()


def tool_list_projects(args):
    """Lista projetos/grafos disponiveis (neste caso, o grafo unico)."""
    conn = get_db()
    try:
        meta = conn.execute("SELECT key, value FROM metadata").fetchall()
        total_nodes = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
        total_edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
        return {
            "projects": [{
                "name": "kg-infra",
                "db_path": DB_PATH,
                "total_nodes": total_nodes,
                "total_edges": total_edges,
                "metadata": {r["key"]: r["value"] for r in meta},
            }],
        }
    finally:
        conn.close()


def tool_export_json(args):
    """Exporta o grafo completo como JSON (compativel com Obsidian/D3.js)."""
    conn = get_db()
    try:
        nodes = conn.execute("SELECT id, label, name, qualified_name, properties, provenance FROM nodes").fetchall()
        edges = conn.execute(
            """SELECT e.source_id, e.target_id, e.type, e.provenance, e.weight, e.properties
               FROM edges e"""
        ).fetchall()
        return {
            "nodes": [
                {"id": r["id"], "label": r["label"], "name": r["name"],
                 "qualified_name": r["qualified_name"],
                 "properties": json.loads(r["properties"]) if r["properties"] else {},
                 "provenance": r["provenance"]}
                for r in nodes
            ],
            "edges": [
                {"source": r["source_id"], "target": r["target_id"], "type": r["type"],
                 "provenance": r["provenance"], "weight": r["weight"],
                 "properties": json.loads(r["properties"]) if r["properties"] else {}}
                for r in edges
            ],
        }
    finally:
        conn.close()


# ============================================================
# Helpers
# ============================================================

def _resolve_node(conn, ref):
    """Resolve referencia de no (id int ou qualified_name str) para id."""
    if isinstance(ref, int):
        return ref
    if isinstance(ref, str):
        row = conn.execute("SELECT id FROM nodes WHERE qualified_name = ?", (ref,)).fetchone()
        return row["id"] if row else None
    return None


# ============================================================
# Task 1: Visualizacao interativa (graph.html com vis.js)
# ============================================================

# Cores por label (palette acessivel, alto contraste)
LABEL_COLORS = {
    # Negocio (original)
    "Customer": "#e74c3c", "Company": "#c0392b", "Contact": "#e67e22",
    "Product": "#2ecc71", "Deal": "#27ae60", "Ticket": "#3498db",
    "Issue": "#2980b9", "Agent": "#9b59b6", "Channel": "#8e44ad",
    "Article": "#1abc9c", "Topic": "#16a085", "Keyword": "#f39c12",
    "Campaign": "#d35400", "Audience": "#7f8c8d", "Interaction": "#95a5a6",
    "Proposal": "#bdc3c7", "Feedback": "#ecf0f1", "Lead": "#34495e",
    "Opportunity": "#2c3e50", "Contract": "#fd79a8", "Service": "#fdcb6e",
    "Department": "#e17055", "Event": "#00b894", "Document": "#00cec9",
    "Note": "#0984e3", "Tag": "#6c5ce7", "Person": "#a29bfe", "Organization": "#ffeaa7",
    # Infraestrutura/codigo (novos)
    "Project": "#e84393", "Module": "#6c5ce7", "Config": "#fdcb6e",
    "Folder": "#74b9ff", "File": "#a29bfe",
}

# Constantes de shape/dashes por provenance (evita dicionarios inline repetidos)
PROV_SHAPES = {"EXTRACTED": "dot", "INFERRED": "triangle", "AMBIGUOUS": "diamond"}
PROV_DASHES = {"EXTRACTED": False, "INFERRED": [10, 10], "AMBIGUOUS": [2, 8]}


def _esc(s):
    """HTML escape para prevenir XSS em dados do banco injetados no template."""
    return _html_escape(str(s), quote=True)


def _json_safe(obj):
    """json.dumps com escape de </script> para prevenir script injection.
    Tecnica: substituir < por \\u003C e / por \\/ apos json.dumps."""
    s = json.dumps(obj, ensure_ascii=False)
    return s.replace("<", "\\u003C").replace("/", "\\/").replace(">", "\\u003E")

def _validate_output_path(path, default_name):
    """Valida path de output: deve ser dentro do diretorio do DB ou absoluto seguro.
    Previne path traversal (../../etc/cron.d/evil) e symlink bypass.
    Default: subdiretorio grafo-out/ ao lado do DB."""
    if not path:
        out_dir = Path(DB_PATH).parent / "grafo-out"
        out_dir.mkdir(exist_ok=True)
        return str(out_dir / default_name)
    p = Path(path)
    # Se relativo, resolve contra o diretorio do DB
    if not p.is_absolute():
        p = Path(DB_PATH).parent / p
    # Verificar symlinks em qualquer componente do path (defesa contra symlink bypass)
    current = p
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("Symlinks nao permitidos no path de output")
        current = current.parent
    # Resolve e verifica que esta dentro do diretorio do DB (ou subdiretorio)
    db_dir = Path(DB_PATH).parent.resolve()
    resolved = p.resolve()
    if not str(resolved).startswith(str(db_dir)):
        raise ValueError(f"Output path deve estar dentro de {db_dir}")
    # So permite extensoes seguras
    if p.suffix not in (".html", ".json", ".md", ".csv", ".txt", ".db"):
        raise ValueError("Extensao de arquivo nao permitida (use .html, .json, .md, .csv, .txt, .db)")
    return str(p)


def tool_export_html(args):
    """Gera graph.html interativo standalone (vis.js CDN, CSS inline, sem deps).
    Design polido: dark mode, painel de detalhes, layout options, node sizing por degree,
    paleta perceptual, grid, tooltip estilizado, clustering, fisica configuravel.
    Args: output_path? (default: grafo-out/graph.html)"""
    output_path = _validate_output_path(args.get("output_path"), "graph.html")

    conn = get_db()
    try:
        nodes = conn.execute(
            "SELECT n.id, n.label, n.name, n.qualified_name, n.properties, n.provenance, n.source,"
            " c.community_id FROM nodes n LEFT JOIN communities c ON n.id = c.node_id"
        ).fetchall()
        edges = conn.execute(
            "SELECT e.source_id, e.target_id, e.type, e.provenance, e.weight, e.properties FROM edges e"
        ).fetchall()

        # Guarda: grafos muito grandes geram HTML pesado que trava o browser
        if len(nodes) > 5000:
            return {"error": f"Grafo muito grande para visualizar: {len(nodes)} nos > 5000 limite. Use export_json ou query_graph com limit."}

        # Calcular degree por no (para sizing log-scale, achado 2)
        degree_map = {}
        for e in edges:
            degree_map[e["source_id"]] = degree_map.get(e["source_id"], 0) + 1
            degree_map[e["target_id"]] = degree_map.get(e["target_id"], 0) + 1
        max_degree = max(degree_map.values()) if degree_map else 1

        # Construir nos para vis.js
        vis_nodes = []
        seen_node_ids = set()
        for n in nodes:
            if n["id"] in seen_node_ids:
                continue
            seen_node_ids.add(n["id"])
            props = json.loads(n["properties"]) if n["properties"] else {}
            # Filtrar PII das propriedades no tooltip
            props_safe = _filter_sensitive_props(props)
            color = LABEL_COLORS.get(n["label"], "#95a5a6")
            # Shape por provenance: EXTRACTED=circulo, INFERRED=triangulo, AMBIGUOUS=diamante
            shape = PROV_SHAPES.get(n["provenance"], "dot")
            # Size por degree (log scale, achado 2): min 15, max 40
            degree = degree_map.get(n["id"], 0)
            size = 15 + (25 * (math.log(degree + 1) / math.log(max_degree + 1))) if max_degree > 0 else 20
            # Tooltip rico (HTML, estilizado via CSS .vis-tooltip) com XSS escape
            tooltip_parts = [f"<b>{_esc(n['name'])}</b>", f"Label: {_esc(n['label'])}", f"Provenance: {_esc(n['provenance'])}"]
            if n["community_id"] is not None:
                tooltip_parts.append(f"Comunidade: {_esc(n['community_id'])}")
            if n["source"]:
                tooltip_parts.append(f"Fonte: {_esc(n['source'])}")
            tooltip_parts.append(f"Degree: {degree}")
            for k, v in props_safe.items():
                tooltip_parts.append(f"{_esc(k)}: {_esc(v)}")
            tooltip = "<br>".join(tooltip_parts)

            vis_nodes.append({
                "id": n["id"], "label": n["name"], "group": n["label"],
                "color": {"background": color, "border": color, "highlight": {"background": color, "border": "#2c3e50"}},
                "shape": shape, "size": round(size, 1),
                "title": tooltip,
                "font": {"size": 14, "color": "#f8fafc", "face": "Inter, Segoe UI, sans-serif"},
                "shadow": {"enabled": True, "color": "rgba(0,0,0,0.3)", "size": 8, "x": 3, "y": 3},
                # Metadados para painel de detalhes
                "_props": props_safe, "_degree": degree, "_qn": n["qualified_name"],
                "_prov": n["provenance"], "_source": n["source"], "_comm": n["community_id"],
            })

        # Construir arestas para vis.js
        vis_edges = []
        for i, e in enumerate(edges):
            # Cor por provenance: EXTRACTED=solido, INFERRED=tracejado, AMBIGUOUS=pontilhado
            dashes = PROV_DASHES.get(e["provenance"], False)
            vis_edges.append({
                "id": i, "from": e["source_id"], "to": e["target_id"],
                "label": e["type"], "dashes": dashes,
                "color": {"color": "#64748b", "highlight": "#38bdf8", "inherit": False},
                "font": {"size": 10, "align": "middle", "color": "#94a3b8"},
                "width": max(1, min(e["weight"], 4)),
                "arrows": {"to": {"enabled": True, "type": "arrow", "scaleFactor": 0.7}},
                "smooth": {"type": "continuous", "roundness": 0.5},
                "_prov": e["provenance"],  # para filterEdges O(1)
            })

        # Top 5 nos por degree (para stats panel)
        top_nodes = sorted(vis_nodes, key=lambda x: x.get("_degree", 0), reverse=True)[:5]
        top_nodes_info = [{"name": n["label"], "degree": n.get("_degree", 0), "label_type": n["group"]} for n in top_nodes]

        # HTML standalone com vis.js CDN, CSS inline, design polido
        html = f"""<!DOCTYPE html>
<html lang="pt-BR" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' https://unpkg.com; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'none';">
<title>kg-infra: Knowledge Graph</title>
<style>
:root {{
  --bg: #0f172a; --surface: #1e293b; --surface-hover: #334155; --text: #f8fafc; --muted: #94a3b8;
  --border: #334155; --accent: #38bdf8; --shadow: rgba(0,0,0,0.5); --card-bg: #1e293b;
}}
[data-theme="light"] {{
  --bg: #f8fafc; --surface: #ffffff; --surface-hover: #f1f5f9; --text: #0f172a; --muted: #64748b;
  --border: #e2e8f0; --accent: #0284c7; --shadow: rgba(0,0,0,0.08); --card-bg: #ffffff;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: 100%; height: 100%; overflow: hidden; }}
body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); display: flex; flex-direction: column; transition: background 0.3s, color 0.3s; }}
#header {{ padding: 12px 24px; background: var(--surface); border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; z-index: 100; flex-shrink: 0; }}
#header h1 {{ font-size: 18px; font-weight: 600; color: var(--text); }}
#header h1 span {{ color: var(--accent); }}
#stats {{ font-size: 12px; color: var(--muted); }}
#controls {{ padding: 8px 24px; background: var(--surface); border-bottom: 1px solid var(--border); display: flex; gap: 12px; flex-wrap: wrap; align-items: center; z-index: 90; flex-shrink: 0; }}
#controls label {{ font-size: 12px; font-weight: 500; color: var(--muted); }}
#controls select, #controls input {{ padding: 6px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 12px; background: var(--bg); color: var(--text); outline: none; }}
#controls select:focus, #controls input:focus {{ border-color: var(--accent); }}
#controls button {{ padding: 6px 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg); color: var(--text); cursor: pointer; font-size: 12px; font-weight: 500; transition: all 0.2s; }}
#controls button:hover {{ background: var(--surface-hover); border-color: var(--accent); }}
#controls button.active {{ background: var(--accent); color: #0f172a; border-color: var(--accent); font-weight: 600; }}

#main {{ display: flex; flex: 1; height: calc(100vh - 100px); position: relative; overflow: hidden; }}
#canvas-area {{ flex: 1; position: relative; width: 100%; height: 100%; overflow: hidden; background: var(--bg); }}
#network {{ width: 100%; height: 100%; background: var(--bg); }}

#sidebar {{ width: 320px; height: 100%; background: var(--surface); border-left: 1px solid var(--border); padding: 18px; overflow-y: auto; z-index: 80; flex-shrink: 0; box-shadow: -4px 0 20px var(--shadow); transition: margin-right 0.3s; }}
#sidebar.hidden {{ display: none; }}
#sidebar h2 {{ font-size: 15px; margin-bottom: 14px; color: var(--accent); border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
#sidebar .detail-row {{ margin-bottom: 8px; font-size: 13px; display: flex; justify-content: space-between; gap: 8px; }}
#sidebar .detail-label {{ color: var(--muted); font-weight: 500; }}
#sidebar .detail-value {{ color: var(--text); font-weight: 600; word-break: break-word; }}
#sidebar .props-grid {{ display: grid; grid-template-columns: auto 1fr; gap: 6px 12px; margin-top: 8px; background: var(--bg); padding: 10px; border-radius: 6px; border: 1px solid var(--border); }}
#sidebar .prop-key {{ color: var(--muted); font-size: 12px; font-weight: 500; }}
#sidebar .prop-val {{ color: var(--text); font-size: 12px; word-break: break-word; }}

#legend {{ position: absolute; bottom: 16px; left: 16px; background: var(--surface); padding: 14px; border-radius: 10px; box-shadow: 0 4px 16px var(--shadow); font-size: 11px; max-height: 280px; overflow-y: auto; border: 1px solid var(--border); z-index: 50; backdrop-filter: blur(8px); }}
#legend h3 {{ font-size: 12px; margin-bottom: 8px; color: var(--accent); text-transform: uppercase; letter-spacing: 0.5px; }}
.legend-item {{ display: flex; align-items: center; gap: 8px; margin-bottom: 4px; color: var(--text); }}
.legend-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
.legend-shape {{ width: 10px; height: 10px; flex-shrink: 0; }}

#stats-panel {{ position: absolute; top: 16px; right: 16px; background: var(--surface); padding: 14px; border-radius: 10px; box-shadow: 0 4px 16px var(--shadow); font-size: 11px; border: 1px solid var(--border); min-width: 220px; z-index: 50; backdrop-filter: blur(8px); }}
#stats-panel h3 {{ font-size: 12px; margin-bottom: 8px; color: var(--accent); text-transform: uppercase; letter-spacing: 0.5px; }}
#stats-panel .stat-row {{ display: flex; justify-content: space-between; margin-bottom: 4px; color: var(--text); }}

#loading {{ position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: var(--surface); padding: 20px 40px; border-radius: 12px; box-shadow: 0 4px 25px var(--shadow); font-size: 14px; color: var(--accent); font-weight: 600; border: 1px solid var(--border); z-index: 1000; }}
#loading.hidden {{ display: none; }}

div.vis-tooltip {{ position: absolute; visibility: hidden; padding: 12px 16px; background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 8px; font-size: 12px; font-family: 'Segoe UI', sans-serif; box-shadow: 0 8px 24px var(--shadow); pointer-events: none; z-index: 1000; max-width: 300px; line-height: 1.5; }}
.vis-network {{ outline: none; }}

#physics-config {{ display: none; position: absolute; bottom: 16px; right: 16px; background: var(--surface); padding: 16px; border-radius: 10px; box-shadow: 0 4px 16px var(--shadow); font-size: 11px; border: 1px solid var(--border); width: 240px; z-index: 60; backdrop-filter: blur(8px); }}
#physics-config.show {{ display: block; }}
#physics-config h3 {{ font-size: 12px; margin-bottom: 10px; color: var(--accent); text-transform: uppercase; }}
#physics-config .slider-row {{ margin-bottom: 10px; }}
#physics-config .slider-row label {{ display: block; font-size: 11px; color: var(--muted); margin-bottom: 4px; }}
#physics-config input[type="range"] {{ width: 100%; accent-color: var(--accent); }}
</style>
</head>
<body>
<div id="header">
  <h1>kg-infra: <span>Knowledge Graph</span></h1>
  <div id="stats">{len(vis_nodes)} nos, {len(vis_edges)} arestas, densidade {round(2*len(vis_edges)/max(len(vis_nodes),1), 3)}</div>
  <button onclick="toggleTheme()" style="margin-left:16px;padding:6px 12px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);cursor:pointer;font-size:12px;font-weight:500;">&#9681; Theme</button>
</div>
<div id="controls">
  <label>Buscar:</label>
  <input type="text" id="search" placeholder="Filtrar nos por nome..." oninput="filterNodes()" style="width:160px;">
  <label>Label:</label>
  <select id="labelFilter" onchange="filterNodes()">
    <option value="">Todos</option>
{chr(10).join(f'    <option value="{_esc(g)}">{_esc(g)}</option>' for g in sorted(set(n["group"] for n in vis_nodes)))}
  </select>
  <label>Provenance:</label>
  <select id="provFilter" onchange="filterEdges()">
    <option value="">Todas</option>
    <option value="EXTRACTED">EXTRACTED</option>
    <option value="INFERRED">INFERRED</option>
    <option value="AMBIGUOUS">AMBIGUOUS</option>
  </select>
  <label>Layout:</label>
  <select id="layoutSelect" onchange="changeLayout()">
    <option value="force">Force-directed</option>
    <option value="hierarchical">Hierarquico</option>
    <option value="circular">Circular</option>
  </select>
  <button onclick="network.fit()">Fit</button>
  <button onclick="togglePhysics()" id="physicsBtn">Pause Physics</button>
  <button onclick="togglePhysicsConfig()">Config</button>
  <button onclick="clusterByCommunity()" id="clusterBtn">Cluster</button>
  <button onclick="toggleSidebar()" id="sidebarBtn">Detalhes</button>
</div>
<div id="main">
  <div id="canvas-area">
    <div id="network"></div>
    <div id="legend">
      <h3>Labels</h3>
{chr(10).join(f'      <div class="legend-item"><div class="legend-dot" style="background:{LABEL_COLORS.get(l, "#95a5a6")}"></div>{_esc(l)}</div>' for l in sorted(set(n["group"] for n in vis_nodes)))}
      <h3 style="margin-top:12px">Shapes</h3>
      <div class="legend-item"><div class="legend-shape" style="background:#e74c3c;border-radius:50%"></div>EXTRACTED</div>
      <div class="legend-item"><div class="legend-shape" style="background:#e74c3c;clip-path:polygon(50% 0,100% 100%,0 100%)"></div>INFERRED</div>
      <div class="legend-item"><div class="legend-shape" style="background:#e74c3c;transform:rotate(45deg)"></div>AMBIGUOUS</div>
      <h3 style="margin-top:12px">Tamanho</h3>
      <div class="legend-item" style="font-size:10px;color:var(--muted);">Maior = mais conexoes (degree)</div>
    </div>
    <div id="stats-panel">
      <h3>Top Nos (degree)</h3>
{chr(10).join(f'      <div class="stat-row"><span>{_esc(t["name"])}</span><span>{t["degree"]}</span></div>' for t in top_nodes_info)}
    </div>
    <div id="physics-config">
      <h3>Fisica</h3>
      <div class="slider-row"><label>Repulsao: <span id="repVal">-4000</span></label><input type="range" id="repulsion" min="-10000" max="-500" value="-4000" oninput="updatePhysics()"></div>
      <div class="slider-row"><label>Spring: <span id="springVal">200</span></label><input type="range" id="springLen" min="50" max="400" value="200" oninput="updatePhysics()"></div>
      <div class="slider-row"><label>Gravidade: <span id="gravVal">0.2</span></label><input type="range" id="centralGrav" min="0" max="100" value="20" oninput="updatePhysics()"></div>
      <div class="slider-row"><label>Damping: <span id="dampVal">0.1</span></label><input type="range" id="damping" min="1" max="50" value="10" oninput="updatePhysics()"></div>
    </div>
    <div id="loading">Estabilizando layout...</div>
  </div>
  <aside id="sidebar" class="hidden">
    <h2>Detalhes do No</h2>
    <div id="detail-content"><p style="color:var(--muted);font-size:13px;">Clique em um no para ver detalhes.</p></div>
  </aside>
</div>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<script>
var nodesData = {_json_safe(vis_nodes)};
var edgesData = {_json_safe(vis_edges)};
var nodes = new vis.DataSet(nodesData);
var edges = new vis.DataSet(edgesData);
var container = document.getElementById('network');
var data = {{ nodes: nodes, edges: edges }};
var currentLayout = 'force';
var options = {{
  physics: {{
    barnesHut: {{ gravitationalConstant: -4000, centralGravity: 0.2, springLength: 200, springConstant: 0.02, damping: 0.1, avoidOverlap: 0.3 }},
    stabilization: {{ iterations: 300, updateInterval: 50, fit: true }}
  }},
  layout: {{ randomSeed: 42 }},
  interaction: {{ hover: true, tooltipDelay: 200, navigationButtons: true, keyboard: true, hoverConnectedEdges: true, zoomView: true, dragView: true }},
  nodes: {{ borderWidth: 2, shadow: {{ enabled: true, color: 'rgba(0,0,0,0.3)', size: 8, x: 3, y: 3 }} }},
  edges: {{ smooth: {{ type: 'continuous', roundness: 0.5 }}, arrows: {{ to: {{ enabled: true, scaleFactor: 0.7 }} }} }}
}};
var network = new vis.Network(container, data, options);
var physicsOn = true;
var clustered = false;

// Loading indicator
network.on('stabilizationProgress', function(params) {{
  document.getElementById('loading').classList.remove('hidden');
}});
network.on('stabilizationIterationsDone', function() {{
  document.getElementById('loading').classList.add('hidden');
}});
network.once('afterDrawing', function() {{
  document.getElementById('loading').classList.add('hidden');
}});

// Grid background (achado 19)
network.on('beforeDrawing', function(ctx) {{
  var w = ctx.canvas.clientWidth; var h = ctx.canvas.clientHeight;
  ctx.strokeStyle = 'rgba(255,255,255,0.05)'; ctx.lineWidth = 1;
  ctx.beginPath();
  for (var x = -w*4; x <= w*4; x += 50) {{ ctx.moveTo(x, h*4); ctx.lineTo(x, -h*4); }}
  for (var y = -h*4; y <= h*4; y += 50) {{ ctx.moveTo(w*4, y); ctx.lineTo(-w*4, y); }}
  ctx.stroke();
}});

// Click no no: mostrar detalhes no painel lateral
network.on('click', function(params) {{
  if (params.nodes.length > 0) {{
    var nodeId = params.nodes[0];
    var node = nodes.get(nodeId);
    showDetails(node);
    network.focus(nodeId, {{ scale: 1.2, animation: {{ duration: 500, easingFunction: 'easeInOutQuad' }} }});
  }}
}});

function showDetails(node) {{
  var container = document.getElementById('detail-content');
  container.innerHTML = '';
  function addRow(label, value) {{
    var row = document.createElement('div');
    row.className = 'detail-row';
    var l = document.createElement('span'); l.className = 'detail-label'; l.textContent = label + ':';
    var v = document.createElement('span'); v.className = 'detail-value'; v.textContent = String(value);
    row.appendChild(l); row.appendChild(v); container.appendChild(row);
  }}
  addRow('Nome', node.label || '');
  addRow('Label', node.group || '');
  addRow('QN', node._qn || '');
  addRow('Provenance', node._prov || '');
  addRow('Fonte', node._source || '');
  addRow('Comunidade', node._comm !== null && node._comm !== undefined ? node._comm : '-');
  addRow('Degree', node._degree || 0);
  if (node._props && Object.keys(node._props).length > 0) {{
    var h = document.createElement('h2'); h.textContent = 'Propriedades'; h.style.marginTop = '16px'; container.appendChild(h);
    var grid = document.createElement('div'); grid.className = 'props-grid';
    for (var k in node._props) {{
      var key = document.createElement('span'); key.className = 'prop-key'; key.textContent = k;
      var val = document.createElement('span'); val.className = 'prop-val'; val.textContent = String(node._props[k]);
      grid.appendChild(key); grid.appendChild(val);
    }}
    container.appendChild(grid);
  }}
  document.getElementById('sidebar').classList.remove('hidden');
  document.getElementById('sidebarBtn').classList.add('active');
}}

function toggleSidebar() {{
  var sb = document.getElementById('sidebar');
  sb.classList.toggle('hidden');
  document.getElementById('sidebarBtn').classList.toggle('active');
}}

function toggleTheme() {{
  var body = document.documentElement;
  var current = body.getAttribute('data-theme');
  var next = current === 'dark' ? 'light' : 'dark';
  body.setAttribute('data-theme', next);
  var fontColor = next === 'dark' ? '#f8fafc' : '#0f172a';
  nodesData.forEach(function(n) {{ nodes.update({{ id: n.id, font: {{ color: fontColor }} }}); }});
}}

function togglePhysics() {{
  if (physicsOn) {{ network.setOptions({{ physics: false }}); document.getElementById('physicsBtn').textContent = 'Resume Physics'; }}
  else {{ network.setOptions({{ physics: true }}); document.getElementById('physicsBtn').textContent = 'Pause Physics'; }}
  physicsOn = !physicsOn;
}}

function togglePhysicsConfig() {{ document.getElementById('physics-config').classList.toggle('show'); }}

function updatePhysics() {{
  var rep = parseInt(document.getElementById('repulsion').value);
  var spring = parseInt(document.getElementById('springLen').value);
  var grav = parseInt(document.getElementById('centralGrav').value) / 100;
  var damp = parseInt(document.getElementById('damping').value) / 100;
  document.getElementById('repVal').textContent = rep;
  document.getElementById('springVal').textContent = spring;
  document.getElementById('gravVal').textContent = grav;
  document.getElementById('dampVal').textContent = damp;
  network.setOptions({{ physics: {{ barnesHut: {{ gravitationalConstant: rep, centralGravity: grav, springLength: spring, damping: damp }} }} }});
}}

function changeLayout() {{
  var layout = document.getElementById('layoutSelect').value;
  currentLayout = layout;
  if (layout === 'force') {{
    network.setOptions({{ layout: {{ hierarchical: false, randomSeed: 42 }}, physics: {{ enabled: true }} }});
  }} else if (layout === 'hierarchical') {{
    network.setOptions({{ layout: {{ hierarchical: {{ enabled: true, levelSeparation: 150, nodeSpacing: 100, direction: 'UD', sortMethod: 'hubsize' }} }}, physics: {{ enabled: false }} }});
  }} else if (layout === 'circular') {{
    var n = nodesData.length;
    var radius = Math.max(200, n * 15);
    var positions = {{}};
    for (var i = 0; i < n; i++) {{
      var angle = (i / n) * 2 * Math.PI;
      positions[nodesData[i].id] = {{ x: Math.cos(angle) * radius, y: Math.sin(angle) * radius }};
    }}
    network.setOptions({{ physics: {{ enabled: false }} }});
    network.moveTo({{ position: {{ x: 0, y: 0 }}, scale: 0.8 }});
    for (var id in positions) {{ nodes.update({{ id: parseInt(id), x: positions[id].x, y: positions[id].y, fixed: {{ x: true, y: true }} }}); }}
  }}
}}

function clusterByCommunity() {{
  if (clustered) {{ network.unselectAll(); clustered = false; document.getElementById('clusterBtn').classList.remove('active'); return; }}
  var communities = {{}};
  nodesData.forEach(function(n) {{
    var key = (n._comm !== null && n._comm !== undefined) ? 'comm_' + n._comm : 'label_' + n.group;
    if (!communities[key]) communities[key] = [];
    communities[key].push(n.id);
  }});
  for (var c in communities) {{
    (function(clusterName, ids) {{
      network.cluster({{
        joinCondition: function(nodeOptions) {{ return ids.indexOf(nodeOptions.id) >= 0; }},
        clusterNodeProperties: {{ id: 'cluster_' + clusterName, label: clusterName + ' (' + ids.length + ')', shape: 'database', color: {{ background: '#f39c12', border: '#e67e22' }}, size: 30 }}
      }});
    }})(c, communities[c]);
  }}
  clustered = true;
  document.getElementById('clusterBtn').classList.add('active');
}}

function filterNodes() {{
  var q = document.getElementById('search').value.toLowerCase();
  var label = document.getElementById('labelFilter').value;
  nodes.forEach(function(n) {{
    var match = (!q || n.label.toLowerCase().indexOf(q) >= 0) && (!label || n.group === label);
    nodes.update({{ id: n.id, hidden: !match, opacity: match ? 1 : 0.1 }});
  }});
}}

function filterEdges() {{
  var prov = document.getElementById('provFilter').value;
  edges.forEach(function(e) {{
    if (!prov) {{ edges.update({{ id: e.id, hidden: false }}); return; }}
    edges.update({{ id: e.id, hidden: e._prov !== prov }});
  }});
}}
</script>
</body>
</html>"""

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        return {"path": output_path, "nodes": len(vis_nodes), "edges": len(vis_edges)}
    finally:
        conn.close()


# ============================================================
# Task 2: Deteccao de comunidades (Louvain em Python puro)
# ============================================================

def _louvain_communities(adj, resolution=1.0, max_iter=100):
    """Louvain simplificado em Python puro (sem networkx/igraph).
    adj: dict {node_id: set(vizinhos)}. Retorna dict {node_id: community_id}.
    Otimizado: sigma_tot mantido em dict (O(1) por lookup) em vez de O(n) scan."""
    if not adj:
        return {}

    n = len(adj)
    # Guarda: para grafos grandes, fallback para connected components
    if n > 2000:
        # Connected components via Union-Find
        parent = {node: node for node in adj}
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for node in adj:
            for nb in adj[node]:
                parent[find(node)] = find(nb)
        comm = {node: find(node) for node in adj}
        unique = sorted(set(comm.values()))
        remap = {old: new for new, old in enumerate(unique)}
        return {node: remap[c] for node, c in comm.items()}

    # Fase 1: cada no comeca na propria comunidade
    comm = {n: i for i, n in enumerate(adj)}

    # Calcula grau total
    degree = {n: len(adj[n]) for n in adj}
    total_edges = sum(degree.values()) / 2
    if total_edges == 0:
        return comm

    # sigma_tot mantido em dict: soma dos graus por comunidade (O(1) por lookup)
    # comm[n] = i (indice), entao sigma_tot key = community_id (indice), nao node_id
    sigma_tot = {comm[n]: degree[n] for n in adj}

    for iteration in range(max_iter):
        improved = False
        for node in adj:
            current_comm = comm[node]
            # Comunidades dos vizinhos
            neighbor_comms = {}
            for nb in adj[node]:
                c = comm[nb]
                neighbor_comms[c] = neighbor_comms.get(c, 0) + 1

            # Modularity gain para cada comunidade vizinha
            best_comm = current_comm
            best_gain = 0
            k_i = degree[node]
            for c, k_i_in in neighbor_comms.items():
                if c == current_comm:
                    continue
                # sigma_tot agora e O(1) (dict lookup)
                gain = k_i_in - resolution * sigma_tot[c] * k_i / (2 * total_edges)
                if gain > best_gain:
                    best_gain = gain
                    best_comm = c

            if best_comm != current_comm:
                # Atualizar sigma_tot: remove do old, adiciona no new
                sigma_tot[current_comm] -= k_i
                sigma_tot[best_comm] = sigma_tot.get(best_comm, 0) + k_i
                comm[node] = best_comm
                improved = True

        if not improved:
            break

    # Renumerar comunidades para 0..N-1
    unique_comms = sorted(set(comm.values()))
    remap = {old: new for new, old in enumerate(unique_comms)}
    return {n: remap[c] for n, c in comm.items()}


def tool_detect_communities(args):
    """Detecta comunidades automaticamente (Louvain em Python puro).
    Atribui cada no a uma comunidade e salva na tabela communities.
    Args: algorithm? (default: louvain), resolution? (default: 1.0)"""
    algorithm = args.get("algorithm", "louvain")
    if algorithm not in ("louvain", "connected_components"):
        raise ValueError("Algoritmo deve ser: louvain ou connected_components")
    resolution = args.get("resolution", 1.0)
    if not isinstance(resolution, (int, float)) or resolution <= 0:
        raise ValueError("resolution deve ser numero > 0")

    conn = get_db()
    try:
        # Carregar arestas e construir adjacencia
        rows = conn.execute("SELECT source_id, target_id FROM edges").fetchall()
        adj = {}
        node_ids = {r["id"] for r in conn.execute("SELECT id FROM nodes").fetchall()}
        for nid in node_ids:
            adj.setdefault(nid, set())
        for r in rows:
            adj.setdefault(r["source_id"], set()).add(r["target_id"])
            adj.setdefault(r["target_id"], set()).add(r["source_id"])

        if algorithm == "connected_components":
            # Union-Find para connected components
            parent = {n: n for n in adj}
            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x
            def union(a, b):
                parent[find(a)] = find(b)
            for r in rows:
                union(r["source_id"], r["target_id"])
            comm = {n: find(n) for n in adj}
            unique = sorted(set(comm.values()))
            remap = {old: new for new, old in enumerate(unique)}
            comm = {n: remap[c] for n, c in comm.items()}
        else:
            comm = _louvain_communities(adj, resolution)

        # Salvar comunidades
        for node_id, community_id in comm.items():
            conn.execute(
                "INSERT OR REPLACE INTO communities (node_id, community_id, algorithm) VALUES (?, ?, ?)",
                (node_id, community_id, algorithm),
            )
        conn.commit()

        # Estatisticas
        comm_sizes = {}
        for c in comm.values():
            comm_sizes[c] = comm_sizes.get(c, 0) + 1

        return {
            "algorithm": algorithm,
            "total_communities": len(comm_sizes),
            "total_nodes": len(comm),
            "communities": [
                {"community_id": c, "size": s}
                for c, s in sorted(comm_sizes.items(), key=lambda x: -x[1])
            ],
        }
    finally:
        conn.close()


# ============================================================
# Task 3: Metricas de centralidade (Python puro, O(n^2) para <1000 nos)
# ============================================================

def tool_get_centrality(args):
    """Calcula metricas de centralidade: degree, betweenness, closeness, pagerank.
    Retorna ranking dos nos mais influentes. Args: metric? (default: all), limit? (default: 20)"""
    metric = args.get("metric", "all")
    if metric not in ("all", "degree", "betweenness", "closeness", "pagerank"):
        raise ValueError("metric deve ser: all, degree, betweenness, closeness ou pagerank")
    limit = min(args.get("limit", 20), 100)

    conn = get_db()
    try:
        node_rows = conn.execute("SELECT id, label, name, qualified_name FROM nodes").fetchall()
        edge_rows = conn.execute("SELECT source_id, target_id FROM edges").fetchall()

        if not node_rows:
            return {"error": "Grafo vazio"}

        nodes = {r["id"]: {"label": r["label"], "name": r["name"], "qualified_name": r["qualified_name"]} for r in node_rows}
        adj = {nid: set() for nid in nodes}
        for r in edge_rows:
            adj.setdefault(r["source_id"], set()).add(r["target_id"])
            adj.setdefault(r["target_id"], set()).add(r["source_id"])

        results = {}

        # Degree centrality: grau / (n-1)
        if metric in ("all", "degree"):
            n = len(nodes)
            degree = {nid: len(adj.get(nid, set())) / max(1, n - 1) for nid in nodes}
            results["degree"] = sorted(
                [{"node_id": nid, **nodes[nid], "score": round(s, 4)} for nid, s in degree.items()],
                key=lambda x: -x["score"]
            )[:limit]

        # Betweenness centrality: fracao de caminhos mais curtos que passam pelo no
        # O(n^2 * (n+e)) via Brandes. Guarda: skip se n > 500 (pode travar maquina fraca).
        if metric in ("all", "betweenness"):
            n = len(nodes)
            if n > 500:
                results["betweenness"] = [{"error": f"Betweenness skipado: {n} nos > 500 limite (O(n^3) pode travar). Use metric=degree ou metric=pagerank."}]
            else:
                betweenness = {nid: 0.0 for nid in nodes}
                for source in nodes:
                    # BFS contando caminhos mais curtos (deque = O(1) popleft)
                    dist = {source: 0}
                    paths = {source: 1}
                    queue = deque([source])
                    visited_order = [source]
                    while queue:
                        current = queue.popleft()
                        for nb in adj.get(current, set()):
                            if nb not in dist:
                                dist[nb] = dist[current] + 1
                                paths[nb] = paths[current]
                                queue.append(nb)
                                visited_order.append(nb)
                            elif dist[nb] == dist[current] + 1:
                                paths[nb] += paths[current]
                    # Dependencia acumulada (Brandes algorithm)
                    dep = {nid: 0.0 for nid in nodes}
                    for node in reversed(visited_order):
                        for nb in adj.get(node, set()):
                            if dist.get(nb, float('inf')) == dist.get(node, 0) + 1:
                                dep[node] += paths[node] / paths[nb] * (1 + dep[nb])
                        if node != source:
                            betweenness[node] += dep[node]
                # Normalizar
                norm = max(1, (n - 1) * (n - 2) / 2)
                betweenness = {nid: b / norm for nid, b in betweenness.items()}
                results["betweenness"] = sorted(
                    [{"node_id": nid, **nodes[nid], "score": round(s, 4)} for nid, s in betweenness.items()],
                    key=lambda x: -x["score"]
                )[:limit]

        # Closeness centrality: 1 / soma das distancias
        # O(n * (n+e)) via BFS. Guarda: skip se n > 2000.
        if metric in ("all", "closeness"):
            n = len(nodes)
            if n > 2000:
                results["closeness"] = [{"error": f"Closeness skipado: {n} nos > 2000 limite."}]
            else:
                closeness = {}
                for source in nodes:
                    dist = {source: 0}
                    queue = deque([source])
                    while queue:
                        current = queue.popleft()
                        for nb in adj.get(current, set()):
                            if nb not in dist:
                                dist[nb] = dist[current] + 1
                                queue.append(nb)
                    total_dist = sum(dist.values())
                    closeness[source] = (len(dist) - 1) / total_dist if total_dist > 0 else 0
                results["closeness"] = sorted(
                    [{"node_id": nid, **nodes[nid], "score": round(s, 4)} for nid, s in closeness.items()],
                    key=lambda x: -x["score"]
                )[:limit]

        # PageRank: power iteration
        if metric in ("all", "pagerank"):
            n = len(nodes)
            if n == 0:
                results["pagerank"] = []
            else:
                pr = {nid: 1.0 / n for nid in nodes}
                damping = 0.85
                for _ in range(100):
                    new_pr = {nid: (1 - damping) / n for nid in nodes}
                    for nid in nodes:
                        neighbors = adj.get(nid, set())
                        if neighbors:
                            share = damping * pr[nid] / len(neighbors)
                            for nb in neighbors:
                                new_pr[nb] += share
                        else:
                            # Dangling node: redistribui
                            for nb in nodes:
                                new_pr[nb] += damping * pr[nid] / n
                    # Convergencia
                    diff = sum(abs(new_pr[nid] - pr[nid]) for nid in nodes)
                    pr = new_pr
                    if diff < 1e-6:
                        break
                results["pagerank"] = sorted(
                    [{"node_id": nid, **nodes[nid], "score": round(s, 6)} for nid, s in pr.items()],
                    key=lambda x: -x["score"]
                )[:limit]

        return results
    finally:
        conn.close()


# ============================================================
# Task 4: Telemetria/tracing (spans em SQLite, zero deps)
# ============================================================

def tool_get_telemetry(args):
    """Retorna metricas de telemetria: latencia de queries, operacoes por minuto,
    erros, top tools. Args: window? (default: 60 minutos), limit? (default: 20)"""
    window = args.get("window", 60)
    if not isinstance(window, int) or window <= 0 or window > 10080:
        raise ValueError("window deve ser inteiro entre 1 e 10080 minutos (7 dias)")
    limit = min(args.get("limit", 20), 100)

    conn = get_db()
    try:
        # Verificar se tabela telemetry existe
        try:
            conn.execute("SELECT 1 FROM telemetry_spans LIMIT 1")
        except sqlite3.OperationalError:
            return {"error": "Tabela telemetry_spans nao existe. Rode o schema atualizado."}

        # Window modifier para SQLite datetime
        window_mod = f"-{window} minutes"

        # Total de spans na janela
        total = conn.execute(
            "SELECT COUNT(*) as c FROM telemetry_spans WHERE timestamp >= datetime('now', ?)",
            (window_mod,),
        ).fetchone()["c"]

        if total == 0:
            return {"window_minutes": window, "total_spans": 0, "latency": {}, "top_tools": [], "errors": []}

        # Latencia: p50, p90, max
        latency = conn.execute(
            """SELECT
               MIN(duration_ms) as min,
               (SELECT duration_ms FROM telemetry_spans WHERE timestamp >= datetime('now', ?)
                ORDER BY duration_ms LIMIT 1 OFFSET (
                    SELECT COUNT(*)/2 FROM telemetry_spans WHERE timestamp >= datetime('now', ?)
                )) as p50,
               (SELECT duration_ms FROM telemetry_spans WHERE timestamp >= datetime('now', ?)
                ORDER BY duration_ms LIMIT 1 OFFSET (
                    SELECT COUNT(*)*9/10 FROM telemetry_spans WHERE timestamp >= datetime('now', ?)
                )) as p90,
               MAX(duration_ms) as max
               FROM telemetry_spans WHERE timestamp >= datetime('now', ?)""",
            (window_mod, window_mod, window_mod, window_mod, window_mod),
        ).fetchone()

        # Top tools por frequencia
        top_tools = conn.execute(
            """SELECT tool, COUNT(*) as c, AVG(duration_ms) as avg_ms, MAX(duration_ms) as max_ms
               FROM telemetry_spans WHERE timestamp >= datetime('now', ?)
               GROUP BY tool ORDER BY c DESC LIMIT ?""",
            (window_mod, limit),
        ).fetchall()

        # Erros
        errors = conn.execute(
            """SELECT tool, error, COUNT(*) as c
               FROM telemetry_spans WHERE error IS NOT NULL AND timestamp >= datetime('now', ?)
               GROUP BY tool, error ORDER BY c DESC LIMIT ?""",
            (window_mod, limit),
        ).fetchall()

        return {
            "window_minutes": window,
            "total_spans": total,
            "latency": {
                "min_ms": latency["min"] if latency["min"] else 0,
                "p50_ms": latency["p50"] if latency["p50"] else 0,
                "p90_ms": latency["p90"] if latency["p90"] else 0,
                "max_ms": latency["max"] if latency["max"] else 0,
            },
            "top_tools": [
                {"tool": r["tool"], "calls": r["c"], "avg_ms": round(r["avg_ms"], 2) if r["avg_ms"] else 0, "max_ms": r["max_ms"]}
                for r in top_tools
            ],
            "errors": [
                {"tool": r["tool"], "error": r["error"], "count": r["c"]}
                for r in errors
            ],
        }
    finally:
        conn.close()


# ============================================================
# Task 5: Relatorio automatico (GRAPH_REPORT.md)
# ============================================================

def tool_generate_report(args):
    """Gera GRAPH_REPORT.md com: god nodes, surprising connections, suggested questions.
    Combina centralidade, comunidades e estatisticas. Args: output_path? (default: GRAPH_REPORT.md)"""
    output_path = _validate_output_path(args.get("output_path"), "GRAPH_REPORT.md")

    conn = get_db()
    try:
        total_nodes = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
        total_edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
        if total_nodes == 0:
            return {"error": "Grafo vazio"}

        # God nodes (top degree)
        degree_sql = """
            SELECT node_id, SUM(cnt) as degree FROM (
                SELECT source_id as node_id, COUNT(*) as cnt FROM edges GROUP BY source_id
                UNION ALL
                SELECT target_id as node_id, COUNT(*) as cnt FROM edges GROUP BY target_id
            ) GROUP BY node_id ORDER BY degree DESC LIMIT 5
        """
        god_nodes = conn.execute(degree_sql).fetchall()
        god_node_ids = [r["node_id"] for r in god_nodes]
        god_details = {}
        if god_node_ids:
            placeholders = ",".join("?" * len(god_node_ids))
            for r in conn.execute(f"SELECT id, label, name, qualified_name FROM nodes WHERE id IN ({placeholders})", god_node_ids).fetchall():
                god_details[r["id"]] = r

        # Surprising connections: betweenness (nos que conectam comunidades diferentes)
        # Aproximacao: nos com high betweenness que conectam labels diferentes
        edge_labels = conn.execute(
            """SELECT n1.label as l1, n2.label as l2, e.type, n1.name as n1_name, n2.name as n2_name,
               n1.qualified_name as q1, n2.qualified_name as q2
               FROM edges e
               JOIN nodes n1 ON e.source_id = n1.id
               JOIN nodes n2 ON e.target_id = n2.id
               WHERE n1.label != n2.label
               LIMIT 10"""
        ).fetchall()

        # Comunidades
        comm_count = conn.execute("SELECT COUNT(DISTINCT community_id) as c FROM communities").fetchone()["c"]
        comm_sizes = conn.execute(
            "SELECT community_id, COUNT(*) as c FROM communities GROUP BY community_id ORDER BY c DESC LIMIT 5"
        ).fetchall()

        # Labels distribution
        labels = conn.execute("SELECT label, COUNT(*) as c FROM nodes GROUP BY label ORDER BY c DESC").fetchall()
        edge_types = conn.execute("SELECT type, COUNT(*) as c FROM edges GROUP BY type ORDER BY c DESC").fetchall()

        # Provenance distribution
        prov = conn.execute("SELECT provenance, COUNT(*) as c FROM nodes GROUP BY provenance ORDER BY c DESC").fetchall()

        # Suggested questions (baseado no que existe no grafo)
        suggestions = []
        if any(l["label"] == "Customer" for l in labels):
            suggestions.append("Quais clientes tem os maiores MRR e quais produtos eles compraram?")
        if any(l["label"] == "Ticket" for l in labels):
            suggestions.append("Quais tickets estao abertos e qual produto cada ticket reclama?")
        if any(l["label"] == "Deal" for l in labels):
            suggestions.append("Quais deals estao em negociacao e qual o valor total do pipeline?")
        if any(l["label"] == "Article" for l in labels):
            suggestions.append("Quais artigos tem mais visualizacoes e quais topicos eles cobrem?")
        if any(l["label"] == "Campaign" for l in labels):
            suggestions.append("Quais campanhas tem maior budget e quais clientes elas targetam?")
        suggestions.append("Trace o caminho entre o cliente com maior MRR e o artigo mais popular.")
        suggestions.append("Quais nos sao 'god nodes' (mais conectados) e por que?")

        # Gerar markdown
        report = f"""# GRAPH_REPORT.md: Analise do Knowledge Graph

**Data:** {__import__('datetime').datetime.now().isoformat()}
**Total:** {total_nodes} nos, {total_edges} arestas

## God Nodes (nos mais conectados)

Os "god nodes" sao os nos com maior grau de conectividade. Eles sao pontos de
passagem obrigatórios no grafo e frequentemente representam entidades centrais
no negocio.

| # | No | Label | Grau |
|---|---|---|---|
"""
        for i, gn in enumerate(god_nodes, 1):
            d = god_details.get(gn["node_id"])
            if d:
                report += f"| {i} | {d['name']} | {d['label']} | {gn['degree']} |\n"

        report += f"""
## Surprising Connections (conexoes cross-domain)

Conexoes entre nos de labels diferentes revelam relacoes inesperadas no negocio.
Estas sao as conexoes cross-label mais relevantes:

| # | De | Label | Para | Label | Tipo |
|---|---|---|---|---|---|
"""
        for i, e in enumerate(edge_labels, 1):
            report += f"| {i} | {e['n1_name']} | {e['l1']} | {e['n2_name']} | {e['l2']} | {e['type']} |\n"

        report += f"""
## Comunidades

"""
        if comm_count > 0:
            report += f"**Comunidades detectadas:** {comm_count}\n\n"
            report += "| Community | Size |\n|---|---|\n"
            for c in comm_sizes:
                report += f"| {c['community_id']} | {c['c']} |\n"
        else:
            report += "Nenhuma comunidade detectada. Rode `detect_communities` para agrupar nos.\n"

        report += f"""
## Distribuicao

### Labels (nos)
| Label | Count |
|---|---|
"""
        for l in labels:
            report += f"| {l['label']} | {l['c']} |\n"

        report += "\n### Tipos de aresta\n| Type | Count |\n|---|---|\n"
        for e in edge_types:
            report += f"| {e['type']} | {e['c']} |\n"

        report += "\n### Provenance\n| Provenance | Count |\n|---|---|\n"
        for p in prov:
            report += f"| {p['provenance']} | {p['c']} |\n"

        report += f"""
## Perguntas sugeridas

Perguntas que o grafo pode responder (use no Devin/Antigravity):

"""
        for i, q in enumerate(suggestions, 1):
            report += f"{i}. {q}\n"

        report += f"""
## Como usar este relatorio

1. **God nodes**: investigar por que esses nos sao tao conectados. Sao hubs
   naturais do negocio ou ruido?
2. **Surprising connections**: validar se essas conexoes cross-domain fazem
   sentido. Se nao, revisar a extracao do LLM.
3. **Comunidades**: cada comunidade representa um subsistema do negocio.
   Comparar com a estrutura organizacional esperada.
4. **Perguntas sugeridas**: copiar para o Devin/Antigravity e usar as tools
   `search_graph`, `trace_path`, `get_centrality` para responder.
"""

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)
        return {"path": output_path, "god_nodes": len(god_nodes), "surprising_connections": len(edge_labels), "suggestions": len(suggestions)}
    finally:
        conn.close()


def _redact_pii(text, max_len=500):
    """Redact PII de args_summary antes de salvar em telemetry_spans.
    Remove emails, valores de propriedades sensiveis."""
    import re
    if not text:
        return ""
    # Redact valores de propriedades sensiveis (email, phone, cpf, cnpj, password, token, secret, api_key)
    text = re.sub(r'"(email|phone|cpf|cnpj|password|token|secret|api_key)"\s*:\s*"[^"]*"', r'"\1":"[REDACTED]"', text, flags=re.IGNORECASE)
    # Redact qualquer email restante (soltos ou em valores de outras propriedades)
    text = re.sub(r'[\w.+-]+@[\w-]+\.[\w.-]+', '[EMAIL]', text)
    # Truncar
    return text[:max_len]


# ============================================================
# Task 6: Export consolidado (grafo-out/)
# ============================================================

def _save_json(data, filepath):
    """Helper: salva dict como JSON em filepath. Reduz duplicacao em export_all."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _safe_error(e, max_len=200):
    """Sanitiza mensagem de erro para nao expor internals (paths, nomes de tabela)."""
    msg = str(e)[:max_len]
    # Remover paths absolutos
    msg = re.sub(r'/[^\s,)]+', '[PATH]', msg)
    return msg


def tool_export_all(args):
    """Gera todos os artefatos de saida em grafo-out/: graph.html, graph.json,
    GRAPH_REPORT.md, HEALTH.json, COMMUNITIES.json, CENTRALITY.json.
    Args: output_dir? (default: grafo-out/ ao lado do DB)"""
    out_dir = Path(DB_PATH).parent / "grafo-out"
    if args.get("output_dir"):
        custom = Path(args["output_dir"])
        if not custom.is_absolute():
            custom = Path(DB_PATH).parent / custom
        # Verificar symlinks (defesa contra symlink bypass)
        current = custom
        while current != current.parent:
            if current.is_symlink():
                raise ValueError("Symlinks nao permitidos no output_dir")
            current = current.parent
        db_dir = Path(DB_PATH).parent.resolve()
        if not str(custom.resolve()).startswith(str(db_dir)):
            raise ValueError(f"output_dir deve estar dentro de {db_dir}")
        out_dir = custom
    out_dir.mkdir(parents=True, exist_ok=True)

    # Verificar espaco em disco (defesa contra DoS por exaustao)
    import shutil
    disk = shutil.disk_usage(out_dir)
    if disk.free < 100 * 1024 * 1024:  # 100MB minimo
        return {"error": "Espaco em disco insuficiente (minimo 100MB livre)", "free_mb": round(disk.free / (1024*1024), 1)}

    results = {}
    # 1. graph.html
    try:
        r = tool_export_html({"output_path": str(out_dir / "graph.html")})
        results["graph.html"] = r
    except Exception as e:
        results["graph.html"] = {"error": _safe_error(e)}
    # 2. graph.json
    try:
        r = tool_export_json({})
        _save_json(r, out_dir / "graph.json")
        results["graph.json"] = {"path": str(out_dir / "graph.json"), "nodes": len(r.get("nodes", [])), "edges": len(r.get("edges", []))}
    except Exception as e:
        results["graph.json"] = {"error": _safe_error(e)}
    # 3. GRAPH_REPORT.md
    try:
        r = tool_generate_report({"output_path": str(out_dir / "GRAPH_REPORT.md")})
        results["GRAPH_REPORT.md"] = r
    except Exception as e:
        results["GRAPH_REPORT.md"] = {"error": _safe_error(e)}
    # 4. HEALTH.json
    try:
        r = tool_health_check({})
        _save_json(r, out_dir / "HEALTH.json")
        results["HEALTH.json"] = r
    except Exception as e:
        results["HEALTH.json"] = {"error": _safe_error(e)}
    # 5. COMMUNITIES.json
    try:
        r = tool_detect_communities({"algorithm": "louvain"})
        _save_json(r, out_dir / "COMMUNITIES.json")
        results["COMMUNITIES.json"] = {"communities": len(r.get("communities", [])), "modularity": r.get("modularity")}
    except Exception as e:
        results["COMMUNITIES.json"] = {"error": _safe_error(e)}
    # 6. CENTRALITY.json
    try:
        r = tool_get_centrality({"metric": "all", "limit": 50})
        _save_json(r, out_dir / "CENTRALITY.json")
        results["CENTRALITY.json"] = {"metrics": list(r.keys())}
    except Exception as e:
        results["CENTRALITY.json"] = {"error": _safe_error(e)}

    results["output_dir"] = str(out_dir)
    results["files"] = sorted([f.name for f in out_dir.iterdir() if f.is_file()])
    return results


# ============================================================
# Robustez: health check, integrity check, circuit breaker
# ============================================================

# Circuit breaker: contador de falhas por tool
_failure_counts = {}
_failure_windows = {}

def _circuit_breaker_check(tool_name):
    """Retorna True se tool deve ser bloqueada (circuito aberto)."""
    now = time.time()
    # Resetar janela a cada 60s
    if tool_name in _failure_windows and now - _failure_windows[tool_name] > 60:
        _failure_counts.pop(tool_name, None)
        _failure_windows.pop(tool_name, None)
    return _failure_counts.get(tool_name, 0) >= 5

def _circuit_breaker_record_failure(tool_name):
    """Registra falha. Se >= 5 em 60s, circuito abre."""
    now = time.time()
    if tool_name not in _failure_windows:
        _failure_windows[tool_name] = now
    _failure_counts[tool_name] = _failure_counts.get(tool_name, 0) + 1

def _circuit_breaker_record_success(tool_name):
    """Registra sucesso: reseta contador."""
    _failure_counts.pop(tool_name, None)
    _failure_windows.pop(tool_name, None)


def tool_health_check(args):
    """Health check: status do server, DB, telemetria.
    Retorna: status (ok/degraded/critical), db_size, node_count, latency_p50, error_rate."""
    conn = get_db()
    try:
        db_size = os.path.getsize(DB_PATH)
        node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        # Latencia p50 dos ultimos 60s
        cutoff = int(time.time()) - 60
        rows = conn.execute(
            "SELECT duration_ms FROM telemetry_spans WHERE timestamp > datetime(?, 'unixepoch') ORDER BY duration_ms",
            (cutoff,),
        ).fetchall()
        if rows:
            p50 = rows[len(rows) // 2]["duration_ms"]
            errors = conn.execute(
                "SELECT COUNT(*) FROM telemetry_spans WHERE timestamp > datetime(?, 'unixepoch') AND error IS NOT NULL",
                (cutoff,),
            ).fetchone()[0]
            error_rate = (errors / len(rows)) * 100 if rows else 0
        else:
            p50 = 0
            error_rate = 0
        # Integrity check
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        # Status
        status = "ok"
        if error_rate > 50 or integrity != "ok":
            status = "critical"
        elif error_rate > 10 or p50 > 1000:
            status = "degraded"
        return {
            "status": status,
            "db_size_bytes": db_size,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "node_count": node_count,
            "edge_count": edge_count,
            "latency_p50_ms": round(p50, 2),
            "error_rate_pct": round(error_rate, 2),
            "integrity": integrity,
            "circuit_breakers": {k: v for k, v in _failure_counts.items() if v > 0},
        }
    finally:
        conn.close()


def tool_integrity_check(args):
    """Verifica integridade do banco SQLite. Retorna: ok ou lista de problemas."""
    conn = get_db()
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result == "ok":
            # Estatisticas adicionais
            node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            orphan_edges = conn.execute(
                "SELECT COUNT(*) FROM edges e LEFT JOIN nodes n ON e.source_id = n.id WHERE n.id IS NULL"
            ).fetchone()[0]
            return {
                "integrity": "ok",
                "node_count": node_count,
                "edge_count": edge_count,
                "orphan_edges": orphan_edges,
                "warnings": [] if orphan_edges == 0 else [f"{orphan_edges} arestas orfas (source_id sem no)"],
            }
        else:
            return {"integrity": "corrupt", "details": result}
    finally:
        conn.close()


def tool_backup(args):
    """Cria backup manual do banco via VACUUM INTO. Args: output_path? (default: grafo-out/backups/kg-backup-YYYY-MM-DD.db)"""
    backup_dir = Path(DB_PATH).parent / "grafo-out" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d-%H%M%S")
    output_path = args.get("output_path", str(backup_dir / f"kg-backup-{timestamp}.db"))
    output_path = _validate_output_path(output_path, f"kg-backup-{timestamp}.db")
    # Escape aspas simples para previnir SQL injection mesmo se _validate_output_path tiver bug
    safe_path = output_path.replace("'", "''")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(f"VACUUM INTO '{safe_path}'")
        os.chmod(output_path, 0o600)
        return {"backup_path": output_path, "size_bytes": os.path.getsize(output_path)}
    finally:
        conn.close()


# ============================================================
# Task 7: Analise de impacto, caminhos, contexto e telemetria
# ============================================================

def tool_get_impact(args):
    """Blast radius de um no: quais nos seriam afetados se este fosse removido?
    Faz BFS a partir do no, agrupando afetados por distancia (hop 1, hop 2, etc).
    Args: node (id ou qualified_name), max_depth? (default 3, max 5),
          direction? (outgoing/incoming/both, default both)"""
    node_ref = args["node"]
    if not isinstance(node_ref, (int, str)):
        raise ValueError("node deve ser id (int) ou qualified_name (str)")
    max_depth = min(args.get("max_depth", 3), 5)
    if max_depth < 1:
        raise ValueError("max_depth deve ser >= 1")
    direction = args.get("direction", "both")
    if direction not in ("outgoing", "incoming", "both"):
        raise ValueError("direction deve ser: outgoing, incoming ou both")

    conn = get_db()
    try:
        node_id = _resolve_node(conn, node_ref)
        if node_id is None:
            return {"error": f"No nao encontrado: {node_ref}"}

        # Guarda: nao carregar grafo inteiro se for muito grande
        total_edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
        if total_edges > MAX_GRAPH_NODES_FOR_ALGO * 10:
            return {"error": f"Grafo muito grande ({total_edges} arestas). Use max_depth=1 ou reduza o grafo."}

        # Carregar arestas UMA vez, construir adjacencia conforme direction
        rows = conn.execute("SELECT source_id, target_id FROM edges").fetchall()
        adj = {}
        for r in rows:
            if direction != "incoming":
                adj.setdefault(r["source_id"], set()).add(r["target_id"])
            if direction != "outgoing":
                adj.setdefault(r["target_id"], set()).add(r["source_id"])

        # BFS em memoria, agrupando por profundidade
        by_depth = {}
        visited = {node_id}
        queue = deque([(node_id, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for nid in adj.get(current, set()):
                if nid not in visited:
                    visited.add(nid)
                    next_depth = depth + 1
                    by_depth.setdefault(next_depth, []).append(nid)
                    queue.append((nid, next_depth))

        # Resolver info dos nos afetados em 1 query (nao 2)
        affected_ids = set()
        for ids in by_depth.values():
            affected_ids.update(ids)
        node_info = {}
        affected_labels = {}
        if affected_ids:
            if len(affected_ids) > 900:
                affected_ids = set(list(affected_ids)[:900])
            placeholders = ",".join("?" * len(affected_ids))
            for r in conn.execute(
                f"SELECT id, label, name, qualified_name FROM nodes WHERE id IN ({placeholders})",
                list(affected_ids),
            ).fetchall():
                node_info[r["id"]] = {"id": r["id"], "label": r["label"], "name": r["name"], "qualified_name": r["qualified_name"]}
                lbl = r["label"]
                affected_labels[lbl] = affected_labels.get(lbl, 0) + 1

        by_depth_serialized = {str(k): [node_info.get(nid, {"id": nid}) for nid in v] for k, v in sorted(by_depth.items())}

        return {
            "node": node_id,
            "affected_count": len(affected_ids),
            "by_depth": by_depth_serialized,
            "affected_labels": affected_labels,
        }
    finally:
        conn.close()


def tool_trace_paths(args):
    """Multiplos caminhos entre dois nos (DFS iterativo com poda).
    Args: source, target, max_paths? (default 3, max 10), max_hops? (default 8, max 15)"""
    source = args["source"]
    target = args["target"]
    if not isinstance(source, (int, str)):
        raise ValueError("source deve ser id (int) ou qualified_name (str)")
    if not isinstance(target, (int, str)):
        raise ValueError("target deve ser id (int) ou qualified_name (str)")
    max_paths = min(args.get("max_paths", 3), 10)
    if max_paths < 1:
        raise ValueError("max_paths deve ser >= 1")
    max_hops = min(args.get("max_hops", 8), 15)
    if max_hops < 1:
        raise ValueError("max_hops deve ser >= 1")

    conn = get_db()
    try:
        source_id = _resolve_node(conn, source)
        target_id = _resolve_node(conn, target)
        if source_id is None:
            return {"error": f"Source nao encontrado: {source}"}
        if target_id is None:
            return {"error": f"Target nao encontrado: {target}"}
        if source_id == target_id:
            return {"paths": [[source_id]], "count": 1, "truncated": False}

        # Guarda: nao carregar grafo inteiro se for muito grande
        total_edges = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
        if total_edges > MAX_GRAPH_NODES_FOR_ALGO * 10:
            return {"error": f"Grafo muito grande ({total_edges} arestas). Use max_hops menor."}

        # Carregar arestas uma vez (grafo nao-direcional para caminhos)
        rows = conn.execute("SELECT source_id, target_id FROM edges").fetchall()
        adj = {}
        for r in rows:
            adj.setdefault(r["source_id"], set()).add(r["target_id"])
            adj.setdefault(r["target_id"], set()).add(r["source_id"])

        # DFS iterativo com poda: stack de (current, path, visited)
        paths = []
        truncated = False
        stack = [(source_id, [source_id], {source_id})]
        while stack:
            if len(paths) >= max_paths:
                truncated = True
                break
            current, path, vis = stack.pop()
            if len(path) - 1 >= max_hops:
                continue
            for nid in adj.get(current, set()):
                if nid == target_id:
                    paths.append(path + [nid])
                    continue
                if nid not in vis:
                    stack.append((nid, path + [nid], vis | {nid}))

        return {"paths": paths, "count": len(paths), "truncated": truncated}
    finally:
        conn.close()


def tool_explain_node(args):
    """Subgrafo ao redor de um no com contexto: no, vizinhos diretos, arestas entre eles.
    Args: node (id ou qualified_name), depth? (default 1, max 2), limit_neighbors? (default 20, max 50)"""
    node_ref = args["node"]
    if not isinstance(node_ref, (int, str)):
        raise ValueError("node deve ser id (int) ou qualified_name (str)")
    depth = min(args.get("depth", 1), 2)
    if depth < 1:
        raise ValueError("depth deve ser >= 1")
    limit_neighbors = min(args.get("limit_neighbors", 20), 50)
    if limit_neighbors < 1:
        raise ValueError("limit_neighbors deve ser >= 1")

    conn = get_db()
    try:
        node_id = _resolve_node(conn, node_ref)
        if node_id is None:
            return {"error": f"No nao encontrado: {node_ref}"}

        # Detalhes do no (colunas explicitas, nao SELECT *)
        row = conn.execute(
            "SELECT id, label, name, qualified_name, properties, provenance, source FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if not row:
            return {"error": "No nao encontrado"}
        try:
            props = json.loads(row["properties"]) if row["properties"] else {}
        except (json.JSONDecodeError, TypeError):
            props = {}
        node_data = {
            "id": row["id"], "label": row["label"], "name": row["name"],
            "qualified_name": row["qualified_name"],
            "properties": _filter_sensitive_props(props),
            "provenance": row["provenance"], "source": row["source"],
        }

        # Vizinhos diretos (arestas de saida e entrada)
        out_edges = conn.execute(
            """SELECT e.id, e.type, e.provenance, e.weight, e.properties,
                      n.id as target_id, n.label as target_label, n.name as target_name, n.qualified_name as target_qualified_name
               FROM edges e JOIN nodes n ON e.target_id = n.id WHERE e.source_id = ? LIMIT ?""",
            (node_id, limit_neighbors),
        ).fetchall()
        in_edges = conn.execute(
            """SELECT e.id, e.type, e.provenance, e.weight, e.properties,
                      n.id as source_id, n.label as source_label, n.name as source_name, n.qualified_name as source_qualified_name
               FROM edges e JOIN nodes n ON e.source_id = n.id WHERE e.target_id = ? LIMIT ?""",
            (node_id, limit_neighbors),
        ).fetchall()

        def _safe_props(raw):
            try:
                return json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                return {}

        neighbors = {}
        edges = []
        for e in out_edges:
            neighbors[e["target_id"]] = {"id": e["target_id"], "label": e["target_label"], "name": e["target_name"], "qualified_name": e["target_qualified_name"]}
            edges.append({"id": e["id"], "type": e["type"], "provenance": e["provenance"], "weight": e["weight"],
                          "source": node_id, "target": e["target_id"],
                          "properties": _safe_props(e["properties"])})
        for e in in_edges:
            neighbors[e["source_id"]] = {"id": e["source_id"], "label": e["source_label"], "name": e["source_name"], "qualified_name": e["source_qualified_name"]}
            edges.append({"id": e["id"], "type": e["type"], "provenance": e["provenance"], "weight": e["weight"],
                          "source": e["source_id"], "target": node_id,
                          "properties": _safe_props(e["properties"])})

        # Se depth=2, buscar arestas entre os vizinhos (limite de placeholders)
        if depth == 2 and neighbors:
            neighbor_ids = list(neighbors.keys())[:450]  # SQLite limita ~999 params
            if neighbor_ids:
                placeholders = ",".join("?" * len(neighbor_ids))
                inner_edges = conn.execute(
                    f"""SELECT e.id, e.type, e.provenance, e.weight, e.source_id, e.target_id, e.properties
                        FROM edges e WHERE e.source_id IN ({placeholders}) AND e.target_id IN ({placeholders})""",
                    neighbor_ids + neighbor_ids,
                ).fetchall()
                for e in inner_edges:
                    edges.append({"id": e["id"], "type": e["type"], "provenance": e["provenance"], "weight": e["weight"],
                                  "source": e["source_id"], "target": e["target_id"],
                                  "properties": _safe_props(e["properties"])})

        neighbor_list = list(neighbors.values())[:limit_neighbors]
        summary = f"{node_data['name']} ({node_data['label']}) e conectado a {len(neighbor_list)} nos via {len(out_edges) + len(in_edges)} arestas"

        return {
            "node": node_data,
            "neighbors": neighbor_list,
            "edges": edges,
            "summary": summary,
        }
    finally:
        conn.close()


def tool_what_if_remove(args):
    """Simula remocao de um no sem remove-lo. Calcula nos que ficariam isolados,
    arestas perdidas e comunidades afetadas.
    Args: node (id ou qualified_name)"""
    node_ref = args["node"]
    if not isinstance(node_ref, (int, str)):
        raise ValueError("node deve ser id (int) ou qualified_name (str)")

    conn = get_db()
    try:
        node_id = _resolve_node(conn, node_ref)
        if node_id is None:
            return {"error": f"No nao encontrado: {node_ref}"}

        # Arestas que seriam perdidas (CASCADE)
        edges_lost = conn.execute(
            "SELECT COUNT(*) as c FROM edges WHERE source_id = ? OR target_id = ?",
            (node_id, node_id),
        ).fetchone()["c"]

        # Nos que perderiam conexao: vizinhos diretos que ficam isolados sem este no
        neighbor_rows = conn.execute(
            """SELECT DISTINCT n.id FROM nodes n
               JOIN edges e ON (e.source_id = n.id AND e.target_id = ?) OR (e.target_id = n.id AND e.source_id = ?)
               WHERE n.id != ?""",
            (node_id, node_id, node_id),
        ).fetchall()
        neighbor_ids = [r["id"] for r in neighbor_rows][:450]  # Limite de placeholders SQLite

        # Para cada vizinho, verificar se teria outras arestas (sem o no removido)
        nodes_that_lose = []
        if neighbor_ids:
            placeholders = ",".join("?" * len(neighbor_ids))
            for r in conn.execute(
                f"""SELECT n.id,
                    (SELECT COUNT(*) FROM edges e WHERE (e.source_id = n.id OR e.target_id = n.id)
                     AND e.source_id != ? AND e.target_id != ?) as remaining_edges
                    FROM nodes n WHERE n.id IN ({placeholders})""",
                [node_id, node_id] + neighbor_ids,
            ).fetchall():
                if r["remaining_edges"] == 0:
                    nodes_that_lose.append(r["id"])

        # Comunidades afetadas
        comm_row = conn.execute(
            "SELECT community_id FROM communities WHERE node_id = ?", (node_id,)
        ).fetchone()
        communities_affected = []
        if comm_row:
            comm_id = comm_row["community_id"]
            # Quantos nos na mesma comunidade
            comm_size = conn.execute(
                "SELECT COUNT(*) as c FROM communities WHERE community_id = ?", (comm_id,)
            ).fetchone()["c"]
            communities_affected.append({"community_id": comm_id, "size_before": comm_size, "size_after": comm_size - 1})

        # Risk: high se >5 nos isolados ou >10 arestas perdidas, medium se >0, low se 0
        isolated_count = len(nodes_that_lose)
        if isolated_count > 5 or edges_lost > 10:
            isolation_risk = "high"
        elif isolated_count > 0 or edges_lost > 0:
            isolation_risk = "medium"
        else:
            isolation_risk = "low"

        # Resolver nomes dos nos que perdem conexao
        lose_info = []
        if nodes_that_lose:
            nodes_that_lose = nodes_that_lose[:450]  # Limite de placeholders SQLite
            placeholders = ",".join("?" * len(nodes_that_lose))
            for r in conn.execute(
                f"SELECT id, label, name, qualified_name FROM nodes WHERE id IN ({placeholders})",
                nodes_that_lose,
            ).fetchall():
                lose_info.append({"id": r["id"], "label": r["label"], "name": r["name"], "qualified_name": r["qualified_name"]})

        return {
            "node": node_id,
            "edges_lost": edges_lost,
            "nodes_that_lose_connection": lose_info,
            "communities_affected": communities_affected,
            "isolation_risk": isolation_risk,
        }
    finally:
        conn.close()


def tool_replay_trace(args):
    """Reconstroi o fluxo de execucao a partir de telemetry_spans de um trace_id.
    Retorna spans ordenados por timestamp, com duracao acumulada, erros e arvore de chamadas.
    Args: trace_id, limit? (default 100, max 500)"""
    trace_id = args["trace_id"]
    if not isinstance(trace_id, str) or not trace_id:
        raise ValueError("trace_id e obrigatorio e deve ser string")
    limit = min(args.get("limit", 100), 500)
    if limit < 1:
        raise ValueError("limit deve ser >= 1")

    conn = get_db()
    try:
        # Verificar se tabela existe
        try:
            conn.execute("SELECT 1 FROM telemetry_spans LIMIT 1")
        except sqlite3.OperationalError:
            return {"error": "Tabela telemetry_spans nao existe. Rode o schema atualizado."}

        rows = conn.execute(
            """SELECT span_id, parent_id, tool, duration_ms, error, args_summary,
                      agent_id, cost_usd, timestamp
               FROM telemetry_spans WHERE trace_id = ? ORDER BY timestamp LIMIT ?""",
            (trace_id, limit),
        ).fetchall()

        if not rows:
            return {"trace_id": trace_id, "spans": [], "total_duration_ms": 0, "error_count": 0, "call_tree": {}}

        spans = []
        total_duration = 0
        error_count = 0
        for r in rows:
            total_duration += r["duration_ms"] if r["duration_ms"] else 0
            has_error = r["error"] is not None and r["error"] != ""
            if has_error:
                error_count += 1
            spans.append({
                "span_id": r["span_id"], "parent_id": r["parent_id"], "tool": r["tool"],
                "duration_ms": r["duration_ms"], "error": r["error"], "args_summary": r["args_summary"],
                "agent_id": r["agent_id"], "cost_usd": r["cost_usd"], "timestamp": r["timestamp"],
            })

        # Construir arvore de chamadas (parent_id -> children)
        span_map = {s["span_id"]: {**s, "children": []} for s in spans}
        roots = []
        for s in spans:
            if s["parent_id"] and s["parent_id"] in span_map:
                span_map[s["parent_id"]]["children"].append(span_map[s["span_id"]])
            else:
                roots.append(span_map[s["span_id"]])

        call_tree = {"roots": roots} if len(roots) == 1 else {"roots": roots}

        return {
            "trace_id": trace_id,
            "spans": spans,
            "total_duration_ms": round(total_duration, 2),
            "error_count": error_count,
            "call_tree": call_tree,
        }
    finally:
        conn.close()


def tool_get_impact_summary(args):
    """Resume o impacto de um tipo de aresta no grafo: quantos nos dependem dessa relacao,
    quais labels sao mais afetados.
    Args: edge_type, limit? (default 20)"""
    edge_type = args["edge_type"]
    validate_edge_type(edge_type)
    limit = min(args.get("limit", 20), 100)
    if limit < 1:
        raise ValueError("limit deve ser >= 1")

    conn = get_db()
    try:
        # Total de arestas desse tipo
        total_edges = conn.execute(
            "SELECT COUNT(*) as c FROM edges WHERE type = ?", (edge_type,)
        ).fetchone()["c"]

        if total_edges == 0:
            return {"edge_type": edge_type, "total_edges": 0, "source_labels": [], "target_labels": [], "affected_nodes": 0}

        # Labels dos sources
        source_labels = conn.execute(
            """SELECT n.label, COUNT(*) as c FROM edges e
               JOIN nodes n ON e.source_id = n.id WHERE e.type = ?
               GROUP BY n.label ORDER BY c DESC LIMIT ?""",
            (edge_type, limit),
        ).fetchall()

        # Labels dos targets
        target_labels = conn.execute(
            """SELECT n.label, COUNT(*) as c FROM edges e
               JOIN nodes n ON e.target_id = n.id WHERE e.type = ?
               GROUP BY n.label ORDER BY c DESC LIMIT ?""",
            (edge_type, limit),
        ).fetchall()

        # Nos afetados (distintos: sources + targets)
        affected = conn.execute(
            """SELECT COUNT(DISTINCT nid) as c FROM (
                SELECT source_id as nid FROM edges WHERE type = ?
                UNION
                SELECT target_id as nid FROM edges WHERE type = ?
            )""",
            (edge_type, edge_type),
        ).fetchone()["c"]

        return {
            "edge_type": edge_type,
            "total_edges": total_edges,
            "source_labels": [{"label": r["label"], "count": r["c"]} for r in source_labels],
            "target_labels": [{"label": r["label"], "count": r["c"]} for r in target_labels],
            "affected_nodes": affected,
        }
    finally:
        conn.close()


def tool_find_orphans(args):
    """Encontra nos sem arestas (isolados) e arestas com provenance AMBIGUOUS.
    Args: limit? (default 50, max 200)"""
    limit = args.get("limit", 50)
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("limit deve ser inteiro >= 1")
    limit = min(limit, 200)

    conn = get_db()
    try:
        # Nos isolados: sem arestas de saida nem entrada
        orphan_nodes = conn.execute(
            """SELECT n.id, n.label, n.name, n.qualified_name, n.provenance
               FROM nodes n
               WHERE n.id NOT IN (SELECT source_id FROM edges)
                 AND n.id NOT IN (SELECT target_id FROM edges)
               LIMIT ?""",
            (limit,),
        ).fetchall()

        # Arestas com provenance AMBIGUOUS
        ambiguous_edges = conn.execute(
            """SELECT e.id, e.type, e.provenance, e.weight,
                      ns.label as source_label, ns.name as source_name,
                      nt.label as target_label, nt.name as target_name
               FROM edges e
               JOIN nodes ns ON e.source_id = ns.id
               JOIN nodes nt ON e.target_id = nt.id
               WHERE e.provenance = 'AMBIGUOUS'
               LIMIT ?""",
            (limit,),
        ).fetchall()

        return {
            "orphan_nodes": [
                {"id": r["id"], "label": r["label"], "name": r["name"],
                 "qualified_name": r["qualified_name"], "provenance": r["provenance"]}
                for r in orphan_nodes
            ],
            "ambiguous_edges": [
                {"id": r["id"], "type": r["type"], "provenance": r["provenance"], "weight": r["weight"],
                 "source": {"label": r["source_label"], "name": r["source_name"]},
                 "target": {"label": r["target_label"], "name": r["target_name"]}}
                for r in ambiguous_edges
            ],
            "orphan_count": len(orphan_nodes),
            "ambiguous_count": len(ambiguous_edges),
        }
    finally:
        conn.close()


# ============================================================
# Task 8: Tools de codigo (AST parsing, como graphify mas leve)
# Usa ast module do Python stdlib (zero dependencias)
# ============================================================

def _safe_json_loads_list(raw):
    """json.loads com fallback seguro para lista vazia."""
    if not raw:
        return []
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_python_file(filepath):
    """Faz parse de um arquivo Python via ast module.
    Retorna: {functions: [{name, lineno, args, decorators}], classes: [{name, lineno, bases, methods}],
              imports: [{module, names, lineno}], calls: [{caller, func, lineno}]}
    Leve: usa ast.parse do stdlib, sem LLM, sem tree-sitter."""
    import ast as _ast
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = _ast.parse(source, filename=filepath)
    except (SyntaxError, ValueError, OSError):
        return None

    functions = []
    classes = []
    imports = []
    calls = []

    class _Visitor(_ast.NodeVisitor):
        def __init__(self):
            self.current_class = None
            self.current_func = None

        def visit_FunctionDef(self, node):
            decos = [_ast.unparse(d) if hasattr(_ast, 'unparse') else '' for d in node.decorator_list]
            func_info = {
                "name": node.name,
                "lineno": node.lineno,
                "args": [a.arg for a in node.args.args],
                "decorators": decos,
                "class": self.current_class,
            }
            functions.append(func_info)
            old_func = self.current_func
            self.current_func = node.name
            self.generic_visit(node)
            self.current_func = old_func

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            bases = [_ast.unparse(b) if hasattr(_ast, 'unparse') else str(getattr(b, 'id', '')) for b in node.bases]
            classes.append({
                "name": node.name,
                "lineno": node.lineno,
                "bases": bases,
                "methods": [n.name for n in node.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))],
            })
            old_class = self.current_class
            self.current_class = node.name
            self.generic_visit(node)
            self.current_class = old_class

        def visit_Import(self, node):
            for alias in node.names:
                imports.append({"module": alias.name, "names": [], "lineno": node.lineno, "from": False})

        def visit_ImportFrom(self, node):
            module = node.module or ""
            names = [alias.name for alias in node.names]
            imports.append({"module": module, "names": names, "lineno": node.lineno, "from": True})

        def visit_Call(self, node):
            try:
                func_name = _ast.unparse(node.func) if hasattr(_ast, 'unparse') else ''
            except Exception:
                func_name = ''
            if func_name and self.current_func:
                calls.append({
                    "caller": self.current_func,
                    "func": func_name,
                    "lineno": node.lineno,
                    "class": self.current_class,
                })
            self.generic_visit(node)

    _Visitor().visit(tree)
    return {"functions": functions, "classes": classes, "imports": imports, "calls": calls}


def tool_scan_codebase(args):
    """Mapeia um diretorio de codigo Python via ast module (zero deps, sem LLM).
    Extrai functions, classes, imports, calls e adiciona como nos/arestas no grafo.
    Args: path (diretorio raiz do projeto), max_files? (default 200, max 1000),
          exclude? (lista de dirs para ignorar, default: __pycache__, .git, venv, node_modules)"""
    import ast as _ast  # ja importado em _parse_python_file, mas explicito aqui
    path = args["path"]
    if not isinstance(path, str) or not path:
        raise ValueError("path e obrigatorio e deve ser string")
    if len(path) > 4096:
        raise ValueError("path muito longo (max 4096 chars)")
    max_files = min(args.get("max_files", 200), 1000)
    if max_files < 1:
        raise ValueError("max_files deve ser >= 1")
    exclude = set(args.get("exclude", ["__pycache__", ".git", "venv", ".venv", "node_modules", ".tox", "build", "dist"]))
    if not os.path.isdir(path):
        return {"error": f"Diretorio nao encontrado: {path}"}

    # Defesa contra path traversal: resolver path real e nao seguir symlinks
    resolved_path = os.path.realpath(path)
    if not resolved_path.startswith("/home/"):
        return {"error": "Path deve estar dentro de /home/ (defesa contra path traversal)"}

    # Coletar arquivos .py
    py_files = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in exclude]
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(root, f))
                if len(py_files) >= max_files:
                    break
        if len(py_files) >= max_files:
            break

    if not py_files:
        return {"error": "Nenhum arquivo .py encontrado", "path": path}

    project_name = os.path.basename(os.path.abspath(path))
    proj_qname = f"proj:{normalize_name(project_name)}"

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Upsert do no do projeto
        proj_row = conn.execute("SELECT id FROM nodes WHERE qualified_name = ?", (proj_qname,)).fetchone()
        if proj_row:
            proj_id = proj_row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO nodes (label, name, qualified_name, properties, provenance, source) VALUES (?, ?, ?, ?, ?, ?)",
                ("Project", project_name, proj_qname, json.dumps({"path": path}), "EXTRACTED", "scan_codebase"),
            )
            proj_id = cur.lastrowid
            audit_log(conn, "node_create", "node", proj_id, "Project", proj_qname, "scan_codebase")

        stats = {"files": 0, "functions": 0, "classes": 0, "imports": 0, "calls": 0, "errors": 0}
        file_nodes = {}  # filepath -> node_id

        for filepath in py_files:
            rel_path = os.path.relpath(filepath, path)
            file_qname = f"file:{normalize_name(project_name)}:{rel_path.replace('/', ':')}"
            # Upsert file node
            row = conn.execute("SELECT id FROM nodes WHERE qualified_name = ?", (file_qname,)).fetchone()
            if row:
                file_id = row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO nodes (label, name, qualified_name, properties, provenance, source) VALUES (?, ?, ?, ?, ?, ?)",
                    ("File", rel_path, file_qname, json.dumps({"path": filepath}), "EXTRACTED", "scan_codebase"),
                )
                file_id = cur.lastrowid
                audit_log(conn, "node_create", "node", file_id, "File", file_qname, "scan_codebase")
            file_nodes[filepath] = file_id
            # Edge: project CONTAINS file
            conn.execute(
                "INSERT OR IGNORE INTO edges (source_id, target_id, type, provenance) VALUES (?, ?, 'CONTAINS', 'EXTRACTED')",
                (proj_id, file_id),
            )

            # Parse do arquivo
            parsed = _parse_python_file(filepath)
            if parsed is None:
                stats["errors"] += 1
                continue
            stats["files"] += 1

            # Functions
            func_node_ids = {}
            for func in parsed["functions"]:
                func_qname = f"func:{normalize_name(project_name)}:{rel_path.replace('/', ':')}:{func['name']}"
                func_props = json.dumps({
                    "lineno": func["lineno"],
                    "args": func["args"],
                    "decorators": func["decorators"],
                    "class": func["class"],
                })
                row = conn.execute("SELECT id FROM nodes WHERE qualified_name = ?", (func_qname,)).fetchone()
                if row:
                    fid = row["id"]
                else:
                    cur = conn.execute(
                        "INSERT INTO nodes (label, name, qualified_name, properties, provenance, source) VALUES (?, ?, ?, ?, ?, ?)",
                        ("Function", func["name"], func_qname, func_props, "EXTRACTED", "scan_codebase"),
                    )
                    fid = cur.lastrowid
                    audit_log(conn, "node_create", "node", fid, "Function", func_qname, "scan_codebase")
                func_node_ids[func["name"]] = fid
                # Edge: file DEFINES_FUNC function
                conn.execute(
                    "INSERT OR IGNORE INTO edges (source_id, target_id, type, provenance) VALUES (?, ?, 'DEFINES_FUNC', 'EXTRACTED')",
                    (file_id, fid),
                )
                stats["functions"] += 1

            # Classes
            for cls in parsed["classes"]:
                cls_qname = f"class:{normalize_name(project_name)}:{rel_path.replace('/', ':')}:{cls['name']}"
                cls_props = json.dumps({"lineno": cls["lineno"], "bases": cls["bases"], "methods": cls["methods"]})
                row = conn.execute("SELECT id FROM nodes WHERE qualified_name = ?", (cls_qname,)).fetchone()
                if row:
                    cid = row["id"]
                else:
                    cur = conn.execute(
                        "INSERT INTO nodes (label, name, qualified_name, properties, provenance, source) VALUES (?, ?, ?, ?, ?, ?)",
                        ("Class", cls["name"], cls_qname, cls_props, "EXTRACTED", "scan_codebase"),
                    )
                    cid = cur.lastrowid
                    audit_log(conn, "node_create", "node", cid, "Class", cls_qname, "scan_codebase")
                # Edge: file DEFINES_CLASS class
                conn.execute(
                    "INSERT OR IGNORE INTO edges (source_id, target_id, type, provenance) VALUES (?, ?, 'DEFINES_CLASS', 'EXTRACTED')",
                    (file_id, cid),
                )
                # Edge: class INHERITS_FROM base (se houver)
                for base in cls["bases"]:
                    base_qname = f"class:{normalize_name(base)}"
                    base_row = conn.execute("SELECT id FROM nodes WHERE qualified_name = ?", (base_qname,)).fetchone()
                    if base_row:
                        conn.execute(
                            "INSERT OR IGNORE INTO edges (source_id, target_id, type, provenance) VALUES (?, ?, 'INHERITS_FROM', 'INFERRED')",
                            (cid, base_row["id"]),
                        )
                stats["classes"] += 1

            # Imports
            for imp in parsed["imports"]:
                imp_qname = f"import:{normalize_name(project_name)}:{rel_path.replace('/', ':')}:{normalize_name(imp['module'])}"
                imp_props = json.dumps({"module": imp["module"], "names": imp["names"], "lineno": imp["lineno"], "from": imp["from"]})
                row = conn.execute("SELECT id FROM nodes WHERE qualified_name = ?", (imp_qname,)).fetchone()
                if row:
                    iid = row["id"]
                else:
                    cur = conn.execute(
                        "INSERT INTO nodes (label, name, qualified_name, properties, provenance, source) VALUES (?, ?, ?, ?, ?, ?)",
                        ("Import", imp["module"], imp_qname, imp_props, "EXTRACTED", "scan_codebase"),
                    )
                    iid = cur.lastrowid
                    audit_log(conn, "node_create", "node", iid, "Import", imp_qname, "scan_codebase")
                # Edge: file IMPORTS_FROM module
                conn.execute(
                    "INSERT OR IGNORE INTO edges (source_id, target_id, type, provenance) VALUES (?, ?, 'IMPORTS_FROM', 'EXTRACTED')",
                    (file_id, iid),
                )
                stats["imports"] += 1

            # Calls (function -> function)
            for call in parsed["calls"]:
                caller_id = func_node_ids.get(call["caller"])
                if caller_id is None:
                    continue
                if not call.get("func"):
                    continue
                # Buscar se a funcao chamada existe no mesmo projeto
                called_name = call["func"].split(".")[-1]  # ex: os.path.join -> join
                called_qname = f"func:{normalize_name(project_name)}:{called_name}"
                called_row = conn.execute(
                    "SELECT id FROM nodes WHERE qualified_name = ? OR name = ?",
                    (called_qname, called_name),
                ).fetchone()
                if called_row and called_row["id"] != caller_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO edges (source_id, target_id, type, provenance) VALUES (?, ?, 'CALLS_FUNC', 'EXTRACTED')",
                        (caller_id, called_row["id"]),
                    )
                    stats["calls"] += 1

        conn.commit()
        return {"project": proj_qname, "project_id": proj_id, "stats": stats, "files_scanned": len(py_files)}
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def tool_get_call_graph(args):
    """Gera grafo de chamadas: quem chama quem.
    Args: project? (qualified_name do projeto, default: todos),
          direction? (outgoing/incoming/both, default both),
          limit? (default 100, max 500)"""
    project = args.get("project")
    if project is not None and not isinstance(project, str):
        raise ValueError("project deve ser string ou omitido")
    direction = args.get("direction", "both")
    if direction not in ("outgoing", "incoming", "both"):
        raise ValueError("direction deve ser: outgoing, incoming ou both")
    limit = min(args.get("limit", 100), 500)
    if limit < 1:
        raise ValueError("limit deve ser >= 1")

    conn = get_db()
    try:
        # Filtrar por projeto se especificado
        where_clause = ""
        params = []
        if project:
            where_clause = " AND EXISTS (SELECT 1 FROM edges e2 JOIN nodes pf ON e2.source_id = pf.id WHERE e2.target_id = n.id AND pf.qualified_name = ? AND e2.type = 'DEFINES_FUNC')"
            params = [project]

        # Buscar functions que tem CALLS_FUNC edges
        if direction in ("outgoing", "both"):
            out_edges = conn.execute(
                f"""SELECT n.id, n.name, n.qualified_name, e.target_id, tn.name as target_name, tn.qualified_name as target_qname
                    FROM nodes n
                    JOIN edges e ON e.source_id = n.id AND e.type = 'CALLS_FUNC'
                    JOIN nodes tn ON e.target_id = tn.id
                    WHERE n.label = 'Function'{where_clause}
                    LIMIT ?""",
                params + [limit],
            ).fetchall()
        else:
            out_edges = []

        if direction in ("incoming", "both"):
            in_edges = conn.execute(
                f"""SELECT n.id, n.name, n.qualified_name, e.source_id, sn.name as source_name, sn.qualified_name as source_qname
                    FROM nodes n
                    JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS_FUNC'
                    JOIN nodes sn ON e.source_id = sn.id
                    WHERE n.label = 'Function'{where_clause}
                    LIMIT ?""",
                params + [limit],
            ).fetchall()
        else:
            in_edges = []

        callers = {}
        callees = {}
        for r in out_edges:
            callees.setdefault(r["id"], {"name": r["name"], "qualified_name": r["qualified_name"], "calls": []})
            callees[r["id"]]["calls"].append({"id": r["target_id"], "name": r["target_name"], "qualified_name": r["target_qname"]})
        for r in in_edges:
            callers.setdefault(r["id"], {"name": r["name"], "qualified_name": r["qualified_name"], "called_by": []})
            callers[r["id"]]["called_by"].append({"id": r["source_id"], "name": r["source_name"], "qualified_name": r["source_qname"]})

        return {
            "outgoing": list(callees.values())[:limit],
            "incoming": list(callers.values())[:limit],
            "total_outgoing": len(out_edges),
            "total_incoming": len(in_edges),
        }
    finally:
        conn.close()


def tool_get_import_graph(args):
    """Gera grafo de imports: quais arquivos importam quais modulos.
    Args: project? (qualified_name do projeto, default: todos),
          limit? (default 100, max 500)"""
    project = args.get("project")
    if project is not None and not isinstance(project, str):
        raise ValueError("project deve ser string ou omitido")
    limit = min(args.get("limit", 100), 500)
    if limit < 1:
        raise ValueError("limit deve ser >= 1")

    conn = get_db()
    try:
        where_clause = ""
        params = []
        if project:
            where_clause = " AND EXISTS (SELECT 1 FROM edges e2 JOIN nodes pf ON e2.source_id = pf.id WHERE e2.target_id = n.id AND pf.qualified_name = ? AND e2.type IN ('CONTAINS', 'DEFINES_FUNC'))"
            params = [project]

        edges = conn.execute(
            f"""SELECT n.id as file_id, n.name as file_name, n.qualified_name as file_qname,
                      i.id as import_id, i.name as module, i.qualified_name as import_qname,
                      json_extract(i.properties, '$.names') as imported_names
                FROM nodes n
                JOIN edges e ON e.source_id = n.id AND e.type = 'IMPORTS_FROM'
                JOIN nodes i ON e.target_id = i.id
                WHERE n.label = 'File'{where_clause}
                LIMIT ?""",
            params + [limit],
        ).fetchall()

        imports = {}
        for r in edges:
            file_qname = r["file_qname"]
            if file_qname not in imports:
                imports[file_qname] = {"file": r["file_name"], "qualified_name": file_qname, "imports": []}
            imports[file_qname]["imports"].append({
                "module": r["module"],
                "names": _safe_json_loads_list(r["imported_names"]),
            })

        return {
            "files": list(imports.values())[:limit],
            "total_imports": len(edges),
        }
    finally:
        conn.close()


def tool_find_circular_imports(args):
    """Detecta imports circulares no grafo de codigo.
    Faz DFS no grafo de IMPORTS_FROM procurando ciclos.
    Args: project? (qualified_name do projeto, default: todos),
          max_depth? (default 10, max 20)"""
    project = args.get("project")
    if project is not None and not isinstance(project, str):
        raise ValueError("project deve ser string ou omitido")
    max_depth = min(args.get("max_depth", 10), 20)
    if max_depth < 2:
        raise ValueError("max_depth deve ser >= 2")

    conn = get_db()
    try:
        # Carregar grafo de imports (File -> Import -> File que define o modulo)
        # Se project especificado, filtrar para files desse projeto
        if project:
            rows = conn.execute(
                """SELECT e.source_id as file_id, i.name as module, i.properties
                   FROM edges e JOIN nodes i ON e.target_id = i.id
                   WHERE e.type = 'IMPORTS_FROM'
                     AND EXISTS (
                       SELECT 1 FROM edges e2 JOIN nodes pf ON e2.source_id = pf.id
                       WHERE e2.target_id = e.source_id AND pf.qualified_name = ?
                         AND e2.type = 'CONTAINS'
                     )""",
                (project,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT e.source_id as file_id, i.name as module, i.properties
                   FROM edges e JOIN nodes i ON e.target_id = i.id
                   WHERE e.type = 'IMPORTS_FROM'""",
            ).fetchall()

        # Mapear: module_name -> [file_ids que definem esse modulo]
        module_to_files = {}
        file_to_modules = {}
        for r in rows:
            file_to_modules.setdefault(r["file_id"], set()).add(r["module"])
            module_to_files.setdefault(r["module"], set()).add(r["file_id"])

        # Para cada file, quais outros files ele importa (via modulo)
        adj = {}
        for file_id, modules in file_to_modules.items():
            for mod in modules:
                for target_file in module_to_files.get(mod, set()):
                    if target_file != file_id:
                        adj.setdefault(file_id, set()).add(target_file)

        # DFS para detectar ciclos
        cycles = []
        visited = set()
        for start in adj:
            if start in visited:
                continue
            stack = [(start, [start], {start})]
            while stack:
                current, path, vis = stack.pop()
                if len(path) > max_depth:
                    continue
                for neighbor in adj.get(current, set()):
                    if neighbor in vis:
                        # Ciclo encontrado: achar onde comeca no path
                        if neighbor in path:
                            cycle_start = path.index(neighbor)
                            cycle = path[cycle_start:] + [neighbor]
                            if len(cycle) > 1:
                                cycles.append(cycle)
                    elif neighbor not in visited:
                        stack.append((neighbor, path + [neighbor], vis | {neighbor}))
                visited.add(current)

        # Resolver nomes dos files nos ciclos
        cycle_results = []
        seen_cycles = set()
        for cycle in cycles:
            cycle_key = tuple(sorted(set(cycle)))
            if cycle_key in seen_cycles:
                continue
            seen_cycles.add(cycle_key)
            if len(cycle) > 1:
                cycle_ids = list(set(cycle))
                # Validar que cycle_ids sao inteiros (defesa contra SQL injection)
                if not all(isinstance(c, int) for c in cycle_ids):
                    continue
                placeholders = ",".join("?" * len(cycle_ids))
                file_names = {}
                for r in conn.execute(
                    f"SELECT id, name, qualified_name FROM nodes WHERE id IN ({placeholders})",
                    cycle_ids,
                ).fetchall():
                    file_names[r["id"]] = {"name": r["name"], "qualified_name": r["qualified_name"]}
                cycle_results.append([file_names.get(fid, {"id": fid}) for fid in cycle])

        return {
            "cycles": cycle_results[:50],
            "cycle_count": len(cycle_results),
        }
    finally:
        conn.close()


def tool_get_code_impact(args):
    """Blast radius de uma funcao: se mudar esta funcao, quais outras sao afetadas?
    Faz BFS no grafo de CALLS_FUNC a partir da funcao.
    Args: function (id ou qualified_name), max_depth? (default 3, max 5)"""
    func_ref = args["function"]
    if not isinstance(func_ref, (int, str)):
        raise ValueError("function deve ser id (int) ou qualified_name (str)")
    max_depth = min(args.get("max_depth", 3), 5)
    if max_depth < 1:
        raise ValueError("max_depth deve ser >= 1")

    conn = get_db()
    try:
        func_id = _resolve_node(conn, func_ref)
        if func_id is None:
            return {"error": f"Funcao nao encontrada: {func_ref}"}

        # Verificar que e uma Function
        row = conn.execute("SELECT label, name FROM nodes WHERE id = ?", (func_id,)).fetchone()
        if not row or row["label"] != "Function":
            return {"error": f"No nao e uma funcao: {func_ref} (label: {row['label'] if row else '?'})"}

        # BFS no grafo de CALLS_FUNC (outgoing: quem esta funcao chama)
        # e incoming (quem chama esta funcao)
        by_depth = {}
        visited = {func_id}
        queue = deque([(func_id, 0)])

        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            # Quem esta funcao chama (outgoing)
            out_rows = conn.execute(
                "SELECT target_id FROM edges WHERE source_id = ? AND type = 'CALLS_FUNC'",
                (current,),
            ).fetchall()
            # Quem chama esta funcao (incoming)
            in_rows = conn.execute(
                "SELECT source_id FROM edges WHERE target_id = ? AND type = 'CALLS_FUNC'",
                (current,),
            ).fetchall()

            for r in list(out_rows) + list(in_rows):
                nid = r[0]
                if nid not in visited:
                    visited.add(nid)
                    next_depth = depth + 1
                    by_depth.setdefault(next_depth, []).append(nid)
                    queue.append((nid, next_depth))

        # Resolver nomes
        affected_ids = set()
        for ids in by_depth.values():
            affected_ids.update(ids)
        node_info = {}
        if affected_ids:
            if len(affected_ids) > 900:
                affected_ids = set(list(affected_ids)[:900])
            placeholders = ",".join("?" * len(affected_ids))
            for r in conn.execute(
                f"SELECT id, label, name, qualified_name FROM nodes WHERE id IN ({placeholders})",
                list(affected_ids),
            ).fetchall():
                node_info[r["id"]] = {"id": r["id"], "label": r["label"], "name": r["name"], "qualified_name": r["qualified_name"]}

        by_depth_serialized = {str(k): [node_info.get(nid, {"id": nid}) for nid in v] for k, v in sorted(by_depth.items())}

        return {
            "function": {"id": func_id, "name": row["name"]},
            "affected_count": len(affected_ids),
            "by_depth": by_depth_serialized,
        }
    finally:
        conn.close()

TOOLS = {
    "list_projects": {"fn": tool_list_projects, "desc": "Lista grafos disponiveis e estatisticas"},
    "get_graph_schema": {"fn": tool_get_graph_schema, "desc": "Lista labels e tipos de aresta validos para criar nos/arestas"},
    "add_node": {"fn": tool_add_node, "desc": "Cria um no no grafo. Args: label, name, qualified_name?, properties?, provenance?, source?"},
    "upsert_node": {"fn": tool_upsert_node, "desc": "Cria ou atualiza um no (busca por qualified_name)"},
    "add_edge": {"fn": tool_add_edge, "desc": "Cria uma aresta. Args: source (id ou qualified_name), target (id ou qualified_name), type, properties?, provenance?, weight?"},
    "add_nodes_batch": {"fn": tool_add_nodes_batch, "desc": "Cria multiplos nos. Args: {nodes: [{label, name, ...}]}"},
    "add_edges_batch": {"fn": tool_add_edges_batch, "desc": "Cria multiplas arestas. Args: {edges: [{source, target, type, ...}]}"},
    "delete_node": {"fn": tool_delete_node, "desc": "Deleta um no e suas arestas. Args: id ou qualified_name"},
    "set_community": {"fn": tool_set_community, "desc": "Atribui um no a uma comunidade. Args: node_id, community_id, algorithm?"},
    "search_graph": {"fn": tool_search_graph, "desc": "Busca nos por padrao de nome e/ou label. Args: name_pattern?, label?, limit?"},
    "get_node": {"fn": tool_get_node, "desc": "Detalhes de um no com vizinhos. Args: id ou qualified_name"},
    "trace_path": {"fn": tool_trace_path, "desc": "Caminho mais curto entre dois nos (BFS). Args: source, target, max_hops?"},
    "get_architecture": {"fn": tool_get_architecture, "desc": "Visao geral do grafo: labels, tipos, contagens, nos mais conectados"},
    "query_graph": {"fn": tool_query_graph, "desc": "Executa query SQL read-only no grafo. Args: query, limit?"},
    "export_json": {"fn": tool_export_json, "desc": "Exporta grafo completo como JSON (compativel com Obsidian/D3.js)"},
    "export_html": {"fn": tool_export_html, "desc": "Gera graph.html interativo (vis.js CDN, CSS inline, cores por label, tooltips, filter). Args: output_path?"},
    "detect_communities": {"fn": tool_detect_communities, "desc": "Detecta comunidades automaticamente (Louvain em Python puro). Args: algorithm? (louvain/connected_components), resolution?"},
    "get_centrality": {"fn": tool_get_centrality, "desc": "Calcula centralidade: degree, betweenness, closeness, pagerank. Args: metric? (all/degree/betweenness/closeness/pagerank), limit?"},
    "get_telemetry": {"fn": tool_get_telemetry, "desc": "Metricas de telemetria: latencia p50/p90, top tools, erros. Args: window? (minutos), limit?"},
    "generate_report": {"fn": tool_generate_report, "desc": "Gera GRAPH_REPORT.md: god nodes, surprising connections, suggested questions. Args: output_path?"},
    "export_all": {"fn": tool_export_all, "desc": "Gera todos os artefatos em grafo-out/: graph.html, graph.json, GRAPH_REPORT.md, HEALTH.json, COMMUNITIES.json, CENTRALITY.json. Args: output_dir?"},
    "health_check": {"fn": tool_health_check, "desc": "Health check: status (ok/degraded/critical), db_size, node_count, latency_p50, error_rate, integrity"},
    "integrity_check": {"fn": tool_integrity_check, "desc": "Verifica integridade do banco SQLite. Retorna: ok ou lista de problemas"},
    "backup": {"fn": tool_backup, "desc": "Cria backup manual do banco via VACUUM INTO. Args: output_path?"},
    "get_impact": {"fn": tool_get_impact, "desc": "Blast radius de um no: nos afetados agrupados por distancia (BFS). Args: node (id ou qualified_name), max_depth? (default 3, max 5), direction? (outgoing/incoming/both)"},
    "trace_paths": {"fn": tool_trace_paths, "desc": "Multiplos caminhos entre dois nos (DFS iterativo). Args: source, target, max_paths? (default 3, max 10), max_hops? (default 8, max 15)"},
    "explain_node": {"fn": tool_explain_node, "desc": "Subgrafo ao redor de um no com contexto: vizinhos, arestas e propriedades. Args: node (id ou qualified_name), depth? (default 1, max 2), limit_neighbors? (default 20, max 50)"},
    "what_if_remove": {"fn": tool_what_if_remove, "desc": "Simula remocao de um no: arestas perdidas, nos isolados, comunidades afetadas. Args: node (id ou qualified_name)"},
    "replay_trace": {"fn": tool_replay_trace, "desc": "Reconstroi fluxo de execucao de um trace_id: spans ordenados, duracao, erros, arvore de chamadas. Args: trace_id, limit? (default 100, max 500)"},
    "get_impact_summary": {"fn": tool_get_impact_summary, "desc": "Resume impacto de um tipo de aresta: nos dependentes, labels afetados. Args: edge_type, limit? (default 20)"},
    "find_orphans": {"fn": tool_find_orphans, "desc": "Encontra nos isolados (sem arestas) e arestas com provenance AMBIGUOUS. Args: limit? (default 50, max 200)"},
    # Tools de codigo (AST parsing, como graphify mas leve)
    "scan_codebase": {"fn": tool_scan_codebase, "desc": "Mapeia diretorio de codigo Python via ast module (zero deps). Extrai functions, classes, imports, calls. Args: path, max_files? (default 200), exclude?"},
    "get_call_graph": {"fn": tool_get_call_graph, "desc": "Grafo de chamadas: quem chama quem. Args: project? (qualified_name), direction? (outgoing/incoming/both), limit? (default 100)"},
    "get_import_graph": {"fn": tool_get_import_graph, "desc": "Grafo de imports: quais arquivos importam quais modulos. Args: project?, limit? (default 100)"},
    "find_circular_imports": {"fn": tool_find_circular_imports, "desc": "Detecta imports circulares via DFS no grafo de imports. Args: project?, max_depth? (default 10)"},
    "get_code_impact": {"fn": tool_get_code_impact, "desc": "Blast radius de uma funcao: se mudar esta funcao, quais outras sao afetadas. Args: function (id ou qualified_name), max_depth? (default 3)"},
}


def handle_request(request):
    """Processa uma requisicao JSON-RPC."""
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "kg-infra", "version": "1.0.0"},
            },
        }
    elif method == "notifications/initialized":
        return None  # notification, sem resposta
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "tools": [
                    {"name": name, "description": t["desc"], "inputSchema": {"type": "object"}}
                    for name, t in TOOLS.items()
                ]
            },
        }
    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        if tool_name not in TOOLS:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Tool nao encontrada: {tool_name}"},
            }
        # Circuit breaker: se tool falhou >= 5x em 60s, retornar erro rapido
        if _circuit_breaker_check(tool_name):
            error_json = json.dumps({
                "error": f"Tool {tool_name} temporariamente bloqueada (circuito aberto: 5+ falhas em 60s)",
                "hint": "Aguarde 60s e tente novamente, ou use health_check para diagnosticar."
            }, ensure_ascii=False)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": error_json}], "isError": True},
            }
        # Tracing: medir latencia e registrar span
        trace_id = str(req_id) if req_id is not None else str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        start = time.time()
        try:
            result = TOOLS[tool_name]["fn"](tool_args)
            _circuit_breaker_record_success(tool_name)
            duration_ms = (time.time() - start) * 1000
            result_json = json.dumps(result, ensure_ascii=False)
            # Registrar span de telemetria (sem dados sensiveis nos args)
            args_summary = _redact_pii(json.dumps({k: ("..." if isinstance(v, str) and len(v) > 100 else v) for k, v in tool_args.items()}, ensure_ascii=False))
            try:
                conn = get_db()
                # Incluir agent_id e cost_usd se presentes nos args (migracao condicional)
                extra_cols = ""
                extra_vals = []
                if "agent_id" in tool_args:
                    extra_cols += ", agent_id"
                    extra_vals.append(tool_args["agent_id"])
                if "cost_usd" in tool_args:
                    extra_cols += ", cost_usd"
                    extra_vals.append(tool_args["cost_usd"])
                conn.execute(
                    f"INSERT INTO telemetry_spans (trace_id, span_id, tool, duration_ms, args_summary, result_size{extra_cols}) VALUES (?, ?, ?, ?, ?, ?{',?' * len(extra_vals)})",
                    (trace_id, span_id, tool_name, round(duration_ms, 2), args_summary, len(result_json), *extra_vals),
                )
                conn.commit()
                conn.close()
            except sqlite3.OperationalError:
                pass  # tabela telemetry_spans ou colunas podem nao existir em DBs antigos
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": result_json}]},
            }
        except (ValueError, KeyError) as e:
            # Erros de validacao: retornar isError para LLM se recuperar
            _circuit_breaker_record_failure(tool_name)
            duration_ms = (time.time() - start) * 1000
            try:
                conn = get_db()
                conn.execute(
                    "INSERT INTO telemetry_spans (trace_id, span_id, tool, duration_ms, error, args_summary) VALUES (?, ?, ?, ?, ?, ?)",
                    (trace_id, span_id, tool_name, round(duration_ms, 2), str(e)[:500], _redact_pii(json.dumps(tool_args, ensure_ascii=False))),
                )
                conn.commit()
                conn.close()
            except sqlite3.OperationalError:
                pass
            error_json = json.dumps({"error": str(e), "hint": "Verifique os argumentos e tente novamente."}, ensure_ascii=False)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": error_json}], "isError": True},
            }
        except sqlite3.Error as e:
            # Erros de DB: retornar isError com dica de recuperacao
            _circuit_breaker_record_failure(tool_name)
            duration_ms = (time.time() - start) * 1000
            try:
                conn = get_db()
                conn.execute(
                    "INSERT INTO telemetry_spans (trace_id, span_id, tool, duration_ms, error, args_summary) VALUES (?, ?, ?, ?, ?, ?)",
                    (trace_id, span_id, tool_name, round(duration_ms, 2), str(e)[:500], _redact_pii(json.dumps(tool_args, ensure_ascii=False))),
                )
                conn.commit()
                conn.close()
            except sqlite3.OperationalError:
                pass
            error_json = json.dumps({"error": f"Erro de banco: {e}", "hint": "Pode ser lock temporario. Aguarde e tente novamente."}, ensure_ascii=False)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": error_json}], "isError": True},
            }
        except Exception as e:
            # Catch-all: erros inesperados (MemoryError, OSError, etc)
            _circuit_breaker_record_failure(tool_name)
            duration_ms = (time.time() - start) * 1000
            try:
                conn = get_db()
                conn.execute(
                    "INSERT INTO telemetry_spans (trace_id, span_id, tool, duration_ms, error, args_summary) VALUES (?, ?, ?, ?, ?, ?)",
                    (trace_id, span_id, tool_name, round(duration_ms, 2), f"{type(e).__name__}: {str(e)[:400]}", _redact_pii(json.dumps(tool_args, ensure_ascii=False))),
                )
                conn.commit()
                conn.close()
            except sqlite3.OperationalError:
                pass
            error_json = json.dumps({"error": f"Erro interno: {type(e).__name__}", "hint": "Reinicie o MCP server se persistir."}, ensure_ascii=False)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": error_json}], "isError": True},
            }
    elif method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}
    elif method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}
    else:
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method nao encontrado: {method}"},
        }


def _graceful_shutdown(signum=None, frame=None):
    """Shutdown graceful: checkpoint WAL, fechar conexoes, sair."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception:
        pass
    sys.exit(0)


def _backup_db():
    """Backup automatico via VACUUM INTO. Rotacao de backups antigos."""
    backup_dir = Path(DB_PATH).parent / "grafo-out" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Remover backups antigos
    cutoff = time.time() - (BACKUP_RETENTION_DAYS * 86400)
    for f in backup_dir.glob("kg-backup-*.db"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
    # Backup de hoje (se nao existir)
    today = time.strftime("%Y-%m-%d")
    backup_path = backup_dir / f"kg-backup-{today}.db"
    if not backup_path.exists():
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(f"VACUUM INTO '{backup_path}'")
            conn.close()
            os.chmod(backup_path, 0o600)
        except sqlite3.Error:
            pass


def _idle_alarm_handler(signum, frame):
    """Handler para idle timeout: se sem input por IDLE_TIMEOUT_S, sair."""
    sys.exit(0)


def main():
    """Loop principal: le JSON-RPC do stdin, escreve respostas no stdout.
    Robustez: signal handling, stdin EOF detection, idle timeout, backup automatico."""
    # Garante que o schema existe (idempotente: CREATE IF NOT EXISTS / INSERT OR IGNORE)
    # Roda sempre para aplicar novos indexes em DBs existentes
    schema_path = Path(DB_PATH).parent / "schema.sql"
    if schema_path.exists():
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(schema_path.read_text())
        # Migracao: adicionar colunas novas em telemetry_spans para DBs existentes
        # SQLite nao tem IF NOT EXISTS em ALTER TABLE, então checar PRAGMA antes
        # Allowlist defensiva: col e coltype sao hardcoded, mas validamos por seguranca
        _MIGRATION_COLS = {"agent_id": "TEXT", "cost_usd": "REAL", "checkpoint": "TEXT"}
        try:
            existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(telemetry_spans)").fetchall()}
        except sqlite3.OperationalError:
            existing_cols = set()  # tabela nao existe ainda
        for col, coltype in _MIGRATION_COLS.items():
            if col not in existing_cols and col in _MIGRATION_COLS:
                try:
                    conn.execute(f"ALTER TABLE telemetry_spans ADD COLUMN {col} {coltype}")
                except sqlite3.OperationalError:
                    pass  # ja existe (race) ou erro temporario
        conn.commit()
        conn.close()

    # Signal handlers: SIGTERM/SIGINT -> graceful shutdown
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
    # SIGALRM -> idle timeout (self-terminate se sem input por IDLE_TIMEOUT_S)
    signal.signal(signal.SIGALRM, _idle_alarm_handler)

    # Backup automatico na inicializacao
    _backup_db()

    for line in sys.stdin:
        # Resetar idle timer a cada linha recebida
        signal.alarm(IDLE_TIMEOUT_S)
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    # stdin EOF: cliente fechou, sair gracefully
    signal.alarm(0)  # cancelar idle timer
    _graceful_shutdown()


if __name__ == "__main__":
    main()
