#!/usr/bin/env python3
"""Seed completo: mapeia toda a infraestrutura real do usuario no grafo de negocio.
Nao usa dados sinteticos. Indexa projetos, arquivos, servicos, MCPs, skills, containers.
Roda: python3 seed_full.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from server import (
    tool_add_nodes_batch, tool_add_edges_batch, tool_get_architecture,
    normalize_name, DB_PATH, get_db,
)


def qn(label, name):
    return f"{label.lower()}:{normalize_name(name)}"


def collect_nodes_edges():
    nodes = []
    edges = []

    # ========== 1. INFRAESTRUTURA: Docker containers ==========
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            name, image, status = parts[0], parts[1], parts[2]
            ports = parts[3] if len(parts) > 3 else ""
            nodes.append({
                "label": "Service", "name": name,
                "qualified_name": qn("Service", name),
                "provenance": "EXTRACTED", "source": "docker",
                "properties": {"image": image, "status": status, "ports": ports}
            })
    except Exception:
        pass

    # ========== 2. PROJETOS ==========
    projetos = [
        {"name": "kg-infra", "path": "/home/vsf/Projetos/kg-infra",
         "desc": "MCP server: knowledge graph de infraestrutura, 24 tools, Python puro"},
        {"name": "pesquisa", "path": "/home/vsf/Projetos/pesquisa",
         "desc": "Pesquisas: SDD/TDD/ODD, grafos, RAG, seguranca, arquitetura"},
        {"name": "seguranca", "path": "/home/vsf/.seguranca",
         "desc": "Pipeline de seguranca: sandbox, ClamAV, SearXNG, downloads seguros"},
        {"name": "owl-regent-studio", "path": "/home/vsf/.seguranca/github-audit/owl-regent-studio",
         "desc": "Pipeline de assets premium: 5 fases, 64 skills, QA V1-V19, design-review-gate"},
        {"name": "friendly-web-studio", "path": "/home/vsf/.seguranca/github-audit/friendly-web-studio",
         "desc": "Agencia web: scrapers, design-brain, clientes, 11 skills"},
        {"name": "mega-operacao", "path": "/home/vsf/.seguranca/github-audit/mega-operacao",
         "desc": "Workspace unificado: vendas, criacao, marketing, automacao"},
    ]
    for p in projetos:
        nodes.append({
            "label": "Project", "name": p["name"],
            "qualified_name": f"proj:{normalize_name(p['name'])}",
            "provenance": "EXTRACTED", "source": "filesystem",
            "properties": {"path": p["path"], "description": p["desc"]}
        })

    # ========== 3. ARQUIVOS DE CADA PROJETO ==========
    ext_map = {
        ".py": "Module", ".md": "Document", ".sql": "Module",
        ".json": "Config", ".html": "File", ".sh": "Module",
        ".yml": "Config", ".yaml": "Config", ".toml": "Config",
        ".txt": "Document", ".csv": "File",
    }
    ignore_dirs = {"__pycache__", ".git", "node_modules", "grafo-out", "backups",
                   ".venv", "venv", "dist", "build", ".cache", "site-packages"}
    ignore_files = {"graph.html", "graph.json", "GRAPH_REPORT.md",
                    "CENTRALITY.json", "COMMUNITIES.json", "HEALTH.json"}
    MAX_DEPTH = 3  # limitar profundidade para evitar paths enormes

    for proj in projetos:
        proj_path = Path(proj["path"])
        if not proj_path.exists():
            continue
        proj_qn = f"proj:{normalize_name(proj['name'])}"

        for root, dirs, files in os.walk(proj_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            depth = len(Path(root).relative_to(proj_path).parts)
            if depth > MAX_DEPTH:
                dirs.clear()
                continue

            # Diretorios como nos (ate profundidade MAX_DEPTH)
            if depth > 0 and depth <= MAX_DEPTH:
                dir_name = os.path.basename(root)
                dir_rel = str(Path(root).relative_to(proj_path))
                dir_qn = f"dir:{normalize_name(proj['name'])}:{normalize_name(dir_rel)}"
                nodes.append({
                    "label": "Folder", "name": dir_name,
                    "qualified_name": dir_qn,
                    "provenance": "EXTRACTED", "source": "filesystem",
                    "properties": {"path": dir_rel, "project": proj["name"]}
                })
                # Edge: projeto CONTAINS diretorio (ou diretorio pai CONTAINS subdiretorio)
                parent_depth = depth - 1
                if parent_depth == 0:
                    edges.append({"source": proj_qn, "target": dir_qn, "type": "CONTAINS"})
                else:
                    parent_rel = str(Path(root).parent.relative_to(proj_path))
                    parent_qn = f"dir:{normalize_name(proj['name'])}:{normalize_name(parent_rel)}"
                    edges.append({"source": parent_qn, "target": dir_qn, "type": "CONTAINS"})

            # Arquivos como nos
            for f in files:
                if f in ignore_files:
                    continue
                ext = Path(f).suffix.lower()
                if ext not in ext_map:
                    continue
                file_path = os.path.join(root, f)
                rel_path = str(Path(file_path).relative_to(proj_path))
                file_label = ext_map[ext]
                file_qn = f"file:{normalize_name(proj['name'])}:{normalize_name(rel_path)}"

                # Propriedades do arquivo
                props = {"path": rel_path, "project": proj["name"], "extension": ext}
                try:
                    props["size_bytes"] = os.path.getsize(file_path)
                    props["lines"] = sum(1 for _ in open(file_path, errors="ignore"))
                except Exception:
                    pass

                nodes.append({
                    "label": file_label, "name": f,
                    "qualified_name": file_qn,
                    "provenance": "EXTRACTED", "source": "filesystem",
                    "properties": props
                })

                # Edge: diretorio/projeto CONTAINS arquivo
                if depth == 0:
                    edges.append({"source": proj_qn, "target": file_qn, "type": "CONTAINS"})
                else:
                    dir_rel = str(Path(root).relative_to(proj_path))
                    dir_qn = f"dir:{normalize_name(proj['name'])}:{normalize_name(dir_rel)}"
                    edges.append({"source": dir_qn, "target": file_qn, "type": "CONTAINS"})

    # ========== 4. MCP SERVERS ==========
    mcps = [
        {"name": "codebase-memory-mcp", "path": "/home/vsf/.local/bin/codebase-memory-mcp",
         "type": "binary", "desc": "Grafo de codigo: indexa repos, busca semantica, trace_path"},
        {"name": "kg-infra", "path": "/home/vsf/Projetos/kg-infra/server.py",
         "type": "python", "desc": "Grafo de negocio: 24 tools, SQLite, Louvain, PageRank"},
    ]
    for m in mcps:
        nodes.append({
            "label": "Service", "name": m["name"],
            "qualified_name": f"mcp:{normalize_name(m['name'])}",
            "provenance": "EXTRACTED", "source": "config",
            "properties": {"path": m["path"], "type": m["type"], "description": m["desc"],
                          "protocol": "JSON-RPC 2.0 over stdio"}
        })
        # Edge: MCP EXPOSES_MCP projeto (se aplicavel)
        if m["name"] == "kg-infra":
            edges.append({
                "source": f"proj:{normalize_name('kg-infra')}",
                "target": f"mcp:{normalize_name(m['name'])}",
                "type": "EXPOSES_MCP"
            })

    # ========== 5. SKILLS ==========
    skills_dir = Path("/home/vsf/.config/devin/skills")
    skill_meta = {
        "kg-infra": "Guia de uso das 24 tools MCP do grafo de negocio",
        "robustness-audit": "Audita robustez: 8 categorias, gera relatorio",
        "design-viz": "Design de visualizacoes: paleta perceptual, anti-AI-slop",
        "data-storytelling": "Narrativa de dados: tese, causality, 9 estruturas",
        "deep-research": "Pesquisa profunda: SearXNG + Crossref + OpenAlex + sandbox",
        "supply-chain": "Verifica cadeia de suprimentos: SLSA, SBOM, SSDF",
        "secure-code": "Checklist OWASP Proactive Controls 2024",
        "threat-model": "Modelagem de ameacas: STRIDE, Zero Trust",
        "tdd": "Test-Driven Development: Red-Green-Refactor",
        "sdd": "Spec-Driven Development: spec antes do codigo",
        "odd": "Observability-Driven Development: OpenTelemetry",
        "autoresearch": "Loop de otimizacao autonoma: testa, mantem, descarta",
        "improve": "Auditor senior read-only: mapeia, encontra oportunidades",
        "ponytail": "Review minimalista: 3 bem, 3 simples, 0-3 cortadas",
        "kanban": "Orquestra fluxo de tarefas com WIP limit 1",
        "test": "Estrategias de teste: unit, integration, e2e",
    }
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        desc = skill_meta.get(skill_name, "Skill do Devin")
        props = {"path": str(skill_file), "description": desc}
        try:
            props["size_bytes"] = skill_file.stat().st_size
            props["lines"] = sum(1 for _ in open(skill_file, errors="ignore"))
        except Exception:
            pass
        nodes.append({
            "label": "Document", "name": skill_name,
            "qualified_name": f"skill:{normalize_name(skill_name)}",
            "provenance": "EXTRACTED", "source": "filesystem",
            "properties": props
        })
        # Edge: skill DOCUMENTS projeto (se aplicavel)
        if skill_name == "kg-infra":
            edges.append({
                "source": f"skill:{normalize_name(skill_name)}",
                "target": f"proj:{normalize_name('kg-infra')}",
                "type": "DOCUMENTS"
            })
        elif skill_name == "robustness-audit":
            edges.append({
                "source": f"skill:{normalize_name(skill_name)}",
                "target": f"proj:{normalize_name('kg-infra')}",
                "type": "DOCUMENTS",
                "provenance": "INFERRED"
            })

    # ========== 6. CONFIGS ==========
    configs = [
        {"name": "devin-config", "path": "/home/vsf/.config/devin/config.json",
         "desc": "Config do Devin CLI: model, hooks, org"},
        {"name": "devin-mcp-config", "path": "/home/vsf/.config/devin/mcp_config.json",
         "desc": "MCP servers do Devin: codebase-memory + kg-infra"},
        {"name": "gemini-mcp-config", "path": "/home/vsf/.gemini/config/mcp_config.json",
         "desc": "MCP servers do Gemini CLI"},
        {"name": "gemini-settings", "path": "/home/vsf/.gemini/settings.json",
         "desc": "Settings do Gemini CLI: hooks, context"},
        {"name": "agents-global", "path": "/home/vsf/.config/devin/AGENTS.md",
         "desc": "Regras globais: seguranca, factualidade, codigo, 4 gates"},
    ]
    for c in configs:
        if not Path(c["path"]).exists():
            continue
        nodes.append({
            "label": "Config", "name": c["name"],
            "qualified_name": f"config:{normalize_name(c['name'])}",
            "provenance": "EXTRACTED", "source": "filesystem",
            "properties": {"path": c["path"], "description": c["desc"]}
        })

    # Edge: configs INTEGRATES_WITH MCPs
    edges.append({
        "source": f"config:{normalize_name('devin-mcp-config')}",
        "target": f"mcp:{normalize_name('codebase-memory-mcp')}",
        "type": "CONFIGURES"
    })
    edges.append({
        "source": f"config:{normalize_name('devin-mcp-config')}",
        "target": f"mcp:{normalize_name('kg-infra')}",
        "type": "CONFIGURES"
    })
    edges.append({
        "source": f"config:{normalize_name('gemini-mcp-config')}",
        "target": f"mcp:{normalize_name('codebase-memory-mcp')}",
        "type": "CONFIGURES"
    })
    edges.append({
        "source": f"config:{normalize_name('gemini-mcp-config')}",
        "target": f"mcp:{normalize_name('kg-infra')}",
        "type": "CONFIGURES"
    })

    # ========== 7. SERVICOS DOCKER -> PROJETOS ==========
    docker_proj_map = {
        "searxng": "seguranca",
        "clamav-scanner": "seguranca",
        "clamav-watcher": "seguranca",
        "sandbox-download": "seguranca",
    }
    for container, proj in docker_proj_map.items():
        container_qn = qn("Service", container)
        proj_qn = f"proj:{normalize_name(proj)}"
        # Verificar se o no existe
        if any(n["qualified_name"] == container_qn for n in nodes):
            edges.append({"source": proj_qn, "target": container_qn, "type": "RUNS"})

    # ========== 8. CONEXOES ENTRE PROJETOS ==========
    # kg-infra USES codebase-memory
    edges.append({
        "source": f"proj:{normalize_name('kg-infra')}",
        "target": f"mcp:{normalize_name('codebase-memory-mcp')}",
        "type": "USES",
        "provenance": "INFERRED"
    })
    # pesquisa-profunda DOCUMENTS kg-infra
    edges.append({
        "source": f"proj:{normalize_name('pesquisa-profunda')}",
        "target": f"proj:{normalize_name('kg-infra')}",
        "type": "DOCUMENTS",
        "provenance": "INFERRED"
    })
    # seguranca MONITORS kg-infra (pipeline de downloads seguros)
    edges.append({
        "source": f"proj:{normalize_name('seguranca')}",
        "target": f"proj:{normalize_name('kg-infra')}",
        "type": "MONITORS",
        "provenance": "INFERRED"
    })

    # ========== 9. ARQUIVOS IMPORTANTES -> CONEXOES SEMANTICAS ==========
    # server.py EXPOSES_MCP kg-infra
    server_qn = f"file:{normalize_name('kg-infra')}:{normalize_name('server.py')}"
    if any(n["qualified_name"] == server_qn for n in nodes):
        edges.append({
            "source": server_qn,
            "target": f"mcp:{normalize_name('kg-infra')}",
            "type": "IMPLEMENTS"
        })
        # test_kg_infra.py TESTS server.py
        test_qn = f"file:{normalize_name('kg-infra')}:{normalize_name('test_kg_infra.py')}"
        if any(n["qualified_name"] == test_qn for n in nodes):
            edges.append({"source": test_qn, "target": server_qn, "type": "TESTS"})
        # cli.py USES server.py
        cli_qn = f"file:{normalize_name('kg-infra')}:{normalize_name('cli.py')}"
        if any(n["qualified_name"] == cli_qn for n in nodes):
            edges.append({"source": cli_qn, "target": server_qn, "type": "USES"})
        # export.py USES server.py
        export_qn = f"file:{normalize_name('kg-infra')}:{normalize_name('export.py')}"
        if any(n["qualified_name"] == export_qn for n in nodes):
            edges.append({"source": export_qn, "target": server_qn, "type": "USES"})
        # seed.py USES server.py
        seed_qn = f"file:{normalize_name('kg-infra')}:{normalize_name('seed.py')}"
        if any(n["qualified_name"] == seed_qn for n in nodes):
            edges.append({"source": seed_qn, "target": server_qn, "type": "USES"})
        # schema.sql DEFINES server.py
        schema_qn = f"file:{normalize_name('kg-infra')}:{normalize_name('schema.sql')}"
        if any(n["qualified_name"] == schema_qn for n in nodes):
            edges.append({"source": schema_qn, "target": server_qn, "type": "DEFINES"})
        # README.md DOCUMENTS server.py
        readme_qn = f"file:{normalize_name('kg-infra')}:{normalize_name('README.md')}"
        if any(n["qualified_name"] == readme_qn for n in nodes):
            edges.append({"source": readme_qn, "target": server_qn, "type": "DOCUMENTS"})
        # THREAT-MODEL.md DOCUMENTS server.py
        threat_qn = f"file:{normalize_name('kg-infra')}:{normalize_name('THREAT-MODEL.md')}"
        if any(n["qualified_name"] == threat_qn for n in nodes):
            edges.append({"source": threat_qn, "target": server_qn, "type": "DOCUMENTS",
                         "provenance": "INFERRED"})
        # REFERENCIA.md DOCUMENTS server.py
        ref_qn = f"file:{normalize_name('kg-infra')}:{normalize_name('REFERENCIA.md')}"
        if any(n["qualified_name"] == ref_qn for n in nodes):
            edges.append({"source": ref_qn, "target": server_qn, "type": "DOCUMENTS"})

    # ========== 10. SCRIPTS DE SEGURANCA ==========
    scripts_dir = Path("/home/vsf/.seguranca/scripts")
    if scripts_dir.exists():
        for script in sorted(scripts_dir.glob("*.sh")):
            nodes.append({
                "label": "Module", "name": script.name,
                "qualified_name": f"script:{normalize_name(script.name)}",
                "provenance": "EXTRACTED", "source": "filesystem",
                "properties": {"path": str(script), "project": "seguranca",
                              "type": "shell-script"}
            })
            # Edge: seguranca CONTAINS script
            edges.append({
                "source": f"proj:{normalize_name('seguranca')}",
                "target": f"script:{normalize_name(script.name)}",
                "type": "CONTAINS"
            })

    return nodes, edges


def main():
    # Limpar banco atual
    print("Limpando banco atual...")
    conn = get_db()
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM nodes")
    conn.execute("DELETE FROM communities")
    conn.commit()
    conn.close()

    print("Coletando nos e arestas da infraestrutura real...")
    nodes, edges = collect_nodes_edges()

    print(f"  Nos coletados: {len(nodes)}")
    print(f"  Arestas coletadas: {len(edges)}")

    # Inserir em batches
    print("Inserindo nos...")
    BATCH = 200
    for i in range(0, len(nodes), BATCH):
        batch = nodes[i:i+BATCH]
        result = tool_add_nodes_batch({"nodes": batch})
        if i == 0:
            print(f"  Primeiro batch: {result}")

    print("Inserindo arestas...")
    for i in range(0, len(edges), BATCH):
        batch = edges[i:i+BATCH]
        result = tool_add_edges_batch({"edges": batch})
        if i == 0:
            print(f"  Primeiro batch: {result}")

    # Resumo
    arch = tool_get_architecture({})
    print(f"\nGrafo populado:")
    print(f"  Total nos: {arch['total_nodes']}")
    print(f"  Total arestas: {arch['total_edges']}")
    print(f"  Labels: {[(l['label'], l['count']) for l in arch['node_labels']]}")
    print(f"  Tipos de aresta: {[(e['type'], e['count']) for e in arch['edge_types']]}")
    print(f"  Provenance: {arch['provenance']}")
    print(f"\nTop 5 nos por degree:")
    for n in arch['top_connected_nodes'][:5]:
        print(f"  {n['name']} ({n['label']}): degree {n['degree']}")


if __name__ == "__main__":
    main()
