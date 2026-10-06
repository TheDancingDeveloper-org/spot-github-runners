"""Persist one tier replacement; existing identity-guarded drain owns deletion."""
import logging
import copy
import math
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from .runner_tiers import TIERS,tier_for_shape,validate_topology
from .tier_policy import fits,reservation_fits
from .fleet_evidence import io_status,current_job_coverage
from .host_evidence import validate_io_pressure
from .storage_selection import observed_data_storage
from .oom_obligations import obligations_for,pending_kinds,remember_oom,clear_oom

LOG=logging.getLogger('spot_runner_autoscaler')


def data_storage_status(vm,args):
    device=getattr(args,'disk_device_name',None) or 'runner-data-'+vm.name.removeprefix('oxidex-runners-')
    return observed_data_storage(vm.disks,device,getattr(args,'disk_size_gb',100))


def expected_slots(vm,state):
    return int(state.get('host_deployments',{}).get(f'{vm.zone}/{vm.instance_id}',{}).get('expected_runner_count',8))


def topology_for(vms):
    names=[tier.name if (tier:=tier_for_shape(vm.vcpu,vm.ram_gib)) else 'legacy' for vm in vms]
    rank={'legacy':0,'bulky':.5,'large':1,'expanded':2,'starting':3,'small':4}
    return tuple(sorted(names,key=rank.__getitem__))


def begin_migration(vms,target,state,state_path,reason,slots=None,force_ids=(),slot_counts=(),io_pressure_sources=(),staged_bad_id=None):
    from .spot_runner_autoscaler import write_scale_state
    validate_topology(target)
    if state.get('migration') or state.get('host_operation'):
        raise RuntimeError('Another lifecycle transaction is active')
    remaining=Counter(target)
    sources=[]
    retained=[]
    for vm in vms:
        tier=tier_for_shape(vm.vcpu,vm.ram_gib)
        if tier and remaining[tier.name] and vm.instance_id not in force_ids:
            remaining[tier.name]-=1
            retained.append(vm.instance_id)
        else:
            if not vm.instance_id:raise ValueError('Migration requires provider instance identity')
            sources.append(asdict(vm))
    pending_tiers=list(remaining.elements())
    recorded_slots={}
    for name,count in slot_counts:
        if name not in TIERS or not isinstance(count,int) or not 1<=count<=TIERS[name].maximum_slots:
            raise ValueError('Recovery receipt contains unsafe runner density')
        recorded_slots.setdefault(name,[]).append(count)
    pending_slots=[recorded_slots[name].pop(0) if recorded_slots.get(name)
                   else slots or TIERS[name].initial_slots for name in pending_tiers]
    if io_pressure_sources and (reason!='storage-pressure canary replacement'
                               or set(io_pressure_sources)!={row['instance_id'] for row in sources}):
        raise ValueError('Storage relief exemption requires exact retiring sources')
    state['migration']=dict(target=list(target),sources=sources,retained_ids=retained,
        pending_tiers=pending_tiers,pending_slots=pending_slots,replacement_ids=[],
        retained_records=[dict(asdict(vm),_expected_runner_count=expected_slots(vm,state))
                          for vm in vms if vm.instance_id in retained],phase='launch',reason=reason,
        started_at=time.time(),slots=slots)
    if reason=='low':
        state['migration'].update(launch_attempted=False,initial_deployments={
            f'{vm.zone}/{vm.instance_id}':copy.deepcopy(state.get('host_deployments',{}).get(f'{vm.zone}/{vm.instance_id}'))
            for vm in vms})
    if reason=='oom':
        state['migration']['source_oom_origins']={f'{vm.zone}/{vm.instance_id}':copy.deepcopy(obligations_for(state,f'{vm.zone}/{vm.instance_id}').get('host',{}))
            for vm in vms if vm.instance_id in {row['instance_id'] for row in sources}}
        state['migration']['oom_capacity_response']=sum(TIERS[name].ram_gib for name in target)>sum(vm.ram_gib or math.inf for vm in vms)
    if io_pressure_sources:
        state['migration']['io_pressure_source_ids']=sorted(io_pressure_sources)
    if staged_bad_id is not None:
        if (reason!='storage-pressure canary replacement' or list(target)!=['large','large']
                or len(sources)!=2 or staged_bad_id not in {row['instance_id'] for row in sources}):
            raise ValueError('Staging requires exact two-source storage replacement')
        state['migration'].update(storage_staged_bad_id=staged_bad_id,spend_ceiling='15')
    write_scale_state(state_path,state)


def retirement_io_safe(migration,resources,state):
    if migration.get('reason')!='storage-pressure canary replacement':
        return io_status(resources)=='healthy'
    allowed=set(migration.get('io_pressure_source_ids',[]))
    if not allowed or allowed!={row['instance_id'] for row in migration['sources']}:
        return False
    for row in resources or []:
        key=f"{row.get('zone')}/{row.get('instance_id')}";health=state.get('host_health',{}).get(key,{})
        try:
            if (health.get('instance_id')!=row['instance_id'] or health.get('complete') is not True
                    or not health.get('boot_id') or not 0<=time.time()-health['observed_at']<=450
                    or not 0<=time.time()-row['observed_at']<=450
                    or health.get('boot_oom_complete') is not True or health.get('boot_host_oom_ids')!=[]
                    or any(event.get('kind') in ('host','unknown') for event in health.get('events',[]))
                    or pending_kinds(state,key) & {'host','unknown','container'}):
                return False
        except (KeyError,TypeError,ValueError):
            return False
    destinations=[row for row in resources or [] if row.get('instance_id') not in allowed]
    # Source stalls are the explicit reason for replacement. They cannot veto
    # retirement once every destination has healthy IO; missing source evidence
    # still holds because measured demand and pressure must remain complete.
    return io_status(resources)!='incomplete' and io_status(destinations)=='healthy'


