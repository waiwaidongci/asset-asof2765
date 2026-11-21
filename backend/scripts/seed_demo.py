import json
from sqlalchemy import func, select
from src.config.database import Session
from src.constants.enums import Role
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.domain.stocktake.demo import demo_scans, demo_tools
from src.models.identity import User
from src.models.source import Dataset
from src.models.stocktake import StocktakeBatch
from src.services import stocktake
from src.services.datasets import save
from src.utils.security import hash_password


def seed():
    with Session.begin() as db:
        for role in Role:
            if not db.get(User, role.value):
                db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-demo-2611')))
        item, created = save(db, SPEC['default_dataset'], SPEC['sample_name'], demo_rows(), 'seed')
        stocktake_batch_id = None
        if not db.scalar(select(StocktakeBatch).where(StocktakeBatch.name == '桂桥 11 月扫码实盘（演示）')):
            batch = stocktake.create_batch(
                db, '桂桥 11 月扫码实盘（演示）', '2026-11-30', demo_tools(), 'seed')
            stocktake.add_scans(db, batch, demo_scans(), 'seed')
            stocktake_batch_id = batch.id
        return {'users': db.scalar(select(func.count()).select_from(User)),
                'datasets': db.scalar(select(func.count()).select_from(Dataset)),
                'seed_dataset_id': item.id, 'row_count': item.row_count, 'checksum': item.checksum,
                'created_dataset': created, 'stocktake_batch_id': stocktake_batch_id}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
