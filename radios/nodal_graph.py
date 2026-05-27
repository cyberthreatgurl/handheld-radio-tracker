import re
from collections import defaultdict

from django.db.models import Q

from .models import Brand


def _normalize(value):
    return re.sub(r'[^a-z0-9]+', '', (value or '').strip().lower())


def _brand_identity_keys(brand):
    keys = set()
    for value in (brand.name, brand.alias, brand.full_name):
        key = _normalize(value)
        if key:
            keys.add(key)
    return keys


def build_nodal_graph_data(radios_queryset, max_radios=400):
    """
    Build a graph payload (nodes + edges) from radios and brand relationships.

    Node types:
    - brand
    - radio
    - fcc

    Edge types:
    - parent_brand
    - manufactures
    - markets
    - shares_fcc
    - white_label_chain
    """
    radios = list(
        radios_queryset.select_related('manufacturer').order_by('-updated_at')[:max_radios]
    )
    if not radios:
        return {
            'nodes': [],
            'edges': [],
            'stats': {
                'brand_nodes': 0,
                'radio_nodes': 0,
                'fcc_nodes': 0,
                'total_edges': 0,
            },
        }

    manufacturer_ids = {radio.manufacturer_id for radio in radios if radio.manufacturer_id}
    brand_tokens = {radio.brand.strip() for radio in radios if (radio.brand or '').strip()}

    involved_brands = list(
        Brand.objects.filter(
            Q(id__in=manufacturer_ids)
            | Q(name__in=brand_tokens)
            | Q(alias__in=brand_tokens)
            | Q(full_name__in=brand_tokens)
        ).select_related('parent_brand')
    )

    parent_ids = {
        brand.parent_brand_id
        for brand in involved_brands
        if brand.parent_brand_id
    }
    if parent_ids:
        parent_brands = list(Brand.objects.filter(id__in=parent_ids).select_related('parent_brand'))
        brand_map = {brand.id: brand for brand in involved_brands}
        for parent in parent_brands:
            brand_map[parent.id] = parent
        involved_brands = list(brand_map.values())

    brand_by_key = {}
    for brand in involved_brands:
        for key in _brand_identity_keys(brand):
            brand_by_key[key] = brand

    fcc_groups = defaultdict(list)
    for radio in radios:
        fcc = (radio.fcc_id or '').strip().upper()
        if fcc:
            fcc_groups[fcc].append(radio)

    nodes = []
    edges = []
    seen_nodes = set()
    seen_edges = set()

    def add_node(node_id, payload):
        if node_id in seen_nodes:
            return
        seen_nodes.add(node_id)
        data = {'id': node_id}
        data.update(payload)
        nodes.append(data)

    def add_edge(source, target, relation, payload=None):
        edge_key = (source, target, relation)
        if edge_key in seen_edges:
            return
        seen_edges.add(edge_key)
        data = {
            'source': source,
            'target': target,
            'relation': relation,
        }
        if payload:
            data.update(payload)
        edges.append(data)

    for brand in involved_brands:
        brand_node_id = f'brand:{brand.id}'
        add_node(
            brand_node_id,
            {
                'type': 'brand',
                'label': brand.name,
                'alias': brand.alias or '',
                'grantee_code': brand.grantee_code or '',
            },
        )

    for brand in involved_brands:
        if brand.parent_brand_id:
            child_id = f'brand:{brand.id}'
            parent_id = f'brand:{brand.parent_brand_id}'
            if parent_id in seen_nodes and child_id in seen_nodes:
                add_edge(parent_id, child_id, 'parent_brand')

    for radio in radios:
        radio_node_id = f'radio:{radio.id}'
        add_node(
            radio_node_id,
            {
                'type': 'radio',
                'label': f'{radio.brand} {radio.model}',
                'brand': radio.brand,
                'model': radio.model,
                'fcc_id': radio.fcc_id or '',
                'is_white_label': bool(radio.is_a_whitelabel),
            },
        )

        market_brand = brand_by_key.get(_normalize(radio.brand))
        if market_brand:
            market_brand_node_id = f'brand:{market_brand.id}'
            add_edge(market_brand_node_id, radio_node_id, 'markets')

        if radio.manufacturer_id:
            manufacturer_node_id = f'brand:{radio.manufacturer_id}'
            if manufacturer_node_id in seen_nodes:
                add_edge(manufacturer_node_id, radio_node_id, 'manufactures')

            if radio.is_a_whitelabel and market_brand and market_brand.id != radio.manufacturer_id:
                add_edge(
                    manufacturer_node_id,
                    f'brand:{market_brand.id}',
                    'white_label_chain',
                    payload={'label': radio.model},
                )

    for fcc_id, grouped_radios in fcc_groups.items():
        if len(grouped_radios) < 2:
            continue

        fcc_node_id = f'fcc:{fcc_id}'
        add_node(
            fcc_node_id,
            {
                'type': 'fcc',
                'label': fcc_id,
            },
        )

        for radio in grouped_radios:
            add_edge(f'radio:{radio.id}', fcc_node_id, 'shares_fcc')

    brand_node_count = sum(1 for node in nodes if node.get('type') == 'brand')
    radio_node_count = sum(1 for node in nodes if node.get('type') == 'radio')
    fcc_node_count = sum(1 for node in nodes if node.get('type') == 'fcc')

    return {
        'nodes': nodes,
        'edges': edges,
        'stats': {
            'brand_nodes': brand_node_count,
            'radio_nodes': radio_node_count,
            'fcc_nodes': fcc_node_count,
            'total_edges': len(edges),
        },
    }
