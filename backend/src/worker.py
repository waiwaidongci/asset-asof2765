import logging
import time
from pathlib import Path
from src.services.execution import claim, execute, materialize_due
from src.services import stocktake
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def main():
    while True:
        try:
            materialize_due()
            lease = claim()
            if lease:
                execute(lease)
            stocktake_lease = stocktake.claim()
            if stocktake_lease:
                stocktake.execute(stocktake_lease)
            heartbeat.touch()
            if not lease and not stocktake_lease:
                wait()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()
