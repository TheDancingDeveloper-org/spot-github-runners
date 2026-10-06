import unittest
from decimal import Decimal
from unittest.mock import patch
from lib.compute_api import Offer
from lib.runner_tiers import TIERS
from lib.spot_runner_autoscaler import eligible_tier_offers


class TierDiscoveryTests(unittest.TestCase):
    def test_exact_shape_full_storage_and_strict_tier_cap(self):
        offers = [Offer('us-west3','c2d-standard-8',8,32,.04),
                  Offer('us-west3','c2d-highcpu-8',8,16,.03),
                  Offer('us-west3','c2d-standard-32',32,128,.04)]
        with patch('lib.spot_runner_autoscaler.cheapest_storage',return_value=('pd-standard','pd-balanced',Decimal('6'))):
            result = eligible_tier_offers(offers,TIERS['expanded'],{'us-west3':{}},100)
        self.assertEqual([row.machine_type for row in result],['c2d-standard-8'])
        # 0.05/hour is $1.20 compute. $6/month disks put it above $1.25.
        offers[0] = Offer('us-west3','c2d-standard-8',8,32,.05)
        with patch('lib.spot_runner_autoscaler.cheapest_storage',return_value=('pd-standard','pd-balanced',Decimal('6'))):
            self.assertEqual(eligible_tier_offers(offers,TIERS['expanded'],{'us-west3':{}},100),[])

    def test_missing_disk_rate_is_not_compute_only_authorization(self):
        offer = Offer('us-west3','c2d-standard-4',4,16,.01)
        self.assertEqual(eligible_tier_offers([offer],TIERS['small'],{},100),[])