def retarget_unhealthy_storage_canary(vms,args,state,state_path):
    """Safely replace the exact stalled two-32-vCPU canary with one or two measured-fit 16/64 hosts."""
    migration=state.get('migration',{})
    if (migration.get('phase')!='healthy' or migration.get('pending_tiers')
            or migration.get('awaiting_launch') or state.get('host_operation')
            or len(migration.get('sources',[]))!=1 or len(migration.get('replacement_ids',[]))!=1
            or migration.get('target')!=['bulky']):
        return False
    managed=[vm for vm in vms if vm.spot and vm.name.startswith('oxidex-runners-')]
    recorded=migration['sources'][0];source_id=recorded['instance_id'];bad_id=migration['replacement_ids'][0]
    identities={source_id,bad_id}
    if (len(managed)!=2 or len(identities)!=2 or {vm.instance_id for vm in managed}!=identities
            or any(not vm.running or (vm.vcpu,vm.ram_gib)!=(32,128) for vm in managed)):
        return False
    source=next(vm for vm in managed if vm.instance_id==source_id)
    if (source.name!=recorded['name'] or source.zone!=recorded['zone']
            or tuple(source.disks)!=tuple(recorded.get('disks',[]))
            or not source.disks or any(not disk.get('diskId') for disk in source.disks)):
        return False
    if (any(f'{vm.zone}/{vm.name}' in state.get('intentional_deletions',[]) for vm in managed)
            or identities & set(state.get('intentional_deletion_ids',{}).values())):
        return False
    job_memory=getattr(args,'tier_job_memory_gib',15)
    if (not isinstance(job_memory,(int,float)) or isinstance(job_memory,bool) or job_memory<15
            or not reservation_fits(('large',),job_memory)
            or state.get('latest_workload',{}).get('complete') is not True):
        return False
    timestamp=time.time();resources=state.get('latest_resources') or []
    try:
        if ({(row['zone'],row['instance_id']) for row in resources}!={(vm.zone,vm.instance_id) for vm in managed}
                or len(resources)!=2 or io_status(resources)=='incomplete'
                or any(row['windows']<6 or not 0<=timestamp-row['observed_at']<=450
                       or any(not math.isfinite(row[name]) or row[name]<0 for name in ('peak_cpu_cores','peak_ram_gib'))
                       for row in resources)
                or not fits(('large','large'),resources)):
            return False
        for vm in managed:
            key=f'{vm.zone}/{vm.instance_id}';health=state.get('host_health',{}).get(key,{})
            if (health.get('instance_id')!=vm.instance_id or health.get('complete') is not True
                    or not health.get('boot_id') or not 0<=timestamp-health['observed_at']<=450
                    or health.get('boot_oom_complete') is not True or health.get('boot_host_oom_ids')!=[]
                    or any(event.get('kind') in ('host','unknown') for event in health.get('events',[]))
                    or pending_kinds(state,key) & {'host','unknown','container'}):
                return False
        bad=next(vm for vm in managed if vm.instance_id==bad_id);key=f'{bad.zone}/{bad.instance_id}'
        history=state.get('io_history',{}).get(key,{})
        windows=history.get('windows',[])
        storage=data_storage_status(bad,args)
        sustained=(history.get('boot_id')==state['host_health'][key]['boot_id'] and len(windows)>=2
                   and 300<=windows[-1]['observed_at']-windows[-2]['observed_at']<=450
                   and 0<=timestamp-windows[-1]['observed_at']<=450
                   and all(io_status([dict(io_complete=True,io_pressure_peaks=row)])=='pressured' for row in windows[-2:]))
        if storage=='incomplete' or not (storage=='incompatible' or sustained):return False
    except (KeyError,TypeError,ValueError,OverflowError):
        return False
    candidate=copy.deepcopy(state)
    candidate['migration_history']=(candidate.get('migration_history',[])+[
        dict(retargeted_at=timestamp,reason='sustained storage pressure',migration=copy.deepcopy(migration))])[-3:]
    candidate.pop('migration')
    target=('large',) if fits(('large',),resources) else ('large','large')
    ordered=sorted(managed,key=lambda vm:vm.instance_id!=bad_id) if len(target)==1 else managed
    begin_migration(ordered,target,candidate,state_path,'storage-pressure canary replacement',
                    slots=TIERS['large'].initial_slots,io_pressure_sources=identities,
                    staged_bad_id=bad_id if len(target)==2 else None)
    # Existing job trackers hold references into other state subtrees.
    state['migration']=candidate['migration']
    state['migration_history']=candidate['migration_history']
    LOG.info('Retargeted exact storage canary: preserve 32-vCPU sources until %s validates',target)
    return True


