#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将多个产品菜单映射压缩为按 TFS 区域检索的统一业务入口索引。"""
import argparse
import json
import os


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOURCES = os.path.join(SCRIPT_DIR, 'menu-sources.json')
DEFAULT_OUTPUT = os.path.join(SCRIPT_DIR, 'menu-business-index.json')
REQUIRED_FIELDS = ('mcode', 'pcaption', 'menu_path', 'business_domain', 'match_status')
OPTIONAL_FIELDS = ('module_url', 'subproject', 'repo')


def read_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def validate_sources(data, manifest_path):
    sources = data.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('menu-sources.json 必须含非空 sources 数组')
    seen_products, seen_areas = set(), set()
    base = os.path.dirname(os.path.abspath(manifest_path))
    normalized = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('每个菜单源必须为对象')
        product_id = source.get('product_id')
        product_name = source.get('product_name')
        areas = source.get('tfs_area_values')
        relative_path = source.get('source')
        if not all(isinstance(value, str) and value.strip()
                   for value in (product_id, product_name, relative_path)):
            raise ValueError('菜单源必须含非空 product_id、product_name、source')
        if product_id in seen_products:
            raise ValueError(f'重复 product_id: {product_id}')
        if not isinstance(areas, list) or not areas or not all(isinstance(area, str) and area.strip() for area in areas):
            raise ValueError(f'{product_id} 的 tfs_area_values 必须为非空字符串数组')
        duplicate_areas = seen_areas & set(areas)
        if duplicate_areas:
            raise ValueError(f'区域只能归属一个产品: {sorted(duplicate_areas)}')
        source_path = os.path.normpath(os.path.join(base, relative_path))
        if not os.path.isfile(source_path):
            raise ValueError(f'菜单源不存在: {source_path}')
        seen_products.add(product_id)
        seen_areas.update(areas)
        normalized.append({
            'product_id': product_id,
            'product_name': product_name,
            'tfs_area_values': areas,
            'source': relative_path,
            'source_path': source_path,
        })
    return normalized


def compact_menu(menu, source):
    item = {'product_id': source['product_id']}
    for field in REQUIRED_FIELDS:
        item[field] = menu.get(field, '')
    for field in OPTIONAL_FIELDS:
        value = menu.get(field)
        if value not in (None, '', []):
            item[field] = value
    return item


def build_index(manifest_path=DEFAULT_SOURCES):
    manifest = read_json(manifest_path)
    sources = validate_sources(manifest, manifest_path)
    entries, metadata = [], []
    for source in sources:
        raw = read_json(source['source_path'])
        modules = raw.get('modules')
        if not isinstance(modules, dict):
            raise ValueError(f"{source['source']} 缺少 modules 对象")
        menus = [menu for module in modules.values() if isinstance(module, dict)
                 for menu in module.get('menus', []) if isinstance(menu, dict)]
        codes = [menu.get('mcode') for menu in menus]
        if len(codes) != len(set(codes)) or any(not code for code in codes):
            raise ValueError(f"{source['source']} 的 mcode 必须非空且在产品内唯一")
        declared_total = raw.get('total_menus')
        if declared_total is not None and declared_total != len(menus):
            raise ValueError(f"{source['source']} total_menus={declared_total}，实际为 {len(menus)}")
        entries.extend(compact_menu(menu, source) for menu in menus)
        metadata.append({key: source[key] for key in ('product_id', 'product_name', 'tfs_area_values', 'source')}
                        | {'total_menus': len(menus)})
    return {'version': 1, 'total_menus': len(entries), 'products': metadata, 'menus': entries}


def products_for_area(index, area):
    """返回精确匹配 TFS 区域的产品元数据；空数组表示不可安全收敛。"""
    return [product for product in index.get('products', [])
            if area and area in product.get('tfs_area_values', [])]


def write_index(index, output_path):
    directory = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(directory, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, separators=(',', ':'))
        f.write('\n')


def main():
    parser = argparse.ArgumentParser(description='构建 / 查询按 TFS 区域路由的多产品菜单业务索引')
    sub = parser.add_subparsers(dest='cmd')

    build = sub.add_parser('build', help='构建索引（无子命令时的默认行为）')
    build.add_argument('--sources', default=DEFAULT_SOURCES)
    build.add_argument('--output', default=DEFAULT_OUTPUT)

    lookup = sub.add_parser('lookup', help='按 TFS 区域查候选产品')
    lookup.add_argument('--area', required=True,
                        help='TFS 区域（System.AreaPath 首级；缺失才用 teamProject）')
    lookup.add_argument('--index', default=DEFAULT_OUTPUT, help='索引 JSON 路径')

    args = parser.parse_args()

    if args.cmd == 'lookup':
        try:
            index = read_json(args.index)
            products = products_for_area(index, args.area)
            print(json.dumps({'ok': True, 'area': args.area, 'count': len(products),
                              'products': products}, ensure_ascii=False, indent=2))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, indent=2))
            raise SystemExit(1)
        return

    # 默认（无子命令或 build）：构建索引；--sources/--output 缺省取 DEFAULT_*
    sources = getattr(args, 'sources', DEFAULT_SOURCES)
    output = getattr(args, 'output', DEFAULT_OUTPUT)
    try:
        index = build_index(sources)
        write_index(index, output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    print(json.dumps({'ok': True, 'output': output, 'products': len(index['products']),
                      'total_menus': index['total_menus']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
