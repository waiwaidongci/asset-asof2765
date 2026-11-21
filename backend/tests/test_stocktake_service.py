"""stocktake 服务层集成测试：用 SQLite 内存库验证租约、并发补扫与重算口径。

仅依赖标准库与 SQLAlchemy，运行：python3 -m unittest tests.test_stocktake_service
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DATABASE_URL', 'sqlite+pysqlite:///:memory:')
os.environ.setdefault('JWT_SECRET', 'test-secret')

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
import src.config.database as database  # noqa: E402
from src.config.database import Base  # noqa: E402
from src.models.stocktake import DisputeGroup, DisputeResolution, ScanRecord, StocktakeBatch  # noqa: E402
from src.services import stocktake  # noqa: E402

engine = create_engine('sqlite+pysqlite:///:memory:', future=True)
TestSession = sessionmaker(engine, expire_on_commit=False)
_PREVIOUS_SESSION = None


def setUpModule():
    # 仅在本模块测试期间接管全局 Session，结束后恢复，避免污染其他测试模块。
    global _PREVIOUS_SESSION
    _PREVIOUS_SESSION = database.Session
    database.Session = TestSession
    Base.metadata.create_all(engine)


def tearDownModule():
    database.Session = _PREVIOUS_SESSION


def t(tool_id, status='in_custody', name=None):
    return {'tool_id': tool_id, 'name': name or f'工具{tool_id}', 'status': status}


def s(scan_id, candidates, **extra):
    return {'scan_id': scan_id, 'candidates': list(candidates), **extra}


class StocktakeServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)

    def setUp(self):
        # 每个用例独立的内存库（同一连接，表数据清空即可）。
        for model in (DisputeResolution, DisputeGroup, ScanRecord, StocktakeBatch):
            TestSession().execute(model.__table__.delete())
            TestSession().commit()

    def _create(self, tools=None):
        tools = tools or [t('A'), t('B'), t('C'), t('L1', 'checked_out'), t('R1', 'retired')]
        with TestSession.begin() as db:
            batch = stocktake.create_batch(db, '11 月月末实盘', '2026-11-30', tools, 'analyst')
            return batch.id

    def test_full_reconcile_persists_result_and_groups(self):
        batch_id = self._create()
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [s('S1', ['A', 'B']), s('S2', ['B', 'A']), s('S3', ['C'])], 'analyst')
        lease = stocktake.claim()
        self.assertIsNotNone(lease)
        self.assertEqual(lease[0], batch_id)
        stocktake.execute(lease)
        with TestSession() as db:
            batch = db.get(StocktakeBatch, batch_id)
            self.assertEqual(batch.status, stocktake.COMPLETE)
            self.assertEqual(batch.version, 2)
            self.assertEqual(batch.result['metrics']['confirmed'], 1)
            self.assertEqual(batch.result['metrics']['disputed_groups'], 1)
            groups = db.scalars(select(DisputeGroup).where(DisputeGroup.batch_id == batch_id)).all()
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].version, 2)
        # 完成后没有待领取任务。
        self.assertIsNone(stocktake.claim())

    def test_concurrent_superseded_lease_does_not_overwrite_new_ledger(self):
        batch_id = self._create()
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [s('S1', ['A'])], 'analyst')
        lease_old = stocktake.claim()
        # 计算进行中又来一批补扫（新账），并再次排队。
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id, with_for_update=True)
            stocktake.add_scans(db, batch, [s('S2', ['B'])], 'analyst')
        # 旧租约此时才算出结果，必须被拒绝落账；批次回到排队状态等待新租约。
        stocktake.execute(lease_old)
        with TestSession() as db:
            batch = db.get(StocktakeBatch, batch_id)
            self.assertEqual(batch.status, stocktake.QUEUED)
            self.assertIsNone(batch.result)
        # 新租约按新账重算，盘到 2 件。
        lease_new = stocktake.claim()
        self.assertIsNotNone(lease_new)
        stocktake.execute(lease_new)
        with TestSession() as db:
            batch = db.get(StocktakeBatch, batch_id)
            self.assertEqual(batch.status, stocktake.COMPLETE)
            self.assertEqual(batch.result['metrics']['confirmed'], 2)
            self.assertEqual({row['scan_id'] for row in batch.scans}, {'S1', 'S2'})

    def test_interrupted_retry_does_not_double_count(self):
        batch_id = self._create()
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [s('S1', ['A', 'B']), s('S2', ['B', 'A'])], 'analyst')
        first = stocktake.claim()
        self.assertEqual(first[1], 1)
        # 模拟进程中断：execute 未执行，租约停留在 RUNNING。
        second = stocktake.claim()
        self.assertIsNone(second, '租约未过期前不应被重复领取')
        # 强制租约过期后再领，attempt 累加；结果只整体写一次。
        from datetime import timedelta
        from src.utils.time import now
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            batch.lease_until = now() - timedelta(minutes=1)
        retry = stocktake.claim()
        self.assertEqual(retry[1], 2)
        stocktake.execute(retry)
        with TestSession() as db:
            batch = db.get(StocktakeBatch, batch_id)
            self.assertEqual(batch.status, stocktake.COMPLETE)
            self.assertEqual(batch.attempt, 2)
            self.assertEqual(batch.result['matching_size'], 2)
            groups = db.scalars(select(DisputeGroup).where(DisputeGroup.batch_id == batch_id)).all()
            self.assertEqual(len(groups), 1, '重算只落一版争议组，不重复计件')

    def test_duplicate_scan_id_rejected(self):
        batch_id = self._create()
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [s('S1', ['A'])], 'analyst')
        with self.assertRaises(ValueError), TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [s('S1', ['B'])], 'analyst')

    def test_misscan_findings_and_resolution(self):
        batch_id = self._create()
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [
                s('S1', ['A']), s('S2', ['L1']), s('S3', ['R1']), s('S4', ['X']),
            ], 'analyst')
        stocktake.execute(stocktake.claim())
        with TestSession() as db:
            batch = db.get(StocktakeBatch, batch_id)
            result = batch.result
            outcomes = {item['scan_id']: item['outcome'] for item in result['scans']}
            self.assertEqual(outcomes['S2'], 'lent_misscan')
            self.assertEqual(outcomes['S3'], 'retired_misscan')
            self.assertEqual(outcomes['S4'], 'unknown_misscan')
            not_counted = [item for item in result['tools'] if item['outcome'] == 'not_counted']
            self.assertEqual({item['tool_id'] for item in not_counted}, {'B', 'C'})
            self.assertTrue(all(item['basis'] for item in result['scans']))

    def test_dispute_resolution_flow(self):
        batch_id = self._create()
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [s('S1', ['A', 'B']), s('S2', ['B', 'A'])], 'admin')
        stocktake.execute(stocktake.claim())
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            resolution = DisputeResolution(batch_id=batch_id, group_id=0, scan_id='S1',
                                           tool_id='A', note='现场确认 A 贴在 S1 扫码位', author='admin')
            db.add(resolution)
        with TestSession() as db:
            rows = db.scalars(select(DisputeResolution).where(DisputeResolution.batch_id == batch_id)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].tool_id, 'A')

    def test_failure_retries_and_new_scan_resets_attempts(self):
        batch_id = self._create()
        # 直接构造引擎必然报错的输入：扫码候选指向不存在工具不会报错，
        # 故这里通过连续三次租约失败后补扫，验证尝试计数被重置。
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [s('S1', ['A'])], 'analyst')
        # 人为把尝试次数推到上限，模拟反复中断。
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            batch.attempt = stocktake.MAX_ATTEMPTS - 1
        stocktake.execute(stocktake.claim())  # 第 3 次，成功完成而非失败
        with TestSession() as db:
            self.assertEqual(db.get(StocktakeBatch, batch_id).status, stocktake.COMPLETE)
        # 补扫带来新账，尝试计数归零，可再次正常重算。
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [s('S2', ['B'])], 'analyst')
            self.assertEqual(batch.attempt, 0)
        stocktake.execute(stocktake.claim())
        with TestSession() as db:
            batch = db.get(StocktakeBatch, batch_id)
            self.assertEqual(batch.status, stocktake.COMPLETE)
            self.assertEqual(batch.result['metrics']['confirmed'], 2)

    def test_append_after_complete_merges_groups_and_keeps_history(self):
        batch_id = self._create(tools=[t(x) for x in ('A', 'B', 'C', 'D')])
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [
                s('S1', ['A', 'B']), s('S2', ['B', 'A']),
                s('S3', ['C', 'D']), s('S4', ['D', 'C']),
            ], 'analyst')
        stocktake.execute(stocktake.claim())
        with TestSession() as db:
            self.assertEqual(len(db.get(StocktakeBatch, batch_id).result['groups']), 2)
        # 新扫码把两个旧组连到一起，重算合并为一组。
        with TestSession.begin() as db:
            batch = db.get(StocktakeBatch, batch_id)
            stocktake.add_scans(db, batch, [s('S5', ['B', 'C'])], 'analyst')
        stocktake.execute(stocktake.claim())
        with TestSession() as db:
            batch = db.get(StocktakeBatch, batch_id)
            self.assertEqual(len(batch.result['groups']), 1)
            self.assertEqual(batch.version, 3)
            # 争议组随重算整体替换，只保留最新一版且不重复计件。
            history = db.scalars(select(DisputeGroup).where(DisputeGroup.batch_id == batch_id)
                                 .order_by(DisputeGroup.group_seq)).all()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].version, 3)


if __name__ == '__main__':
    unittest.main()
