"""Regional USD/GiB-month fixture prices for offline selection tests."""
from decimal import Decimal


def storage_prices_for(offers):
    return {offer.region: {'pd-standard:capacity': Decimal('.04'),
                          'pd-balanced:capacity': Decimal('.10'),
                          'pd-ssd:capacity': Decimal('.17'),
                          'hyperdisk-balanced:capacity': Decimal('.08')}
            for offer in offers}
