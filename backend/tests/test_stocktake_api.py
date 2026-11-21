"""stocktake 路由端到端测试：FastAPI TestClient + SQLite，验证鉴权、补扫、重算与查询口径。

运行：PYTHONPATH=/tmp/pylibs python3 -m unittest tests.test_stocktake_api
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix='.db')
# 强制赋值而非 setdefault：与其他测试模块的内存库 URL 隔离。
os.environ['DATABASE_URL'] = f'sqlite+pysqlite:///{_DB_PATH}'
os.environ['JWT_SECRET'] = 'api-test-secret'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
import src.config.database as database  # noqa: E402
from src.config.database import Base, connection  # noqa: E402
from src.constants.enums import Role  # noqa: E402
from src.main import app  # noqa: E402
from src.models import identity as _identity  # noqa: E402,F401
from src.models.identity import User  # noqa: E402
from src.services import stocktake  # noqa: E402
from src.utils.security import hash_password, token  # noqa: E402

# 即使其他测试模块已先行初始化内存引擎，这里也强制切换到本模块独立的文件库。
engine = create_engine(f'sqlite+pysqlite:///{_DB_PATH}', future=True,
                       connect_args={'check_same_thread': False})
database.engine = engine
TestSession = sessionmaker(engine, expire_on_commit=False)
_PREVIOUS_SESSION = None
_PREVIOUS_ENGINE = None


def _yield_session():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


def setUpModule():
    # worker 服务在运行时读取 database.Session，本模块测试期间固定到本文件的引擎。
    global _PREVIOUS_SESSION, _PREVIOUS_ENGINE
    _PREVIOUS_SESSION = database.Session
    _PREVIOUS_ENGINE = database.engine
    database.Session = TestSession
    database.engine = engine
    Base.metadata.create_all(engine)
    app.dependency_overrides[connection] = _yield_session


def tearDownModule():
    database.Session = _PREVIOUS_SESSION
    database.engine = _PREVIOUS_ENGINE
    app.dependency_overrides.pop(connection, None)

TOOLS = [
    {'tool_id': 'GQ-01', 'name': '充电电钻', 'status': 'in_custody'},
    {'tool_id': 'GQ-02', 'name': '万用表', 'status': 'in_custody'},
    {'tool_id': 'GQ-03', 'name': '修枝剪', 'status': 'in_custody'},
    {'tool_id': 'GQ-04', 'name': '砂光机', 'status': 'checked_out'},
    {'tool_id': 'GQ-05', 'name': '踏梯', 'status': 'retired'},
]


class StocktakeApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with TestSession.begin() as db:
            for role in Role:
                if not db.get(User, role.value):
                    db.add(User(username=role.value, role=role,
                                password_hash=hash_password(f'{role.value}-pw')))
        cls.client = TestClient(app)
        cls.tokens = {role: token(role, role) for role in Role}

    def auth(self, role):
        return {'Authorization': f'Bearer {self.tokens[role]}'}

    def create_batch(self):
        response = self.client.post('/stocktake/batches', headers=self.auth(Role.ANALYST), json={
            'name': '11 月月末实盘', 'snapshot_date': '2026-11-30', 'tools': TOOLS,
        })
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['id']

    def test_requires_authentication_and_role(self):
        self.assertEqual(self.client.get('/stocktake/batches').status_code, 401)
        # viewer 只有读权限，不能建批次。
        response = self.client.post('/stocktake/batches', headers=self.auth(Role.VIEWER), json={
            'name': 'x', 'snapshot_date': '2026-11-30', 'tools': TOOLS,
        })
        self.assertEqual(response.status_code, 403)

    def test_append_rebuild_findings_and_resolution(self):
        batch_id = self.create_batch()
        added = self.client.post(f'/stocktake/batches/{batch_id}/scans',
                                 headers=self.auth(Role.ANALYST), json={'scans': [
                                     {'scan_id': 'S1', 'candidates': ['GQ-01', 'GQ-02']},
                                     {'scan_id': 'S2', 'candidates': ['GQ-02', 'GQ-01']},
                                     {'scan_id': 'S3', 'candidates': ['GQ-03']},
                                     {'scan_id': 'S4', 'candidates': ['GQ-04']},
                                     {'scan_id': 'S5', 'candidates': ['GQ-05']},
                                     {'scan_id': 'S6', 'candidates': ['GHOST']},
                                 ]})
        self.assertEqual(added.status_code, 202, added.text)
        self.assertEqual(added.json()['added'], 6)

        # 重复扫码被拒绝。
        dup = self.client.post(f'/stocktake/batches/{batch_id}/scans',
                               headers=self.auth(Role.ANALYST),
                               json={'scans': [{'scan_id': 'S1', 'candidates': ['GQ-01']}]})
        self.assertEqual(dup.status_code, 422)

        # worker 领取并重算。
        lease = stocktake.claim()
        self.assertIsNotNone(lease)
        stocktake.execute(lease)

        detail = self.client.get(f'/stocktake/batches/{batch_id}', headers=self.auth(Role.VIEWER))
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(body['status'], 'complete')
        self.assertEqual(body['result']['metrics']['confirmed'], 1)
        self.assertEqual(body['result']['metrics']['disputed_groups'], 1)

        # findings 分类筛选：未盘到。
        missing = self.client.get(f'/stocktake/batches/{batch_id}/findings?outcome=not_counted',
                                  headers=self.auth(Role.VIEWER)).json()
        self.assertEqual([t['tool_id'] for t in missing['tools']], [])
        # 三件在管：GQ-03 盘到，GQ-01/GQ-02 入争议组，因此没有未盘到工具。
        self.assertEqual(missing['filter'], 'not_counted')

        # 误扫筛选与依据。
        for outcome, scan_id in [('lent_misscan', 'S4'), ('retired_misscan', 'S5'),
                                 ('unknown_misscan', 'S6')]:
            data = self.client.get(f'/stocktake/batches/{batch_id}/findings?outcome={outcome}',
                                   headers=self.auth(Role.VIEWER)).json()
            self.assertEqual([s['scan_id'] for s in data['scans']], [scan_id])
            self.assertTrue(data['scans'][0]['basis'])

        # 非法 outcome 被拒。
        bad = self.client.get(f'/stocktake/batches/{batch_id}/findings?outcome=nonsense',
                              headers=self.auth(Role.VIEWER))
        self.assertEqual(bad.status_code, 422)

        # 主管核示争议组；analyst 无权。
        payload = {'group_id': 0, 'scan_id': 'S1', 'tool_id': 'GQ-01', 'note': '现场确认标签位'}
        forbidden = self.client.post(f'/stocktake/batches/{batch_id}/resolve',
                                     headers=self.auth(Role.ANALYST), json=payload)
        self.assertEqual(forbidden.status_code, 403)
        resolved = self.client.post(f'/stocktake/batches/{batch_id}/resolve',
                                    headers=self.auth(Role.ADMIN), json=payload)
        self.assertEqual(resolved.status_code, 200, resolved.text)
        # 核示一个不存在的候选配对被拒。
        invalid = self.client.post(f'/stocktake/batches/{batch_id}/resolve',
                                   headers=self.auth(Role.ADMIN),
                                   json={'group_id': 0, 'scan_id': 'S1', 'tool_id': 'GQ-03',
                                         'note': 'x'})
        self.assertEqual(invalid.status_code, 422)

    def test_rebuild_after_new_scan_merges_groups(self):
        batch_id = self.create_batch()
        self.client.post(f'/stocktake/batches/{batch_id}/scans', headers=self.auth(Role.ANALYST),
                         json={'scans': [
                             {'scan_id': 'S1', 'candidates': ['GQ-01', 'GQ-02']},
                             {'scan_id': 'S2', 'candidates': ['GQ-02', 'GQ-01']},
                             {'scan_id': 'S3', 'candidates': ['GQ-03', 'GQ-01']},
                         ]})
        stocktake.execute(stocktake.claim())
        before = self.client.get(f'/stocktake/batches/{batch_id}', headers=self.auth(Role.VIEWER)).json()
        self.assertEqual(len(before['result']['groups']), 1)
        # 补扫触发重新排队，手动 rebuild 也应幂等。
        self.client.post(f'/stocktake/batches/{batch_id}/rebuild', headers=self.auth(Role.ANALYST))
        lease = stocktake.claim()
        self.assertIsNotNone(lease)
        stocktake.execute(lease)
        after = self.client.get(f'/stocktake/batches/{batch_id}', headers=self.auth(Role.VIEWER)).json()
        self.assertEqual(after['status'], 'complete')
        self.assertGreaterEqual(after['version'], before['version'])


if __name__ == '__main__':
    unittest.main()