def defer_unused_low_plan(entry,vms,runners,args,state,state_path,discovery):
    """Decline an unavailable downsize while retaining all verified current capacity."""
    from . import spot_runner_autoscaler as controller
    current=state.get('migration',{});timestamp=time.time()
    effects=('awaiting_launch','replacement_ids','paused_retirement','storage_staged_bad_id',
             'early_retirement_ids','completed_retirements','health','io_pressure_source_ids','oom_budget_repairs')
    if (discovery.get('complete') is not True or entry.get('reason')!='low' or entry.get('phase')!='launch'
            or entry.get('launch_attempted',False) is not False or current.get('launch_attempted',False) is not False
            or any(entry.get(name) or current.get(name) for name in effects)
            or state.get('host_operation') or state.get('paused_retirement') or state.get('pending_vm_recovery')
            or state.get('scale_up_active')):return False
    try:
        fields=('target','sources','retained_ids','retained_records','pending_tiers','pending_slots','slots','started_at','phase','reason')
        if any(current.get(name)!=entry.get(name) for name in fields):return False
        target=tuple(entry['target']);validate_topology(target)
        managed=[vm for vm in vms if vm.name.startswith('oxidex-runners-')]
        topology=topology_for(managed);validate_topology(topology)
        records=entry['sources']+entry.get('retained_records',[])
        if (not entry['sources'] or len(records)!=len(managed) or not all(vm.running and vm.spot and vm.instance_id for vm in managed)
                or len({vm.instance_id for vm in managed})!=len(managed)
                or len({row['instance_id'] for row in records})!=len(records)
                or sorted(entry['retained_ids'])!=sorted(row['instance_id'] for row in entry.get('retained_records',[]))
                or {row['instance_id'] for row in records}!={vm.instance_id for vm in managed}
                or any(f'{vm.zone}/{vm.name}' in state.get('intentional_deletions',[]) for vm in managed)
                or {vm.instance_id for vm in managed}&set(state.get('intentional_deletion_ids',{}).values())
                or len(managed)>min(5,args.maximum_hosts) or sum(vm.vcpu or 80 for vm in managed)>80
                or sum(expected_slots(vm,state) for vm in managed)>100
                or state.get('desired_topology',list(topology))!=list(topology)
                or sum(TIERS[name].vcpu for name in target)>sum(vm.vcpu for vm in managed)
                or sum(TIERS[name].ram_gib for name in target)>sum(vm.ram_gib for vm in managed)
                or (sum(TIERS[name].vcpu for name in target)==sum(vm.vcpu for vm in managed)
                    and sum(TIERS[name].ram_gib for name in target)==sum(vm.ram_gib for vm in managed))):return False
        remaining=Counter(target)
        for record in entry.get('retained_records',[]):remaining[tier_for_shape(record['vcpu'],record['ram_gib']).name]-=1
        if (any(count<0 for count in remaining.values()) or not entry['pending_tiers']
                or entry['pending_tiers']!=list(remaining.elements())
                or entry['pending_slots']!=[entry.get('slots') or TIERS[name].initial_slots for name in entry['pending_tiers']]):return False
        start=entry['started_at']
        if isinstance(start,bool) or not isinstance(start,(int,float)) or not math.isfinite(start) or not 0<=start<=timestamp:return False
        # Older plans lack an attempt marker: require no CURRENT lifecycle effects,
        # and never reinterpret a recorded later receipt as an untouched plan.
        effect_ticks=list(state.get('failed_launch_candidates',{}).values())
        if 'migration_completed_at' in state:effect_ticks.append(state['migration_completed_at'])
        for archived in state.get('migration_history',[]):
            ticks=[archived[name] for name in ('retargeted_at','deferred_at') if name in archived]
            if not ticks:return False
            effect_ticks.extend(ticks)
        if any(isinstance(tick,bool) or not isinstance(tick,(int,float)) or not math.isfinite(tick)
               or not 0<=tick<start for tick in effect_ticks):return False
        def fresh(value):
            return (not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)
                    and 0<=timestamp-value<=450)
        resources=state.get('latest_resources') or []
        if (state.get('latest_workload',{}).get('complete') is not True or len(resources)!=len(managed)
                or {(row['zone'],row['instance_id']) for row in resources}!={(vm.zone,vm.instance_id) for vm in managed}
                or io_status(resources)=='incomplete'):return False
        if not current_job_coverage(state.get('job_tracking',{}),args.job_repository or [args.org+'/oxidex'],timestamp):return False
        for vm in managed:
            key=f'{vm.zone}/{vm.instance_id}';record=next(row for row in records if row['instance_id']==vm.instance_id)
            if (not same_source_inventory(record,asdict(vm))
                    or any(record.get(name)!=getattr(vm,name) for name in ('machine_type','vcpu','ram_gib','spot'))):return False
            receipt=state.get('host_deployments',{})[key];tier=tier_for_shape(vm.vcpu,vm.ram_gib)
            count=receipt['expected_runner_count'];memory=receipt['runner_memory_gib']
            if (not tier or receipt['tier']!=tier.name or isinstance(count,bool) or not isinstance(count,int)
                    or not 1<=count<=tier.maximum_slots or isinstance(memory,bool) or not isinstance(memory,(int,float))
                    or not math.isfinite(memory) or memory<=0
                    or ('initial_deployments' in entry and entry['initial_deployments'].get(key)!=receipt)):return False
            assigned=controller.host_runners(vm,runners,managed)
            if (len(assigned)!=count or {row.name for row in assigned}!={f'gha-{vm.name}-{index}' for index in range(1,count+1)}
                    or len({row.runner_id for row in assigned})!=count
                    or any(not row.online or not controller.runner_is_managed(row) for row in assigned)):return False
            health=state.get('host_health',{})[key]
            if (health.get('instance_id')!=vm.instance_id or health.get('complete') is not True
                    or not health.get('boot_id') or not fresh(health['observed_at'])
                    or health.get('boot_oom_complete') is not True or not isinstance(health.get('boot_host_oom_ids'),list)
                    or state.get('io_history',{}).get(key,{}).get('boot_id')!=health['boot_id']
                    or health.get('io_complete') is not True or pending_kinds(state,key)&{'unknown','container'}
                    or any(event.get('kind')=='unknown' or (event.get('kind')=='container' and event.get('workload') not in ('runner','builder'))
                           for event in health['events'])):return False
            validate_io_pressure(health['io_pressure'])
            row=next(row for row in resources if row['instance_id']==vm.instance_id and row['zone']==vm.zone)
            if (not fresh(row['observed_at']) or any(isinstance(row[name],bool) or not math.isfinite(row[name]) or row[name]<0
                    for name in ('cpu_cores','used_ram_gib','peak_cpu_cores','peak_ram_gib','cpu_fraction','memory_fraction'))):return False
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError):return False
    candidate=copy.deepcopy(state)
    candidate['migration_history']=(candidate.get('migration_history',[])+[
        dict(deferred_at=timestamp,reason='unavailable low-demand plan; current capacity retained',migration=copy.deepcopy(entry))])[-3:]
    candidate['desired_topology']=list(topology)
    if candidate.get('tier_policy',{}).get('direction')=='down':candidate['tier_policy']['streak']=0
    candidate.pop('migration')
    controller.write_scale_state(state_path,candidate)
    state['migration_history']=candidate['migration_history'];state['desired_topology']=candidate['desired_topology']
    if state.get('tier_policy',{}).get('direction')=='down':state['tier_policy']['streak']=0
    state.pop('migration')
    LOG.info('Unavailable low-demand plan deferred; current %s capacity retained',topology)
    return True


