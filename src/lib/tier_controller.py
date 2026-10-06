"""Tier-mode orchestration and a provider/state read-only planning surface."""
import copy
import json
import logging
import subprocess
import time
from decimal import Decimal
from .fleet_evidence import record_resources,record_io_pressure,workload_signals,read_host_events,remember_jobs,current_job_coverage
from .fleet_migration import topology_for,begin_migration,reconcile_migration,expected_slots
from .tier_policy import plan_capacity,compatible_recovery_tier,reservation_fits
from .runner_tiers import TIERS,tier_for_shape,next_topology,validate_topology
from .oom_obligations import sync_pending,pending_kinds,remember_oom,clear_oom,obligations_for,promote_attributed_ooms

LOG=logging.getLogger('spot_runner_autoscaler')


def evidence_for(vms,runners,args,state):
    from . import spot_runner_autoscaler as controller
    fleet=controller.spot_instances(vms)
    timestamp=time.time()
    try:
        samples=controller.read_utilization(args.project,fleet,timestamp)
    except (RuntimeError,OSError,ValueError,subprocess.SubprocessError) as exc:
        LOG.warning('Tier metrics unavailable: %s',controller.error_detail(exc))
        samples=None
    evidence=record_resources(state,fleet,samples,timestamp)
    state['latest_resources']=evidence
    health={}
    seen=set(state.get('seen_oom_ids',[]))
    sync_pending(state)
    for vm in fleet:
        key=f'{vm.zone}/{vm.instance_id}'
        try:
            previous_boot=state.get('host_health',{}).get(key,{}).get('boot_id')
            attribution_ids=sorted({identity for kind,origin in obligations_for(state,key).items()
                if kind in ('unknown','container') and origin.get('quarantined') is False
                and previous_boot and origin.get('boot_id')==previous_boot
                for identity in origin.get('event_ids',[])})
            observed=read_host_events(args.project,vm,attribution_ids)
            if not observed.get('complete') or abs(time.time()-observed['observed_at'])>450:
                raise RuntimeError('Incomplete or stale host event window')
            if pending_kinds(state,key) & {'unknown','container'}:
                promote_attributed_ooms(state,key,observed)
            health[key]=observed
            for event in observed['events']:
                if event['id'] not in seen:
                    seen.add(event['id'])
                    kind='host' if event['kind']=='host' else event.get('workload','unknown')
                    remember_oom(state,key,kind,observed.get('boot_id'),[event['id']])
        except (RuntimeError,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
            LOG.warning('Host OOM/readiness evidence missing for %s: %s',vm.name,controller.error_detail(exc))
    record_io_pressure(state,evidence,health,time.time())
    state['host_health']=health
    state['seen_oom_ids']=sorted(seen)[-1000:]
    data=state.get('job_tracking',{})
    repositories=args.job_repository or [args.org+'/oxidex']
    complete=(current_job_coverage(data,repositories,timestamp)
              and len(health)==len(fleet))
    signals=workload_signals(list(data.get('capacity_jobs',{}).values()),
                             [set(row.labels) for row in runners if controller.runner_is_managed(row)],
                             timestamp,group_ids={10})
    signals['complete']=complete
    signals['minimum_job_memory_gib']=getattr(args,'tier_job_memory_gib',15)
    kinds=set().union(*(pending_kinds(state,key) for key in health))
    signals['host_oom']='host' in kinds
    signals['container_oom']='runner' in kinds
    signals['builder_oom']='builder' in kinds
    signals['unknown_oom']=bool(kinds & {'unknown','container'})
    state['latest_workload']=signals
    return evidence,signals,timestamp


def check_tiers(vms,runners,args,state,state_path):
    from . import spot_runner_autoscaler as controller
    fleet=controller.spot_instances(vms)
    evidence,signals,timestamp=evidence_for(vms,runners,args,state)
    controller.protected_heavy_routes(vms,runners,state)
    if controller.heavy_route_capacity_hold(state) or controller.runner_inventory_stale(state):
        LOG.warning('Tier changes held: protected heavy route or stale runner inventory; metrics retained')
        controller.write_scale_state(state_path,state)
        return
    if state.get('migration'):
        reconcile_migration(vms,runners,args,state,state_path)
        controller.write_scale_state(state_path,state)
        return
    topology=topology_for(fleet)
    tail=next((vm for vm in fleet if tier_for_shape(vm.vcpu,vm.ram_gib)
               and tier_for_shape(vm.vcpu,vm.ram_gib).name==topology[-1]),None) if topology else None
    if tail:signals['slots']=expected_slots(tail,state)
    # Lost-host cleanup has finished; restore the previously committed topology
    # using allowed tiers, independently of the retired 25-runner floor.
    pending=state.get('pending_vm_recovery',{})
    if pending and all(row.get('_cleanup_complete') for row in pending.values()):
        target=tuple(state.get('desired_topology') or ())
        if not target:
            lost=[tier_for_shape(row.get('vcpu'),row.get('ram_gib')) for row in pending.values()]
            target=topology+tuple(tier.name if tier else 'starting' for tier in lost)
            target=tuple(sorted(target,key=lambda name:-TIERS[name].vcpu))
        # A pre-upgrade multi-32 fleet cannot be recreated as a malformed tail.
        if len(target)>1 and any(name not in ('large','bulky') for name in target[:-1]):
            target=('large',)*min(5,len(target))
        reservation=signals.get('minimum_job_memory_gib',getattr(args,'tier_job_memory_gib',15))
        compatible={name:compatible_recovery_tier(name,reservation) for name in target}
        if any(name is None for name in compatible.values()):
            LOG.warning('Loss recovery holds: known workload memory cannot fit an allowed tier')
            controller.write_scale_state(state_path,state)
            return
        target=tuple(compatible[name] for name in target)
        try:
            validate_topology(target)
        except ValueError as exc:
            LOG.warning('Loss recovery holds at aggregate topology boundary: %s',exc)
            controller.write_scale_state(state_path,state)
            return
        state['pending_vm_recovery']={}
        state['price_replacement_target']=0
        if topology!=target:
            slot_counts=[]
            for record in pending.values():
                tier=tier_for_shape(record.get('vcpu'),record.get('ram_gib'))
                receipt=state.get('host_deployments',{}).get(f"{record['zone']}/{record['instance_id']}",{})
                if tier and receipt.get('tier')==tier.name and receipt.get('expected_runner_count') is not None:
                    slot_counts.append((compatible.get(tier.name,tier.name),receipt['expected_runner_count']))
            begin_migration(fleet,target,state,state_path,'loss recovery',slot_counts=slot_counts)
            reconcile_migration(vms,runners,args,state,state_path)
            return
    decision=plan_capacity(topology,evidence,signals,state.setdefault('tier_policy',{}),timestamp)
    if 'legacy' in topology and not args.migrate_oversized:
        decision=dict(target=topology,action='hold',reason='oversized migration disabled')
    state['tier_decision']={**decision,'observed_at':timestamp,'resources':evidence}
    LOG.info('Tier decision: topology=%s target=%s reason=%s absolute demand=%s',
             topology,decision['target'],decision['reason'],evidence)
    if decision['action']=='topology':
        # Host-OOM obligations belong to exact VM identities; retaining an affected
        # host would prevent replacement readiness from ever clearing that obligation.
        force_ids=[vm.instance_id for vm in fleet
                   if 'host' in pending_kinds(state,f'{vm.zone}/{vm.instance_id}')] if decision['reason']=='oom' else []
        begin_migration(fleet,decision['target'],state,state_path,decision['reason'],decision.get('slots'),force_ids=force_ids)
        reconcile_migration(vms,runners,args,state,state_path)
    elif decision['action']=='density':
        begin_migration(fleet,topology,state,state_path,decision['reason'],decision['slots'],force_ids=[tail.instance_id])
        reconcile_migration(vms,runners,args,state,state_path)
    elif decision['action']=='budget':
        # Apply only to an idle host. Active job/container limits are never
        # changed under a lease; retain the pending OOM until confirmed.
        for vm in fleet:
            key=f'{vm.zone}/{vm.instance_id}'
            for kind in ('runner','builder'):
                if kind not in pending_kinds(state,key):continue
                outcome=controller.adjust_admission(vm,args,kind)
                if outcome is True:
                    clear_oom(state,key,kind)
                elif outcome=='capacity':
                    target=next_topology(topology,'up','oom')
                    reservation=signals.get('minimum_job_memory_gib',getattr(args,'tier_job_memory_gib',15))
                    while target!=topology and not reservation_fits(target,reservation):
                        larger=next_topology(target,'up','memory')
                        if larger==target:
                            target=topology
                            break
                        target=larger
                    if target!=topology:
                        begin_migration(fleet,target,state,state_path,'container cap exceeds measured host envelope',
                                        TIERS[target[-1]].initial_slots)
                        reconcile_migration(vms,runners,args,state,state_path)
                        break
                    LOG.warning('Budget recovery holds: known workload memory cannot fit an allowed growth tier')
            if state.get('migration'):break
    elif topology and 'legacy' not in topology:
        state.setdefault('desired_topology',list(topology))
        # Price-driven retirement also follows replacement readiness and drain;
        # never use the legacy eviction that interrupts active work.
        totals={}
        controller.check_running_prices(fleet,args.project,Decimal('999'),totals=totals)
        overpriced=[vm for vm in fleet if vm.name in totals and not TIERS[tier_for_shape(vm.vcpu,vm.ram_gib).name].affordable(totals[vm.name])]
        if overpriced and evidence and signals['complete']:
            source=overpriced[0]
            begin_migration(fleet,topology,state,state_path,'tier price replacement',
                            expected_slots(source,state),force_ids=[source.instance_id])
            reconcile_migration(vms,runners,args,state,state_path)
    controller.write_scale_state(state_path,state)


def dry_run(args,saved):
    from . import spot_runner_autoscaler as controller
    from .job_recovery import JobRecovery,utc_now
    state=copy.deepcopy(saved)
    vms=controller.list_gcloud_vms(args.project,name_prefix='oxidex-runners-')
    runners=controller.list_github_runners(args.org)
    # Read job endpoints only. Never invoke recovery polling or a POST rerun.
    tracker=JobRecovery(state,lambda:None,args.job_repository or [args.org+'/oxidex'])
    for repo in tracker.repositories:
        try:
            observed=utc_now()
            tracker.begin_scan(repo,observed)
            runs,jobs=tracker._scan(repo,observed)
            remember_jobs(tracker.data,repo,runs,jobs,observed)
            tracker.data['last_poll_by_repository'][repo]=observed
            tracker.finish_scan(repo)
        except (RuntimeError,OSError,ValueError,KeyError,subprocess.SubprocessError):
            tracker.data['last_poll_by_repository'].pop(repo,None)
    evidence,signals,timestamp=evidence_for(vms,runners,args,state)
    fleet=controller.spot_instances(vms)
    topology=topology_for(fleet)
    decision=plan_capacity(topology,evidence,signals,state.setdefault('tier_policy',{}),timestamp)
    if 'legacy' in topology and not args.migrate_oversized:
        decision=dict(target=topology,action='hold',reason='oversized migration disabled')
    offers={}
    for name in set(decision['target'])-{'legacy'}:
        candidates=controller.discover_tier_offers(args.project,TIERS[name],args.disk_size_gb,getattr(args,'tier_job_memory_gib',15))
        offers[name]=[dict(machine=row.machine_type,zone=row.zone,daily_usd=str(row.total_daily_usd),
                           boot_disk=row.boot_disk_type,data_disk=row.data_disk_type) for row in candidates[:10]]
    print(json.dumps(dict(decision=decision,resources=evidence,workload=signals,offers=offers,
                         pending_migration=state.get('migration')),indent=2))
    return 0
