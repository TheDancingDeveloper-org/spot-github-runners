"""Independent OOM obligations with compatible legacy kind/origin projections."""
import copy
import math
import re
import time

PRIORITY={'unknown':0,'container':0,'runner':1,'builder':2,'host':3}


def obligations_for(state,key):
    """Read without mutation so identity/evidence guards can hold atomically."""
    obligations=copy.deepcopy(state.get('pending_oom_obligations',{}).get(key,{}))
    legacy=state.get('pending_oom',{}).get(key)
    if legacy is not None and legacy not in obligations:
        origin=copy.deepcopy(state.get('pending_oom_origins',{}).get(key,{}))
        if origin.get('kind')!=legacy:
            origin=dict(kind=legacy,boot_id=state.get('host_health',{}).get(key,{}).get('boot_id'),
                        event_ids=[],quarantined=True)
        obligations[legacy]=origin
    return obligations


def pending_kinds(state,key):
    return set(obligations_for(state,key))


def sync_pending(state):
    hosts=set(state.get('pending_oom',{}))|set(state.get('pending_oom_obligations',{}))
    ledger={key:rows for key in hosts if (rows:=obligations_for(state,key))}
    state['pending_oom_obligations']=ledger
    state['pending_oom']={key:max(rows,key=lambda kind:PRIORITY.get(kind,0)) for key,rows in ledger.items()}
    state['pending_oom_origins']={key:copy.deepcopy(ledger[key][kind])
                                  for key,kind in state['pending_oom'].items()}
    return ledger


def remember_oom(state,key,kind,boot_id,event_ids):
    ledger=sync_pending(state)
    rows=ledger.setdefault(key,{})
    origin=rows.setdefault(kind,dict(kind=kind,boot_id=boot_id,event_ids=[],quarantined=not bool(boot_id)))
    origin['event_ids']=sorted(set(origin.get('event_ids',[]))|set(event_ids))
    # Preserve the first originating boot, including quarantined legacy flags.
    sync_pending(state)


def clear_oom(state,key,kind=None):
    ledger=sync_pending(state)
    if kind is None:ledger.pop(key,None)
    elif key in ledger:
        ledger[key].pop(kind,None)
        if not ledger[key]:ledger.pop(key)
    # Remove the old projection before synchronizing, so it cannot resurrect
    # an explicitly completed obligation during the legacy import.
    state['pending_oom'].pop(key,None)
    state['pending_oom_origins'].pop(key,None)
    sync_pending(state)


def promote_attributed_ooms(state,key,observed):
    """Transfer proven exact-ID unknown events into still-pending cap repair."""
    if (not isinstance(observed,dict) or observed.get('complete') is not True
        or observed.get('instance_id')!=key.rsplit('/',1)[-1]):return []
    timestamp=observed.get('observed_at');boot=observed.get('boot_id')
    if (not isinstance(timestamp,(int,float)) or isinstance(timestamp,bool) or not math.isfinite(timestamp)
        or not 0<=time.time()-timestamp<=450 or not isinstance(boot,str) or not boot):return []
    rows=obligations_for(state,key)
    events=list(observed.get('events') or [])
    if observed.get('reclassification_complete') is True:
        events.extend(observed.get('reclassified_events') or [])
    claims={}
    for event in events:
        if not isinstance(event,dict):continue
        identity=event.get('id');kind=event.get('kind');workload=event.get('workload')
        if not isinstance(identity,str) or not re.fullmatch(r'[0-9a-f]{64}',identity):continue
        if not isinstance(kind,str) or not isinstance(workload,str):kind,workload='invalid','invalid'
        claims.setdefault(identity,set()).add((kind,workload))
    promoted=[]
    for unknown in ('unknown','container'):
        origin=rows.get(unknown)
        if (not isinstance(origin,dict) or origin.get('kind')!=unknown
            or origin.get('boot_id')!=boot or origin.get('quarantined') is not False):continue
        origin_ids=origin.get('event_ids')
        if not isinstance(origin_ids,list) or any(not isinstance(identity,str) for identity in origin_ids):continue
        remaining=set(origin_ids)
        for identity in sorted(remaining):
            claim=claims.get(identity,set())
            if len(claim)!=1:continue
            kind,workload=next(iter(claim))
            if kind!='container' or workload not in ('runner','builder'):continue
            destination=rows.get(workload)
            if destination is not None and (not isinstance(destination,dict) or destination.get('kind')!=workload or destination.get('boot_id')!=boot
                or destination.get('quarantined') is not False or not isinstance(destination.get('event_ids'),list)
                or any(not isinstance(value,str) for value in destination['event_ids'])):continue
            if destination is None:
                destination=rows[workload]=dict(kind=workload,boot_id=boot,event_ids=[],quarantined=False)
            destination['event_ids']=sorted(set(destination.get('event_ids') or [])|{identity})
            remaining.remove(identity);promoted.append(identity)
        if remaining:origin['event_ids']=sorted(remaining)
        elif origin.get('event_ids'):rows.pop(unknown)
    if promoted:
        state.setdefault('pending_oom_obligations',{})[key]=rows
        # Remove only this host's derived legacy projection before syncing;
        # otherwise importing it would resurrect the transferred unknown IDs.
        state.get('pending_oom',{}).pop(key,None)
        state.get('pending_oom_origins',{}).pop(key,None)
        sync_pending(state)
    return sorted(set(promoted))