def reconcile_migration(vms,runners,args,state,state_path):
    from . import spot_runner_autoscaler as controller
    from .fleet_logging import vm_from_record
    migration=state.get('migration')
    if not migration:return False
    entry=copy.deepcopy(migration)
    if state.get('host_operation'):return True
    fleet=controller.spot_instances(vms)
    by_id={vm.instance_id:vm for vm in fleet}
    for record in migration['sources']:
        reused=next((vm for vm in vms if vm.name==record['name'] and vm.zone==record['zone']),None)
        if reused and reused.instance_id!=record['instance_id']:
            LOG.error('Migration holds: source name has a different provider identity')
            return True
    record_missing_sources(vms,state)
    # Recovery cleans a real lost identity before replacement can resume.
    if any(not row.get('_cleanup_complete') for row in state.get('pending_vm_recovery',{}).values()):
        return True
    if migration.get('awaiting_launch'):
        if migration.get('reason')=='low' and migration.get('launch_attempted') is not True:
            migration['launch_attempted']=True
            controller.write_scale_state(state_path,state)
        pending=migration['awaiting_launch']
        vm=next((vm for vm in fleet if vm.name==pending['name'] and vm.zone==pending['zone']),None)
        if vm:
            if pending.get('instance_id') and vm.instance_id!=pending['instance_id']:
                LOG.error('Migration holds: replacement provider identity changed')
                return True
            tier=tier_for_shape(vm.vcpu,vm.ram_gib)
            if not tier or tier.name!=migration['pending_tiers'][0]:
                raise RuntimeError('Replacement shape differs from requested tier')
            migration['replacement_ids'].append(vm.instance_id)
            migration['pending_tiers'].pop(0)
            if migration.get('pending_slots'):migration['pending_slots'].pop(0)
        migration.pop('awaiting_launch')
        controller.write_scale_state(state_path,state)
    if migration.get('storage_staged_bad_id'):
        if not migration['pending_tiers'] and len(set(migration['replacement_ids']))!=2:
            LOG.error('Staged migration holds: final replacement cardinality is incomplete')
            return True
    if restore_missing_destinations(fleet,state,state_path)=='hold':return True
    paused=migration.get('paused_retirement')
    if paused:
        source=by_id.get(paused['vm']['instance_id'])
        safe=staged_retirement_safe if paused.get('staged_retirement') else retirement_safe
        if source is None or safe(vms,runners,args,state,state_path,paused):
            state['host_operation']=copy.deepcopy(paused)
            migration.pop('paused_retirement')
            controller.write_scale_state(state_path,state)
            return True
    if migration['pending_tiers']:
        # Count provisioning/stopped managed identities too; the hard ceiling
        # cannot be bypassed by an incomplete running-only inventory.
        managed=[vm for vm in vms if vm.spot and vm.name.startswith('oxidex-runners-')]
        tier=TIERS[migration['pending_tiers'][0]]
        slots=(migration.get('pending_slots') or [migration.get('slots') or tier.initial_slots])[0]
        overlap_blocked=(len(managed)>=min(5,args.maximum_hosts)
            or sum(vm.vcpu or 80 for vm in managed)+tier.vcpu>80
            or sum(expected_slots(vm,state) for vm in managed)+slots>100)
        if overlap_blocked:
            if migration.get('storage_staged_bad_id'):
                bad_id=migration['storage_staged_bad_id']
                if not staged_retirement_safe(vms,runners,args,state,state_path,
                        dict(vm=next(row for row in migration['sources'] if row['instance_id']==bad_id),
                             staged_retirement=dict(started_at=migration['started_at'],stage='early'))):
                    LOG.info('Storage staging holds: first canary or surviving capacity is not proven')
                    return True
                migration['early_retirement_ids']=[bad_id]
            else:
                remaining=[vm for vm in fleet if vm.instance_id not in {row['instance_id'] for row in migration['sources']}]
                topology=topology_for(remaining)
                if (state.get('latest_workload',{}).get('complete') is not True
                        or not remaining or 'legacy' in topology
                        or io_status(state.get('latest_resources'))!='healthy'
                        or not fits(topology,state.get('latest_resources'))):
                    LOG.info('Migration holds at overlap ceiling: no safe remaining capacity')
                    return True
            migration['phase']='drain-first'
        else:
            tier=TIERS[migration['pending_tiers'][0]]
            totals={}
            controller.check_running_prices(fleet,args.project,Decimal('999'),totals=totals)
            if len(totals)!=len(fleet):
                LOG.info('Migration launch holds: incomplete current all-disk pricing')
                return True
            current=sum(totals.values(),Decimal(0))
            if migration.get('storage_staged_bad_id'):
                migration['spend_ceiling']='15'
            previous_ceiling=Decimal(migration.get('spend_ceiling','15'))
            if ('spend_ceiling' not in migration or
                    ('overlap_baseline_daily_usd' not in migration and current>=previous_ceiling>=Decimal('15'))):
                allowance=Decimal(0)
                if current>=Decimal('15') and not migration.get('storage_staged_bad_id'):
                    retiring=sum((totals.get(row['name'],Decimal(0)) for row in migration['sources']),Decimal(0))
                    allowance=min(retiring,sum((TIERS[name].max_daily_usd for name in migration['pending_tiers']),Decimal(0)))
                    migration['overlap_baseline_daily_usd']=str(current)
                    migration['overlap_allowance_daily_usd']=str(allowance)
                migration['spend_ceiling']='15' if migration.get('storage_staged_bad_id') else str(max(Decimal('15'),current+allowance))
                controller.write_scale_state(state_path,state)
            discovery={}
            offers=controller.discover_tier_offers(args.project,tier,args.disk_size_gb,getattr(args,'tier_job_memory_gib',15),status=discovery)
            offers=[offer for offer in offers if current+offer.total_daily_usd<Decimal(migration['spend_ceiling'])]
            slots=(migration.get('pending_slots') or [migration.get('slots') or tier.initial_slots])[0]
            if (sum(vm.vcpu or 80 for vm in managed)+tier.vcpu>80
                    or sum(expected_slots(vm,state) for vm in managed)+slots>100):
                LOG.info('Migration holds: overlapping CPU/slot ceiling would be exceeded')
                return True
            if not offers:
                if defer_unused_low_plan(entry,vms,runners,args,state,state_path,discovery):return True
                LOG.info('Migration holds: no qualifying %s offer within disk and overlap budget',tier.name)
                return True
            args.launch_tier=tier.name
            args.launch_slots=slots
            if migration.get('reason')=='low':
                migration['launch_attempted']=True
                controller.write_scale_state(state_path,state)
            controller.scale_up(1,offers,args,state,state_path,
                refresh=lambda: [row for row in controller.discover_tier_offers(args.project,tier,args.disk_size_gb,getattr(args,'tier_job_memory_gib',15))
                                  if current+row.total_daily_usd<Decimal(migration['spend_ceiling'])])
            if state.get('host_operation',{}).get('kind')=='launch':
                migration['awaiting_launch']=state['host_operation']['vm']
                controller.write_scale_state(state_path,state)
            return True
    if not migration['pending_tiers'] and migration['phase']!='drain':
        migration['phase']='healthy'
        if retarget_unhealthy_storage_canary(vms,args,state,state_path):
            return True
        if not replacements_healthy(fleet,runners,args,state,state_path,include_retained=bool(migration['sources'])):
            LOG.info('Migration waits for two healthy complete replacement windows')
            return True
        resources=state.get('latest_resources') or []
        identities={(vm.zone,vm.instance_id) for vm in fleet}
        observed={(row.get('zone'),row.get('instance_id')) for row in resources}
        if (state.get('latest_workload',{}).get('complete') is not True
                or observed!=identities
                or (migration['sources'] and not retirement_io_safe(migration,resources,state))
                or (migration['sources'] and not fits(migration['target'],resources))
                or (not migration['sources'] and not additive_evidence_complete(vms,args,state))):
            LOG.info('Migration keeps source: fresh complete workload/peak demand does not fit target')
            return True
        migration['phase']='drain'
    if migration['phase'] in ('drain','drain-first'):
        if any(row['instance_id'] in by_id for row in migration['sources']) and not retirement_io_safe(migration,state.get('latest_resources'),state):
            LOG.info('Migration retirement holds: incomplete or pressured IO evidence')
            return True
        for record in migration['sources']:
            if (migration.get('storage_staged_bad_id') and migration['pending_tiers']
                    and record['instance_id'] not in migration.get('early_retirement_ids',[])):continue
            vm=by_id.get(record['instance_id'])
            if not vm:continue
            assigned=controller.host_runners(vm,runners,fleet)
            metadata={}
            if migration.get('storage_staged_bad_id'):
                stage='early' if record['instance_id']==migration['storage_staged_bad_id'] else 'final'
                metadata['staged_retirement']=dict(started_at=migration['started_at'],stage=stage,
                    survivor_ids=[item.instance_id for item in fleet if item.instance_id!=vm.instance_id])
                proposal=dict(vm=record,staged_retirement=metadata['staged_retirement'])
                if not staged_retirement_safe(vms,runners,args,state,state_path,proposal):return True
            else:
                metadata['migration_retirement']=dict(started_at=migration['started_at'],
                    survivor_ids=[item.instance_id for item in fleet if item.instance_id!=vm.instance_id])
                proposal=dict(vm=record,migration_retirement=metadata['migration_retirement'])
                if not retirement_safe(vms,runners,args,state,state_path,proposal):return True
            vm=vm_from_record(record)
            controller.begin_host_operation('drain',vm,runners,vms,state,state_path,**metadata,
                runner_names={str(row.runner_id):row.name for row in assigned},
                drain_mode='isolated_group',drain_group_name=f'spot-drain-{vm.name}-{vm.instance_id}')
            return True
        if migration['pending_tiers']:
            migration['phase']='launch'
            controller.write_scale_state(state_path,state)
            return True
        # A persisted drain phase is not a readiness receipt: destinations
        # may have been lost/replaced while sources were already absent.
        if ((migration.get('storage_staged_bad_id') and not staged_replacement_receipts(fleet,state))
                or not missing_sources_clean(fleet,state)
                or not additive_evidence_complete(vms,args,state)
                or not replacements_healthy(fleet,runners,args,state,state_path,include_retained=bool(migration['sources']))
                or (migration['sources'] and (not fits(migration['target'],state.get('latest_resources'))
                    or not retirement_io_safe(migration,state.get('latest_resources'),state)))):
            return True
        state.setdefault('tier_policy',{})['changed_at']=time.time()
        state['desired_topology']=migration['target']
        state['migration_completed_at']=time.time()
        present_ids={vm.instance_id for vm in vms}
        for source in migration['sources']:
            if source['instance_id'] not in present_ids:
                clear_oom(state,f"{source['zone']}/{source['instance_id']}")
        state.pop('migration')
        controller.write_scale_state(state_path,state)
        LOG.info('Tier migration completed: %s',state['desired_topology'])
    return True

