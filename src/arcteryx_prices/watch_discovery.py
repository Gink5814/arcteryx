"""Create exact variant watches from a freshly fetched product, without guessed SKUs."""

from .monitor import discover_for_monitor, import_watches, parse_target_price


def _selections(values, field):
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError(f'{field} must contain at least one exact name')
    if any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in values):
        raise ValueError(f'{field} must contain nonempty names up to 256 characters')
    return {value.strip().casefold() for value in values}


def add_discovered_watches(connection, url, *, sizes, colors=None, rule='available_or_target',
                           target_price=None, interval_minutes=15):
    size_names = _selections(sizes, 'sizes')
    color_names = _selections(colors, 'colors') if colors is not None else None
    # Validate user settings before making a network request.
    price = parse_target_price(target_price)
    if rule not in {'target', 'new_low', 'restock', 'available_or_target'}:
        raise ValueError('unsupported watch rule')
    if rule == 'target' and price is None:
        raise ValueError('target rule requires --target-price in USD')
    if type(interval_minutes) is not int or not 15 <= interval_minutes <= 1440:
        raise ValueError('interval_minutes must be an integer from 15 to 1440')
    discovered = discover_for_monitor(connection, url)
    selected = [variant for variant in discovered['variants']
                if variant['size'].strip().casefold() in size_names
                and (color_names is None or variant['color'].strip().casefold() in color_names)]
    if not selected:
        raise ValueError('No verified variants match the requested sizes/colors; no watches were added')
    payload = [{key: variant[key] for key in ('sku', 'color', 'size')} | {
        'url': discovered['url'],
        'label': f"{discovered.get('label', 'Arc’teryx')} · {variant['color']} · {variant['size']}",
        'rule': rule, 'target_price': target_price, 'interval_minutes': interval_minutes,
    } for variant in selected]
    imported = import_watches(connection, payload)
    color_labels = ({value.strip().casefold(): value.strip() for value in colors} if colors is not None else
                    {variant['color'].strip().casefold(): variant['color'] for variant in discovered['variants']})
    size_labels = {value.strip().casefold(): value.strip() for value in sizes}
    pairs = {(variant['color'].strip().casefold(), variant['size'].strip().casefold()) for variant in selected}
    return imported | {
        'url': discovered['url'], 'source_url': discovered['url'],
        'queried_at': discovered['captured_at'],
        'selected_variants': len(selected),
        'variants': [{key: variant[key] for key in ('sku', 'color', 'size', 'availability')} for variant in selected],
        'colors': sorted({variant['color'] for variant in selected}),
        'sizes': sorted({variant['size'] for variant in selected}),
        'unmatched_sizes': sorted(size_names - {variant['size'].strip().casefold() for variant in selected}),
        'unmatched_colors': sorted(color_names - {variant['color'].strip().casefold() for variant in selected}) if color_names else [],
        'missing_combinations': [{'color': color_labels[color], 'size': size_labels[size]}
            for color in sorted(color_labels) for size in sorted(size_labels) if (color, size) not in pairs],
        'message': 'Exact watches added without price observations. Run a check to confirm current conditions and generate alerts; existing watches keep their settings.',
    }
