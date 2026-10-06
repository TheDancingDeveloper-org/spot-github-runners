"""Evidence-led, conservative topology decisions; no provider side effects."""
import math
from .runner_tiers import TIERS, next_topology
from .fleet_evidence import io_status
from .host_admission import admission_budget


def reservation_fits(topology,job_memory_gib,slots=None):
    if not topology or isinstance(job_memory_gib,bool) or not isinstance(job_memory_gib,(int,float)):
        return False
    try:
        for index,name in enumerate(topology):
            admission_budget(TIERS[name],2,job_memory_gib,
                slots=slots if slots is not None and index==len(topology)-1 else TIERS[name].initial_slots)
    except (KeyError,ValueError,TypeError,OverflowError):
        return False
    return True


def compatible_recovery_tier(name,job_memory_gib):
    candidate=(name,)
    while not reservation_fits(candidate,job_memory_gib):
        next_candidate=next_topology(candidate,'up','memory')
        if next_candidate==candidate or len(next_candidate)!=1:
            return None
        candidate=next_candidate
    return candidate[0]


def fits(target, evidence):
    if not target or not evidence:
        return False
    cores = sum(row['peak_cpu_cores'] for row in evidence)
    ram = sum(row['peak_ram_gib'] for row in evidence)
    # Includes host processes plus listener overhead on every destination.
    return (cores < .60 * sum(TIERS[name].vcpu for name in target)
            and ram * 1.25 + 2 * len(target) < sum(TIERS[name].ram_gib for name in target))


def plan_capacity(topology, evidence, workload, state, timestamp):
    topology = tuple(topology)
    hold = dict(target=topology,action='hold',reason='stable')
    reservation=workload.get('minimum_job_memory_gib',2)
    if not topology:
        candidate=('starting',)
        while not reservation_fits(candidate,reservation):
            next_candidate=next_topology(candidate,'up','memory')
            if next_candidate==candidate:
                return {**hold,'reason':'known workload reservation does not fit allowed tiers'}
            candidate=next_candidate
        return dict(target=candidate,action='topology',reason='empty')
    valid = bool(evidence)
    try:
        valid = valid and all(row['windows'] >= 1 and all(math.isfinite(row[key]) and row[key] >= 0
                    for key in ('cpu_cores','peak_cpu_cores','used_ram_gib','peak_ram_gib',
                                'cpu_fraction','memory_fraction')) for row in evidence)
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        state.pop('streak',None)
        state.pop('direction',None)
        state.pop('timestamp',None)
        return {**hold,'reason':'incomplete resource evidence'}
    if workload.get('complete') is False:
        state.pop('streak',None)
        return {**hold,'reason':'incomplete compatible job inventory'}
    if workload.get('unknown_oom') and not (workload.get('host_oom') or workload.get('container_oom') or workload.get('builder_oom')):
        return {**hold,'reason':'unattributed container OOM needs identity evidence'}
    host_oom = (workload.get('host_oom',False) or ((workload.get('container_oom') or workload.get('builder_oom'))
                and any(row['memory_fraction']>.75 for row in evidence)))
    if (workload.get('container_oom') or workload.get('builder_oom')) and not host_oom:
        return {**hold,'action':'budget','reason':'container OOM with host headroom'}
    io=io_status(evidence)
    # Storage stalls can inflate queue and duration signals while CPU stays
    # idle. Require healthy IO before increasing density or consolidating;
    # actual memory pressure/global OOM can still receive capacity relief.
    memory=any(row['memory_fraction']>.75 for row in evidence)
    if io!='healthy' and not host_oom and not memory:
        state.pop('streak',None);state.pop('direction',None);state.pop('timestamp',None)
        return {**hold,'reason':'incomplete IO pressure evidence' if io=='incomplete' else 'IO pressure requires storage relief'}
    if not host_oom and timestamp - state.get('changed_at',-math.inf) < 900:
        return {**hold,'reason':'cooldown'}
    if 'legacy' in topology:
        if io!='healthy':
            return {**hold,'reason':'legacy consolidation requires healthy IO pressure evidence'}
        if any(row['windows'] < 2 for row in evidence) or workload.get('pending'):
            return {**hold,'reason':'legacy migration needs complete history and no compatible queue'}
        candidate = ('starting',)
        while not fits(candidate,evidence) or not reservation_fits(candidate,reservation):
            next_candidate = next_topology(candidate,'up','memory')
            if candidate == next_candidate:
                return {**hold,'reason':'demand exceeds allowed replacement fleet'}
            candidate = next_candidate
        return dict(target=candidate,action='topology',reason='oversized migration')
    previous = state.get('timestamp')
    if previous is not None and timestamp - previous < 300 and not host_oom:
        return {**hold,'reason':'waiting for distinct resource window'}
    memory = any(row['memory_fraction'] > .75 for row in evidence)
    cpu = any(row['cpu_fraction'] > .75 for row in evidence)
    delayed = workload.get('queue_delayed',False) or workload.get('duration_regression',False)
    direction = 'up' if host_oom or memory or cpu or delayed else 'down'
    if direction=='down' and workload.get('pending'):
        state.update(timestamp=timestamp,direction='wait',streak=0)
        return {**hold,'reason':'compatible jobs pending'}
    streak = state.get('streak',0) + 1 if (state.get('direction') == direction and previous is not None
                                        and 300 <= timestamp - previous <= 450) else 1
    state.update(timestamp=timestamp,direction=direction,streak=streak)
    reason = 'oom' if host_oom else 'memory' if memory else 'cpu' if cpu else 'queue' if delayed else 'low'
    required = 1 if host_oom else 2 if direction == 'up' else 6
    if streak < required:
        return {**hold,'reason':f'{direction} evidence {streak}/{required}'}
    slots=workload.get('slots',0)
    if (delayed and not memory and not cpu and not host_oom and slots
            and slots<TIERS[topology[-1]].maximum_slots and fits(topology,evidence)):
        next_slots=min(TIERS[topology[-1]].maximum_slots,slots+4)
        tier=TIERS[topology[-1]]
        if reservation_fits(topology,reservation,next_slots):
            current=admission_budget(tier,2,reservation,slots=slots)['concurrency']
            future=admission_budget(tier,2,reservation,slots=next_slots)['concurrency']
            if future>current:
                return dict(target=topology,action='density',reason='compatible queue with resource headroom',slots=next_slots)
    if direction == 'down' and (workload.get('pending') or any(row['windows'] < 6 for row in evidence)):
        return {**hold,'reason':'pending job or insufficient consolidation history'}
    target = next_topology(topology,direction,reason)
    if target == topology:
        return {**hold,'reason':'capacity boundary'}
    if direction=='up':
        while not reservation_fits(target,reservation):
            next_target=next_topology(target,'up','memory')
            if next_target==target:
                return {**hold,'reason':'known workload reservation does not fit allowed growth tiers'}
            target=next_target
    if not reservation_fits(target,reservation):
        return {**hold,'reason':'known workload reservation does not fit target topology'}
    if direction == 'down' and not fits(target,evidence):
        return {**hold,'reason':'projected CPU/RAM peaks do not fit smaller topology'}
    decision = dict(target=target,action='topology',reason=reason)
    if host_oom:
        decision['slots'] = TIERS[target[-1]].initial_slots
        state['density_frozen'] = True
    return decision
