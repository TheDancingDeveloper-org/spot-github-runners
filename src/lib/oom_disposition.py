"""Explicit maintenance disposition of the retired sampler's false context.

Never called by the autoscaler. A maintainer must supply original state,
fresh provider and guest evidence, and the complete bounded boot query.
"""
import hashlib
import math
import re
from datetime import datetime
from .oom_obligations import clear_oom,obligations_for


def dispose_false_context(state, provider, guest, journal, timestamp):
    """Clear one provably false legacy flag; retain all real/unknown OOMs."""
    identity = str(provider['id'])
    zone = provider['zone'].rsplit('/', 1)[-1]
    key = zone + '/' + identity
    if (provider.get('status') != 'RUNNING' or guest.get('instance_id') != identity
            or guest.get('complete') is not True or guest.get('boot_oom_complete') is not True
            or guest.get('boot_host_oom_ids') != []):
        raise ValueError('Incomplete or global OOM evidence')
    if not 0 <= timestamp - guest.get('observed_at', 0) <= 120:
        raise ValueError('Disposition requires a fresh guest sample')
    created = datetime.fromisoformat(provider['creationTimestamp']).timestamp()
    started = datetime.fromisoformat(provider['lastStartTimestamp']).timestamp()
    boot_time = guest['kernel_boot_timestamp']
    if (not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (created, started, boot_time))
            or not created <= started <= boot_time <= created + 120):
        raise ValueError('Cannot establish the original deployment boot')
    original = state.get('host_health', {}).get(key, {})
    boot = guest['boot_id']
    origin = obligations_for(state,key).get('host')
    if (not boot or original.get('instance_id') != identity or original.get('boot_id') != boot
            or origin is None):
        raise ValueError('Original flag identity or boot changed')
    if origin and (origin.get('kind') != 'host' or origin.get('quarantined') is not True
                   or origin.get('boot_id') not in (None, boot)):
        raise ValueError('A genuine or different-boot origin cannot be disposed')
    if (guest.get('boot_query_complete') is not True or not isinstance(journal, list)
            or not 0 < len(journal) < 2000):
        raise ValueError('Incomplete bounded boot query')
    false_ids = set()
    for row in journal:
        message = row['MESSAGE']
        if not isinstance(message, str) or len(message) > 65536:
            raise ValueError('Invalid kernel decision')
        at = int(row['__REALTIME_TIMESTAMP']) / 1e6
        if not boot_time <= at <= guest['observed_at']:
            raise ValueError('Kernel decision is outside the proven boot')
        # Reconstruct the retired classifier, rather than trusting a supplied
        # assertion that a hash belonged to diagnostic context.
        if not re.search(r'(out of memory|oom-kill|Killed process)', message, re.I):
            raise ValueError('Boot query contains an unrecognized record')
        old_host = not re.search(r'(cgroup|CONSTRAINT_MEMCG)', message, re.I)
        if old_host:
            if not re.search(r'\binvoked oom-killer:', message, re.I):
                raise ValueError('Real or unknown host decision remains')
            false_ids.add(hashlib.sha256(f'{boot}:{at}:{message}'.encode()).hexdigest())
    recorded = {row['id'] for row in original.get('events', []) if row.get('kind') == 'host'}
    origin_ids = set(origin.get('event_ids', [])) if origin else set()
    if not recorded or not (recorded | origin_ids) <= false_ids:
        raise ValueError('Original host flags are not all proven false context')
    audit = dict(instance_id=identity, boot_id=boot, disposed_at=timestamp,
                 false_context_ids=sorted(recorded | origin_ids),
                 reason='38ab sampler classified diagnostic invoked oom-killer context as host OOM')
    state.setdefault('oom_dispositions', {})[key] = audit
    clear_oom(state,key,'host')
    return audit