def replacements_healthy(fleet,runners,args,state,state_path,include_retained=True):
    from . import spot_runner_autoscaler as controller
    migration=state['migration']
    by_id={vm.instance_id:vm for vm in fleet}
    rows={f"{row['zone']}/{row['instance_id']}":row for row in state.get('latest_resources') or []}
    destinations=list(dict.fromkeys(migration['replacement_ids']+(migration.get('retained_ids',[]) if include_retained else [])))
    healthy=bool(destinations)
    checks=migration.setdefault('healthy_checks',{})
    for identity in destinations:
        vm=by_id.get(identity)
        if not vm:return False
        key=f'{vm.zone}/{vm.instance_id}'
        sample=rows.get(key)
        health=state.get('host_health',{}).get(key,{})
        assigned=controller.host_runners(vm,runners,fleet)
        names={runner.name for runner in assigned if runner.online}
        ready={f'gha-{vm.name}-{i}' for i in range(1,expected_slots(vm,state)+1)}<=names
        observed=time.time()
        # Replacement validation owns the lifecycle barrier, so the normal
        # budget policy cannot repair a constrained container here. Repair
        # only identified containers with fresh host headroom. The remote
        # adjustment owns per-container lease checks and partial outcomes.
        if (ready and sample and sample.get('instance_id')==vm.instance_id
                and health.get('instance_id')==vm.instance_id and health.get('complete') is True
                and 0<=observed-sample.get('observed_at',0)<=450
                and 0<=observed-health.get('observed_at',0)<=450
                and sample['memory_fraction']<.75
                and 'host' not in pending_kinds(state,key)
                and not any(event.get('kind')=='host' for event in health.get('events',[]))):
            repaired=set(migration.get('oom_budget_repairs',[]))
            for kind in ('runner','builder'):
                ids={event['id'] for event in health.get('events',[])
                     if event.get('kind')=='container' and event.get('workload')==kind
                     and event.get('id') and event['id'] not in repaired}
                if ids:remember_oom(state,key,kind,health.get('boot_id'),ids)
                # Already-seen attributed events can leave the bounded
                # telemetry window before busy siblings release their leases.
                ids|=set(obligations_for(state,key).get(kind,{}).get('event_ids',[]))-repaired
                if not ids:continue
                try:
                    outcome=controller.adjust_admission(vm,args,kind)
                except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
                    LOG.warning('Replacement budget repair holds for %s: %s',vm.name,controller.error_detail(exc))
                    continue
                if outcome is True:
                    repaired.update(ids)
                    migration['oom_budget_repairs']=sorted(repaired)
                    clear_oom(state,key,kind)
                    LOG.info('Repaired idle %s memory cap on replacement %s; fresh healthy windows still required',kind,vm.name)
        good=(ready and sample and sample.get('instance_id')==vm.instance_id
              and health.get('instance_id')==vm.instance_id and health.get('boot_id')
              and 0<=observed-sample.get('observed_at',0)<=450
              and health.get('boot_oom_complete') is True and health.get('boot_host_oom_ids')==[]
              and not pending_kinds(state,key)
              and sample['cpu_fraction']<.75 and sample['memory_fraction']<.75
              and io_status([sample])=='healthy'
              and data_storage_status(vm,args)=='healthy'
              and health.get('complete') and not health.get('events')
              and 0<=observed-health.get('observed_at',0)<=450)
        if good and getattr(args,'remote_builder',False):
            good=controller.builder_ready(vm,args)
        tick=sample['observed_at'] if sample else 0
        previous=checks.get(key,{})
        if previous.get('boot_id')!=health.get('boot_id'):previous={}
        count=previous.get('count',0)
        if not good:count=0
        elif tick-previous.get('at',-1e20)>=300:
            count=count+1 if tick-previous.get('at',tick)<=450 else 1
            checks[key]=dict(count=count,at=tick,boot_id=health.get('boot_id'))
        if not good:checks[key]=dict(count=0,at=tick,boot_id=health.get('boot_id'))
        healthy=healthy and count>=2
    controller.write_scale_state(state_path,state)
    return healthy


