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
# Protocolo MCP (JSON-RPC 2.0 over stdio)
# ============================================================

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
                conn.execute(
                    "INSERT INTO telemetry_spans (trace_id, span_id, tool, duration_ms, args_summary, result_size) VALUES (?, ?, ?, ?, ?, ?)",
                    (trace_id, span_id, tool_name, round(duration_ms, 2), args_summary, len(result_json)),
                )
                conn.commit()
                conn.close()
            except sqlite3.OperationalError:
                pass  # tabela telemetry_spans pode nao existir em DBs antigos
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
