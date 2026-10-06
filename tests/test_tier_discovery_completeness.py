"""Bounded synthetic discovery controls; provider access is forbidden."""
import unittest
from contextlib import ExitStack
from decimal import Decimal
from unittest.mock import AsyncMock,Mock,patch

from lib import spot_finder as finder
from lib import spot_runner_autoscaler as controller
from lib.compute_api import Auth
from lib.runner_tiers import TIERS


class TierDiscoveryCompletenessTests(unittest.TestCase):
    def discover(self,*,unreachable=(),missing_compute=None,missing_storage=None,irrelevant_missing=False):
        catalog={('spot','r','n2',part):.01 for part in ('CPU','RAM')}
        if missing_compute:catalog.pop(('spot','r','n2',missing_compute))
        machines=[dict(name='n2-standard-8',zone='r-a',guestCpus=8,memoryMb=32768)]
        if irrelevant_missing:machines.append(dict(name='n2-standard-16',zone='r-a',guestCpus=16,memoryMb=65536))
        if irrelevant_missing:machines[-1]['name']='n4-standard-16'
        rates={kind+':capacity':Decimal('.10') for kind in ('pd-standard','pd-balanced','pd-ssd')}
        if missing_storage:rates.pop(missing_storage+':capacity')
        status={}
        with ExitStack() as stack:
            stack.enter_context(patch.object(controller.subprocess,'run',side_effect=AssertionError('provider command forbidden')))
            stack.enter_context(patch.object(finder,'wait_for_quota_cooldown',new=AsyncMock()))
            stack.enter_context(patch.object(finder,'AsyncGoogleApi',new=Mock()))
            stack.enter_context(patch.object(finder,'list_regions',new=AsyncMock(return_value=[{'name':'r'}])))
            stack.enter_context(patch.object(finder,'list_machines',new=AsyncMock(return_value=(machines,list(unreachable)))))
            stack.enter_context(patch.object(finder,'spot_price',new=AsyncMock(side_effect=AssertionError('capacity request forbidden'))))
            for name,value in (('clear_catalog_cache',None),('get_auth',Auth('synthetic','p')),
                               ('catalog_skus',[]),('parse_component_rates',catalog),('storage_rates',rates),
                               ('load_launch_inventory',None),('launchable_candidates',([],[]))):
                stack.enter_context(patch.object(controller,name,new=Mock(return_value=value)))
            result=controller.discover_tier_offers('p',TIERS['expanded'],100,15,status=status)
        self.assertEqual(result,[])
        return status

    def test_unreachable_machine_scope_is_incomplete_even_with_no_price_errors(self):
        self.assertIs(self.discover(unreachable=['r-b']).get('complete'),False)

    def test_required_compute_component_absence_is_not_complete_empty_discovery(self):
        for part in ('CPU','RAM'):
            with self.subTest(part=part):self.assertIs(self.discover(missing_compute=part).get('complete'),False)

    def test_missing_compatible_boot_or_data_quote_is_incomplete(self):
        for kind in ('pd-standard','pd-balanced','pd-ssd'):
            with self.subTest(kind=kind):self.assertIs(self.discover(missing_storage=kind).get('complete'),False)

    def test_complete_priced_inventory_with_no_affordable_offers_is_complete(self):
        self.assertIs(self.discover().get('complete'),True)

    def test_unpriced_other_shape_does_not_hide_required_shape_absence_proof(self):
        self.assertIs(self.discover(irrelevant_missing=True).get('complete'),True)


if __name__=='__main__':unittest.main()