def staged_retirement_safe(vms,runners,args,state,state_path,operation):
    """Prove current survivors can carry all demand before scoped source retirement."""
    from . import spot_runner_autoscaler as controller
    migration=state.get('migration',{})
    scope=operation.get('staged_retirement',{})
    record=operation.get('vm',{})
    bad_id=migration.get('storage_staged_bad_id')
    sources=migration.get('sources',[])
    if (not bad_id or migration.get('reason')!='storage-pressure canary replacement'
            or migration.get('target')!=['large','large'] or len(sources)!=2
            or scope.get('started_at')!=migration.get('started_at')
            or scope.get('stage') not in ('early','final')
            or not any(row==record for row in sources)
            or (scope['stage']=='early' and record.get('instance_id')!=bad_id)
            or (scope['stage']=='final' and record.get('instance_id')==bad_id)):
        return False
    managed=[vm for vm in vms if vm.spot and vm.name.startswith('oxidex-runners-')]
    by_id={vm.instance_id:vm for vm in managed}
    if (len(managed)>min(5,args.maximum_hosts) or sum(vm.vcpu or 80 for vm in managed)>80
            or sum(expected_slots(vm,state) for vm in managed)>100 or any(not vm.running for vm in managed)):
        return False
    for saved in sources:
        current=by_id.get(saved['instance_id'])
        reused=next((vm for vm in managed if vm.name==saved['name'] and vm.zone==saved['zone']),None)
        if reused and reused.instance_id!=saved['instance_id']:return False
        if current and (current.name!=saved['name'] or current.zone!=saved['zone']
                        or list(current.disks)!=list(saved.get('disks',[]))
                        or not current.disks or any(not disk.get('diskId') for disk in current.disks)):
            return False
    if record['instance_id'] not in by_id or not missing_sources_clean(managed,state):return False
    replacement_ids=migration.get('replacement_ids',[])
    if len(set(replacement_ids))!=len(replacement_ids):return False
    if (scope['stage']=='final' or any(row['instance_id'] not in by_id for row in sources)):
        if len(replacement_ids)!=2 or migration.get('pending_tiers'):return False
    if any(not row.get('_cleanup_complete') for row in state.get('pending_vm_recovery',{}).values()):return False
    survivors=[vm for vm in managed if vm.instance_id!=record['instance_id']]
    topology=topology_for(survivors)
    resources=state.get('latest_resources') or []
    timestamp=time.time()
    try:
        if (not survivors or 'legacy' in topology
                or state.get('latest_workload',{}).get('complete') is not True
                or {(row['zone'],row['instance_id']) for row in resources}!={(vm.zone,vm.instance_id) for vm in managed}
                or len(resources)!=len(managed)
                or any(row.get('windows',0)<1 or not 0<=timestamp-row['observed_at']<=450
                       or any(not math.isfinite(row[name]) or row[name]<0 for name in ('peak_cpu_cores','peak_ram_gib'))
                       for row in resources)
                or not reservation_fits(topology,getattr(args,'tier_job_memory_gib',15))
                or not fits(topology,resources) or not retirement_io_safe(migration,resources,state)
                or io_status([row for row in resources if row['instance_id']!=record['instance_id']])!='healthy'):
            return False
        if scope['stage']=='final' and (bad_id in by_id or migration.get('pending_tiers')
                                       or not fits(migration['target'],resources)):
            return False
        if not staged_replacement_receipts(managed,state):return False
        return replacements_healthy(managed,runners,args,state,state_path)
    except (KeyError,TypeError,ValueError,OverflowError):
        return False


