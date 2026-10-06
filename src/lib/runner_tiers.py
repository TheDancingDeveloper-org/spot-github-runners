"""Allowed fleet shapes, strict all-disk budgets and the ordered capacity ladder."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class RunnerTier:
    name: str
    vcpu: int
    ram_gib: int
    initial_slots: int
    maximum_slots: int
    max_daily_usd: Decimal

    def affordable(self, total) -> bool:
        try:
            price = Decimal(str(total))
            return price.is_finite() and 0 < price < self.max_daily_usd
        except (InvalidOperation, ValueError, TypeError):
            return False


TIERS = {tier.name: tier for tier in (
    RunnerTier('small', 4, 16, 4, 8, Decimal('.60')),
    RunnerTier('starting', 8, 16, 8, 8, Decimal('1.25')),
    RunnerTier('expanded', 8, 32, 8, 16, Decimal('1.25')),
    RunnerTier('large', 16, 64, 20, 20, Decimal('3')),
    RunnerTier('bulky', 32, 128, 16, 20, Decimal('4.50')),
)}


def tier_for_shape(vcpu, ram_gib):
    return next((tier for tier in TIERS.values()
                 if (tier.vcpu, tier.ram_gib) == (vcpu, ram_gib)), None)


def validate_topology(topology):
    if len(topology) > 5 or any(name not in TIERS for name in topology):
        raise ValueError('Only five allowed 4/8/16/32-vCPU tier hosts may be managed')
    if sum(TIERS[name].vcpu for name in topology) > 80:
        raise ValueError('Fleet exceeds 80 vCPU')
    if sum(TIERS[name].maximum_slots for name in topology) > 100:
        raise ValueError('Fleet exceeds 100 runner slots')
    if len(topology) > 1 and (any(name not in ('large','bulky') for name in topology[:-1])
                             or topology[-1] == 'starting'):
        raise ValueError('Fleet must grow its small/expanded tail before adding another host')
    return tuple(topology)


def next_topology(tiers, direction, reason):
    tiers = validate_topology(tuple(tiers))
    if direction not in ('up', 'down'):
        return tiers
    if not tiers:
        return ('starting',) if direction == 'up' else ()
    prefix, tail = tiers[:-1], tiers[-1]
    if direction == 'up':
        if tail == 'large' and reason in ('cpu','memory','oom'):
            result = prefix + ('bulky',)
        elif tail in ('large','bulky'):
            result = tiers + ('small',) if len(tiers) < 5 else tiers
        elif tail == 'small':
            result = prefix + (('starting' if not prefix and reason not in ('memory','oom') else 'expanded'),)
        elif tail == 'starting':
            result = prefix + (('expanded' if reason in ('memory', 'oom','queue') else 'large'),)
        else:
            result = prefix + ('large',)
    elif tail == 'small':
        result = prefix or tiers
    elif tail == 'bulky':
        result = prefix + ('large',)
    elif tail == 'large':
        result = prefix + ('expanded',)
    elif tail == 'expanded':
        result = prefix + (('small' if prefix else 'starting'),)
    else:
        result = prefix + ('small',)
    try:
        return validate_topology(result)
    except ValueError:
        return tiers
