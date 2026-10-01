"""Compare compatible boot and runner disks using full monthly charges."""
from decimal import Decimal
from .billing_catalog import catalog_skus, unit_price
from .machine_profiles import machine_profile

PREFIXES = {
    'Storage PD Capacity in ': ('pd-standard:capacity', 'GiBy.mo'),
    'Balanced PD Capacity in ': ('pd-balanced:capacity', 'GiBy.mo'),
    'SSD backed PD Capacity in ': ('pd-ssd:capacity', 'GiBy.mo'),
    'Hyperdisk Balanced Capacity in ': ('hyperdisk-balanced:capacity', 'GiBy.mo'),
    'Hyperdisk Balanced IOPS in ': ('hyperdisk-balanced:iops', 'mo'),
    'Hyperdisk Balanced Throughput in ': ('hyperdisk-balanced:throughput', 'mo'),
    'Hyperdisk Throughput Capacity in ': ('hyperdisk-throughput:capacity', 'GiBy.mo'),
    'Hyperdisk Throughput Throughput in ': ('hyperdisk-throughput:throughput', 'mo'),
}


def storage_rates(region: str) -> dict[str, Decimal]:
    prices = {}
    for sku in catalog_skus():
        if region not in sku.get('serviceRegions', []):
            continue
        description = sku.get('description', '')
        for prefix, (key, usage_unit) in PREFIXES.items():
            if description.startswith(prefix):
                price = unit_price(sku, usage_unit)
                if price is None and key.endswith(':throughput'):
                    price = unit_price(sku, 'MiBy/s.mo')
                if price is not None:
                    prices[key] = price
    return prices


def throughput_floor(size_gib: int) -> int:
    return max(20, (size_gib * 5 + 1023) // 1024)


def disk_monthly_cost(kind: str, size_gib: int, rates: dict,
                      iops: int = 3000, throughput: int = 140) -> Decimal:
    if size_gib <= 0:
        raise RuntimeError('Disk size is unknown or invalid')
    try:
        cost = rates[kind + ':capacity'] * size_gib
        if kind == 'hyperdisk-balanced':
            if iops > 3000:
                cost += rates[kind + ':iops'] * (iops - 3000)
            if throughput > 140:
                cost += rates[kind + ':throughput'] * (throughput - 140)
        elif kind == 'hyperdisk-throughput':
            cost += rates[kind + ':throughput'] * throughput
    except KeyError as exc:
        raise RuntimeError(f'Missing storage price for {kind}') from exc
    if not cost.is_finite() or cost < 0:
        raise RuntimeError(f'Invalid storage price for {kind}')
    return cost


def cheapest_storage(machine_type: str, size_gib: int, rates: dict) -> tuple[str, str, Decimal]:
    profile = machine_profile(machine_type)
    if profile is None:
        raise RuntimeError(f'Unsupported storage machine type {machine_type}')
    modern = profile.disk_type == 'hyperdisk-balanced'
    boot_types = ['hyperdisk-balanced'] if modern else ['pd-standard', 'pd-balanced', 'pd-ssd']
    data_types = list(boot_types)
    if size_gib >= 2048 and machine_type.split('-', 1)[0] in {'c4', 'c4d', 'n4', 'n4d', 'n2', 'n2d'}:
        data_types.append('hyperdisk-throughput')
    def choose(kinds, size):
        choices = []
        for kind in kinds:
            if kind == 'hyperdisk-throughput' and not {
                    kind + ':capacity', kind + ':throughput'}.issubset(rates):
                continue  # This optional alternative cannot be quoted in the region.
            cost = disk_monthly_cost(kind, size, rates,
                                     throughput=throughput_floor(size) if kind == 'hyperdisk-throughput' else 140)
            choices.append((cost, kind))
        if not choices:
            raise RuntimeError(f'No compatible storage price for {machine_type} ({size} GiB)')
        cost, kind = min(choices)
        return kind, cost
    boot, boot_cost = choose(boot_types, 40)
    data, data_cost = choose(data_types, size_gib)
    return boot, data, boot_cost + data_cost


def observed_storage_monthly(disks, rates: dict) -> Decimal:
    if not disks:
        raise RuntimeError('Attached disk specifications are unavailable')
    total = Decimal(0)
    for disk in disks:
        if '/regions/' in disk.get('source', '') or '/regions/' in disk.get('type', ''):
            raise RuntimeError('Regional storage pricing is unavailable; refusing a zonal price estimate')
        kind = disk.get('diskType') or disk.get('type', '').rsplit('/', 1)[-1]
        if kind.startswith('hyperdisk-') and ('provisionedThroughput' not in disk
                                             or (kind == 'hyperdisk-balanced' and 'provisionedIops' not in disk)):
            raise RuntimeError(f'Provisioned storage performance is unknown for {kind}')
        total += disk_monthly_cost(kind, int(disk.get('diskSizeGb', 0)), rates,
                                   iops=int(disk.get('provisionedIops', 3000)),
                                   throughput=int(disk.get('provisionedThroughput', 140)))
    return total