def guard_staged_retirement(vms,runners,args,state,state_path):
    """Refresh all migration retirement proofs, pausing only for lost survivors."""
    from . import spot_runner_autoscaler as controller
    operation=state.get('host_operation',{})
    migration=state.get('migration',{})
    source_id=operation.get('vm',{}).get('instance_id')
    recorded=next((row for row in migration.get('sources',[]) if row['instance_id']==source_id),None)
    if not recorded:return not bool(operation.get('staged_retirement') or operation.get('migration_retirement'))
    if migration.get('storage_staged_bad_id'):
        if not operation.get('staged_retirement'):return False
        safe=staged_retirement_safe
    else:
        # Legacy persisted migration drains acquire scope only from the same
        # saved original VM/disks, never from the fresh provider attachments.
        if not operation.get('migration_retirement'):
            if operation.get('vm')!=recorded:return False
            operation['migration_retirement']=dict(started_at=migration['started_at'],
                survivor_ids=[item.instance_id for item in vms if item.spot and item.name.startswith('oxidex-runners-') and item.instance_id!=source_id])
            controller.write_scale_state(state_path,state)
        safe=retirement_safe
    try:
        current_vms=controller.list_gcloud_vms(args.project,name_prefix='oxidex-runners-')
        controller.observe_vms(current_vms,state)
        record_missing_sources(current_vms,state)
        fresh=controller.list_github_runners(args.org)
        from .tier_controller import evidence_for
        evidence_for(current_vms,fresh,args,state)
        if safe(current_vms,fresh,args,state,state_path,operation):return True
    except (RuntimeError,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        LOG.warning('Migration retirement proof unavailable: %s',controller.error_detail(exc))
        return False
    # A real survivor loss must not monopolize the scheduling barrier and
    # prevent normal identity-scoped loss cleanup / replacement. Persist the
    # original isolation/drain receipt before releasing only its active slot.
    scope=operation.get('staged_retirement') or operation['migration_retirement']
    alive={vm.instance_id for vm in current_vms if vm.running}
    source_id=operation['vm']['instance_id']
    blocked_losses={row.get('instance_id') for row in state.get('pending_vm_recovery',{}).values()
                    if not row.get('_cleanup_complete') and row.get('instance_id')
                    and row.get('spot') and row.get('name','').startswith('oxidex-runners-')
                    and row.get('instance_id')!=source_id and row.get('instance_id') not in alive}
    if (scope.get('started_at')==migration.get('started_at') and source_id in alive
            and (set(scope.get('survivor_ids',[]))-alive or blocked_losses)):
        saved=migration.get('paused_retirement')
        if saved and saved!=operation:return False
        migration['paused_retirement']=copy.deepcopy(operation)
        controller.write_scale_state(state_path,state)
        state.pop('host_operation')
        controller.write_scale_state(state_path,state)
        LOG.warning('Paused exact migration retirement while missing survivors recover: %s',source_id)
    return False


def restore_missing_destinations(fleet,state,state_path):
    """Restore exact receipted destinations after normal loss cleanup."""
    from . import spot_runner_autoscaler as controller
    migration=state['migration'];by_id={vm.instance_id:vm for vm in fleet}
    for cohort in ('replacement_ids','retained_ids'):
        for identity in list(migration.get(cohort,[])):
            if identity in by_id:continue
            saved=next((row for row in migration.get('retained_records',[]) if row['instance_id']==identity),{})
            if saved and any(vm.name==saved['name'] and vm.zone==saved['zone'] and vm.instance_id!=identity for vm in fleet):
                return 'hold'
            receipt=next((row for key,row in state.get('host_deployments',{}).items() if key.endswith('/'+identity)),{})
            tier_name=receipt.get('tier')
            if not tier_name and saved:
                tier=tier_for_shape(saved.get('vcpu'),saved.get('ram_gib'))
                tier_name=tier.name if tier else None
            if tier_name not in TIERS:return 'hold'
            count=receipt.get('expected_runner_count') or saved.get('_expected_runner_count') or TIERS[tier_name].initial_slots
            if not isinstance(count,int) or isinstance(count,bool) or not 1<=count<=TIERS[tier_name].maximum_slots:return 'hold'
            migration[cohort].remove(identity)
            migration['phase']='launch'
            migration['healthy_checks']={}
            migration['pending_tiers'].append(tier_name)
            migration.setdefault('pending_slots',[]).append(count)
            controller.write_scale_state(state_path,state)
    return None


def retirement_safe(vms,runners,args,state,state_path,operation):
    """Recheck real destination health and capacity through every retirement retry."""
    from . import spot_runner_autoscaler as controller
    migration=state.get('migration',{});record=operation.get('vm',{})
    scope=operation.get('migration_retirement',{})
    if (not migration.get('sources') or scope.get('started_at')!=migration.get('started_at')
            or not any(row==record for row in migration['sources'])):return False
    managed=[vm for vm in vms if vm.spot and vm.name.startswith('oxidex-runners-')]
    if (any(not vm.running for vm in managed) or len(managed)>min(5,getattr(args,'maximum_hosts',5))
            or sum(vm.vcpu or 80 for vm in managed)>80 or sum(expected_slots(vm,state) for vm in managed)>100):return False
    current=next((vm for vm in managed if vm.instance_id==record['instance_id']),None)
    if (not current or current.name!=record['name'] or current.zone!=record['zone']
            or list(current.disks)!=list(record.get('disks',[])) or not current.disks
            or any(not disk.get('diskId') for disk in current.disks)):return False
    for saved in migration['sources']:
        current_source=next((vm for vm in managed if vm.instance_id==saved['instance_id']),None)
        if any(vm.name==saved['name'] and vm.zone==saved['zone'] and vm.instance_id!=saved['instance_id'] for vm in managed):return False
        if current_source and not same_source_inventory(saved,asdict(current_source)):return False
    if not missing_sources_clean(managed,state):return False
    destinations=set(migration['replacement_ids']+migration.get('retained_ids',[]))
    survivors=[vm for vm in managed if vm.instance_id in destinations]
    if len(survivors)!=len(destinations) or not survivors:return False
    if any(not row.get('_cleanup_complete') for row in state.get('pending_vm_recovery',{}).values()):return False
    topology=topology_for(survivors);resources=state.get('latest_resources') or [];timestamp=time.time()
    try:
        if (state.get('latest_workload',{}).get('complete') is not True or 'legacy' in topology
                or len(resources)!=len(managed)
                or {(row['zone'],row['instance_id']) for row in resources}!={(vm.zone,vm.instance_id) for vm in managed}
                or any(not 0<=timestamp-row['observed_at']<=450
                       or any(not math.isfinite(row[name]) or row[name]<0 for name in ('peak_cpu_cores','peak_ram_gib'))
                       for row in resources)
                or not retirement_io_safe(migration,resources,state)
                or not source_oom_proven(migration,current,state,timestamp)
                or not reservation_fits(topology,getattr(args,'tier_job_memory_gib',15))
                or not fits(topology,resources)):return False
        return replacements_healthy(managed,runners,args,state,state_path)
    except (KeyError,TypeError,ValueError,OverflowError):return False


def source_oom_proven(migration,vm,state,timestamp):
    """Ambiguous source history holds; an exact recorded OOM capacity response can retire it."""
    key=f'{vm.zone}/{vm.instance_id}';health=state.get('host_health',{}).get(key,{})
    try:
        if (health.get('instance_id')!=vm.instance_id or health.get('complete') is not True
                or not health.get('boot_id') or not 0<=timestamp-health['observed_at']<=450
                or health.get('boot_oom_complete') is not True
                or not isinstance(health.get('boot_host_oom_ids'),list)
                or pending_kinds(state,key) & {'unknown','container'}):return False
        events=health.get('events',[])
        if any(event.get('kind')=='unknown' or (event.get('kind')=='container' and event.get('workload') not in ('runner','builder')) for event in events):
            return False
        globals=set(health['boot_host_oom_ids'])|{event.get('id') for event in events if event.get('kind')=='host'}
        origin=migration.get('source_oom_origins',{}).get(key,{})
        pending=obligations_for(state,key).get('host')
        if not globals and not pending:return True
        # Deleting an OOM source is a capacity response only when the original
        # transaction recorded these exact same-boot decisions and adds RAM.
        return (migration.get('reason')=='oom' and migration.get('oom_capacity_response') is True
                and origin.get('boot_id')==health['boot_id'] and origin.get('quarantined') is False
                and bool(origin.get('event_ids')) and globals<=set(origin['event_ids'])
                and (pending is None or (pending.get('boot_id')==origin['boot_id']
                     and pending.get('quarantined') is False and set(pending.get('event_ids',[]))<=set(origin['event_ids']))))
    except (KeyError,TypeError,ValueError):return False


def additive_evidence_complete(vms,args,state):
    """Growth keeps all existing capacity; settle it so demand can drive the next step."""
    managed=[vm for vm in vms if vm.spot and vm.name.startswith('oxidex-runners-')]
    migration=state['migration'];resources=state.get('latest_resources') or [];timestamp=time.time()
    try:
        return (state.get('latest_workload',{}).get('complete') is True
            and all(vm.running and vm.instance_id for vm in managed)
            and len({vm.instance_id for vm in managed})==len(managed)
            and len(managed)<=min(5,getattr(args,'maximum_hosts',5))
            and sum(vm.vcpu or 80 for vm in managed)<=80
            and sum(expected_slots(vm,state) for vm in managed)<=100
            and Counter(topology_for(managed))==Counter(migration['target'])
            and reservation_fits(migration['target'],getattr(args,'tier_job_memory_gib',15))
            and len(resources)==len(managed)
            and {(row['zone'],row['instance_id']) for row in resources}=={(vm.zone,vm.instance_id) for vm in managed}
            and all(row.get('windows',0)>=1 and 0<=timestamp-row['observed_at']<=450
                    and all(math.isfinite(row[name]) and row[name]>=0 for name in ('peak_cpu_cores','peak_ram_gib'))
                    for row in resources))
    except (KeyError,TypeError,ValueError,OverflowError):return False


def same_source_inventory(first,second):
    return (all(first.get(name)==second.get(name) and first.get(name) for name in ('instance_id','zone','name'))
        and bool(first.get('disks')) and list(first['disks'])==list(second.get('disks',[]))
        and all(disk.get('diskId') for disk in first['disks']))


def source_absence_proven(source,state):
    migration=state.get('migration',{})
    retired=migration.get('completed_retirements',{}).get(source['instance_id'],{})
    return ((migration.get('started_at') is not None and retired.get('started_at')==migration.get('started_at')
            and retired.get('absence_confirmed') is True and retired.get('disks_absence_confirmed') is True and same_source_inventory(source,retired.get('vm',{})))
        or any(row.get('_cleanup_complete') is True and row.get('_disks_absent') is True and same_source_inventory(source,row)
            for row in state.get('pending_vm_recovery',{}).values()))


def missing_sources_clean(fleet,state):
    """Only confirmed lifecycle/recovery completion explains an absent original source."""
    migration=state.get('migration',{});present={vm.instance_id for vm in fleet}
    for source in migration.get('sources',[]):
        if source['instance_id'] in present:continue
        if not source_absence_proven(source,state):return False
    return True


def record_retirement_completion(operation,state):
    """Called only after VM, assigned runner and drain-group absence is confirmed."""
    migration=state.get('migration',{});scope=operation.get('staged_retirement') or operation.get('migration_retirement',{})
    source=next((row for row in migration.get('sources',[]) if same_source_inventory(row,operation.get('vm',{}))),None)
    if (source and migration.get('started_at') is not None and scope.get('started_at')==migration.get('started_at')
            and operation.get('kind') in ('delete','delete_cleanup') and operation.get('drained') is True
            and operation.get('migration_source_disks_absent') is True):
        migration.setdefault('completed_retirements',{})[source['instance_id']]=dict(
            vm=copy.deepcopy(source),started_at=migration['started_at'],absence_confirmed=True,disks_absence_confirmed=True)


def record_missing_sources(vms,state):
    """Recover unreceipted original sources even when an earlier roster omitted them."""
    from .fleet_logging import vm_from_record,recovery_key
    present={vm.instance_id for vm in vms};operation_id=state.get('host_operation',{}).get('vm',{}).get('instance_id')
    for source in state.get('migration',{}).get('sources',[]):
        if source['instance_id'] in present or source['instance_id']==operation_id:continue
        # A completed exact receipt can legitimately explain this disappearance.
        if source_absence_proven(source,state):continue
        pending=state.setdefault('pending_vm_recovery',{})
        existing=next((row for row in pending.values() if same_source_inventory(source,row)),None)
        if existing is not None:
            # Old completion markers without disk proof require exact cleanup
            # again; never turn an unsupported marker into permanent deadlock.
            if existing.get('_cleanup_complete') and existing.get('_disks_absent') is not True:
                existing['_cleanup_complete']=False
            continue
        vm=vm_from_record(source);key=recovery_key(f'{vm.zone}/{vm.name}',vm,pending)
        pending.setdefault(key,copy.deepcopy(source))


def staged_replacement_receipts(fleet,state):
    migration=state['migration'];identities=migration.get('replacement_ids',[])
    if not identities or len(set(identities))!=len(identities):return False
    by_id={vm.instance_id:vm for vm in fleet}
    for identity in identities:
        vm=by_id.get(identity)
        if not vm:return False
        receipt=state.get('host_deployments',{}).get(f'{vm.zone}/{identity}',{})
        if ((vm.vcpu,vm.ram_gib)!=(16,64) or receipt.get('tier')!='large'
                or receipt.get('runner_memory_gib',0)<15):return False
    return True


def confirm_migration_source_drives(operation,args,state):
    """Refresh every original disk's identity-scoped absence, including cached cleanup retries."""
    from .fleet_logging import vm_from_record
    from .vm_recovery import cleanup_lost_vm,DISK_SOURCE
    record=operation.get('vm',{})
    source=next((row for row in state.get('migration',{}).get('sources',[])
                 if row.get('instance_id')==record.get('instance_id')),None)
    if source is None:return True
    operation.pop('migration_source_disks_absent',None)
    if not same_source_inventory(source,record):return False
    # The cleanup primitive compares exact provider selfLinks. Aliases cannot
    # prove absence against that inventory, even when their path names match.
    for disk in source['disks']:
        if disk.get('type')=='SCRATCH':continue
        value=disk.get('source')
        match=DISK_SOURCE.fullmatch(value) if isinstance(value,str) else None
        if (not match or value!=f'https://www.googleapis.com/compute/v1/projects/{match[1]}/{match[2]}/{match[3]}/disks/{match[4]}'):
            return False
    if not cleanup_lost_vm(vm_from_record(source),args.project):return False
    operation['migration_source_disks_absent']=True
    return True
