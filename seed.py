#!/usr/bin/env python3
"""Cria dados sinteticos de exemplo: vendas + atendimento + conteudo integrados.
Cenario: empresa SaaS B2B com 3 clientes, produtos, tickets, artigos de marketing.
Demonstra como o grafo conecta vendas, atendimento e conteudo em uma visao 360.
"""
import json
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from server import tool_add_node, tool_add_edge, tool_add_nodes_batch, tool_add_edges_batch

# NOS: vendas
customers = [
    {"label": "Customer", "name": "Acme Corp", "properties": {"industry": "tech", "size": "enterprise", "mrr": 4990, "since": "2024-01-15"}, "source": "crm"},
    {"label": "Customer", "name": "TechFlow Ltda", "properties": {"industry": "fintech", "size": "mid-market", "mrr": 1990, "since": "2024-06-01"}, "source": "crm"},
    {"label": "Customer", "name": "StartupXYZ", "properties": {"industry": "saas", "size": "startup", "mrr": 290, "since": "2025-03-01"}, "source": "crm"},
]

contacts = [
    {"label": "Contact", "name": "Joao Silva", "properties": {"role": "CTO", "email": "joao@acme.com"}, "source": "crm"},
    {"label": "Contact", "name": "Maria Santos", "properties": {"role": "VP Eng", "email": "maria@acme.com"}, "source": "crm"},
    {"label": "Contact", "name": "Carlos Lima", "properties": {"role": "CEO", "email": "carlos@techflow.com"}, "source": "crm"},
]

products = [
    {"label": "Product", "name": "Plano Enterprise", "properties": {"price": 4990, "billing": "monthly", "seats": "unlimited"}, "source": "catalog"},
    {"label": "Product", "name": "Plano Pro", "properties": {"price": 1990, "billing": "monthly", "seats": 50}, "source": "catalog"},
    {"label": "Product", "name": "Plano Starter", "properties": {"price": 290, "billing": "monthly", "seats": 5}, "source": "catalog"},
]

deals = [
    {"label": "Deal", "name": "Acme Renewal Q3", "properties": {"value": 59880, "stage": "closed-won", "close_date": "2025-07-01"}, "source": "crm"},
    {"label": "Deal", "name": "TechFlow Upsell", "properties": {"value": 23880, "stage": "negotiation", "close_date": "2025-09-01"}, "source": "crm"},
    {"label": "Deal", "name": "StartupXYZ Onboarding", "properties": {"value": 3480, "stage": "closed-won", "close_date": "2025-03-01"}, "source": "crm"},
]

# NOS: atendimento
tickets = [
    {"label": "Ticket", "name": "TKT-001 Login lento", "properties": {"priority": "P2", "status": "resolved", "channel": "email"}, "source": "zendesk"},
    {"label": "Ticket", "name": "TKT-002 Integracao API quebrada", "properties": {"priority": "P1", "status": "open", "channel": "chat"}, "source": "zendesk"},
    {"label": "Ticket", "name": "TKT-003 Duvido faturamento", "properties": {"priority": "P3", "status": "resolved", "channel": "email"}, "source": "zendesk"},
]

agents = [
    {"label": "Agent", "name": "Ana Atendente", "properties": {"team": "suporte", "level": "L2"}, "source": "system"},
    {"label": "Agent", "name": "Bruno Support", "properties": {"team": "suporte", "level": "L1"}, "source": "system"},
]

# NOS: conteudo/marketing
articles = [
    {"label": "Article", "name": "Como reduzir custo de infra com SaaS", "properties": {"type": "blog", "publish_date": "2025-06-01", "views": 3200}, "source": "marketing"},
    {"label": "Article", "name": "Guia de migracao para cloud", "properties": {"type": "ebook", "publish_date": "2025-05-15", "downloads": 850}, "source": "marketing"},
]

topics = [
    {"label": "Topic", "name": "Reducao de custo", "source": "marketing"},
    {"label": "Topic", "name": "Migracao cloud", "source": "marketing"},
    {"label": "Topic", "name": "Integracao API", "source": "marketing"},
]

campaigns = [
    {"label": "Campaign", "name": "Campanha Q3 Enterprise", "properties": {"budget": 50000, "channel": "linkedin", "start": "2025-07-01"}, "source": "marketing"},
]

# Inserir todos os nos
all_nodes = customers + contacts + products + deals + tickets + agents + articles + topics + campaigns
print(f"Inserindo {len(all_nodes)} nos...")
result = tool_add_nodes_batch({"nodes": all_nodes})
print(f"  Criados: {result['created']}")

# Mapear qualified_names para referencia
def qn(label, name):
    from server import normalize_name
    return f"{label.lower()}:{normalize_name(name)}"

# ARESTAS: vendas
sales_edges = [
    # contacts works_at customer
    {"source": qn("Contact", "Joao Silva"), "target": qn("Customer", "Acme Corp"), "type": "works_at"},
    {"source": qn("Contact", "Maria Santos"), "target": qn("Customer", "Acme Corp"), "type": "works_at"},
    {"source": qn("Contact", "Carlos Lima"), "target": qn("Customer", "TechFlow Ltda"), "type": "works_at"},
    # customer bought product
    {"source": qn("Customer", "Acme Corp"), "target": qn("Product", "Plano Enterprise"), "type": "bought", "properties": {"since": "2024-01-15"}},
    {"source": qn("Customer", "TechFlow Ltda"), "target": qn("Product", "Plano Pro"), "type": "bought", "properties": {"since": "2024-06-01"}},
    {"source": qn("Customer", "StartupXYZ"), "target": qn("Product", "Plano Starter"), "type": "bought", "properties": {"since": "2025-03-01"}},
    # deal proposed_to customer
    {"source": qn("Deal", "Acme Renewal Q3"), "target": qn("Customer", "Acme Corp"), "type": "proposed_to"},
    {"source": qn("Deal", "TechFlow Upsell"), "target": qn("Customer", "TechFlow Ltda"), "type": "proposed_to"},
    {"source": qn("Deal", "StartupXYZ Onboarding"), "target": qn("Customer", "StartupXYZ"), "type": "proposed_to"},
    # deal about product
    {"source": qn("Deal", "Acme Renewal Q3"), "target": qn("Product", "Plano Enterprise"), "type": "about"},
    {"source": qn("Deal", "TechFlow Upsell"), "target": qn("Product", "Plano Pro"), "type": "about"},
    {"source": qn("Deal", "StartupXYZ Onboarding"), "target": qn("Product", "Plano Starter"), "type": "about"},
]

# ARESTAS: atendimento
support_edges = [
    # customer opened_ticket ticket
    {"source": qn("Customer", "Acme Corp"), "target": qn("Ticket", "TKT-001 Login lento"), "type": "opened_ticket"},
    {"source": qn("Customer", "TechFlow Ltda"), "target": qn("Ticket", "TKT-002 Integracao API quebrada"), "type": "opened_ticket"},
    {"source": qn("Customer", "StartupXYZ"), "target": qn("Ticket", "TKT-003 Duvido faturamento"), "type": "opened_ticket"},
    # ticket complained_about product
    {"source": qn("Ticket", "TKT-001 Login lento"), "target": qn("Product", "Plano Enterprise"), "type": "complained_about"},
    {"source": qn("Ticket", "TKT-002 Integracao API quebrada"), "target": qn("Product", "Plano Pro"), "type": "complained_about"},
    {"source": qn("Ticket", "TKT-003 Duvido faturamento"), "target": qn("Product", "Plano Starter"), "type": "complained_about"},
    # ticket resolved_by agent
    {"source": qn("Ticket", "TKT-001 Login lento"), "target": qn("Agent", "Ana Atendente"), "type": "resolved_by"},
    {"source": qn("Ticket", "TKT-003 Duvido faturamento"), "target": qn("Agent", "Bruno Support"), "type": "resolved_by"},
    # ticket assigned_to agent (mesmo os abertos)
    {"source": qn("Ticket", "TKT-002 Integracao API quebrada"), "target": qn("Agent", "Ana Atendente"), "type": "assigned_to", "provenance": "INFERRED"},
]

# ARESTAS: conteudo/marketing
content_edges = [
    # article about topic
    {"source": qn("Article", "Como reduzir custo de infra com SaaS"), "target": qn("Topic", "Reducao de custo"), "type": "about"},
    {"source": qn("Article", "Guia de migracao para cloud"), "target": qn("Topic", "Migracao cloud"), "type": "about"},
    {"source": qn("Article", "Guia de migracao para cloud"), "target": qn("Topic", "Integracao API"), "type": "about", "provenance": "INFERRED"},
    # campaign targets customer
    {"source": qn("Campaign", "Campanha Q3 Enterprise"), "target": qn("Customer", "Acme Corp"), "type": "targets"},
    {"source": qn("Campaign", "Campanha Q3 Enterprise"), "target": qn("Customer", "TechFlow Ltda"), "type": "targets"},
    # campaign published_in article
    {"source": qn("Campaign", "Campanha Q3 Enterprise"), "target": qn("Article", "Como reduzir custo de infra com SaaS"), "type": "published_in"},
    # ticket mentioned_in article (cross-domain: ticket sobre API -> artigo sobre API)
    {"source": qn("Ticket", "TKT-002 Integracao API quebrada"), "target": qn("Article", "Guia de migracao para cloud"), "type": "mentioned_in", "provenance": "INFERRED"},
]

all_edges = sales_edges + support_edges + content_edges
print(f"Inserindo {len(all_edges)} arestas...")
result = tool_add_edges_batch({"edges": all_edges})
print(f"  Criadas: {result['created']}")

# Resumo
from server import tool_get_architecture
arch = tool_get_architecture({})
print(f"\nGrafo criado:")
print(f"  Total nos: {arch['total_nodes']}")
print(f"  Total arestas: {arch['total_edges']}")
print(f"  Labels: {[l['label'] for l in arch['node_labels']]}")
print(f"  Tipos de aresta: {[e['type'] for e in arch['edge_types']]}")
print(f"  Provenance: {arch['provenance']}")
print("\nExemplo de query: python3 cli.py trace_path '{\"source\":\"customer:acme-corp\",\"target\":\"article:guia-de-migracao-para-cloud\"}'")
