"""扫码实盘批次的写入、并发补扫与后台重算编排。"""

import logging
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.config import database
from src.domain.stocktake.engine import reconcile, validate_scans, validate_tools
from src.models.stocktake import DisputeGroup, ScanRecord, StocktakeBatch
from src.services.audit import record
from src.services.datasets import checksum
from src.utils.time import now

log = logging.getLogger(__name__)

QUEUED = 'queued'
RUNNING = 'running'
COMPLETE = 'complete'
FAILED = 'failed'
MAX_ATTEMPTS = 3
_LEASE_MINUTES = 3


def _fingerprint(tools, scans):
    return checksum({'tools': tools, 'scans': scans})


def create_batch(db, name, snapshot_date, tool_rows, actor):
    tools = [tool.__dict__ for tool in validate_tools(tool_rows)]
    batch = StocktakeBatch(
        name=name, snapshot_date=snapshot_date, status=QUEUED, tools=tools, scans=[],
        input_checksum=_fingerprint(tools, []), author=actor, recalculating=True,
    )
    db.add(batch)
    db.flush()
    record(db, actor, 'stocktake.created', batch.id, tools=len(tools))
    return batch


def add_scans(db, batch, scan_rows, actor):
    """并发补扫：只追加流水并把批次重新排队；唯一约束拒绝重复扫码。"""
    scans = validate_scans(scan_rows)
    added = 0
    for scan in scans:
        item = ScanRecord(
            batch_id=batch.id, scan_id=scan.scan_id, tag_read=scan.tag_read,
            candidates=list(scan.candidates), source=scan.source, scanned_at=scan.scanned_at,
            input_version=batch.version, author=actor,
        )
        db.add(item)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            raise ValueError(f'扫码编号 {scan.scan_id} 已存在于本批次，请勿重复补扫。') from None
        added += 1
    # 标记输入已变化；新账给新的计算机会，重置尝试计数与历史错误。
    batch.status = QUEUED
    batch.recalculating = True
    batch.attempt = 0
    batch.last_error = None
    record(db, actor, 'stocktake.scans_appended', batch.id, added=added, version=batch.version)
    return added


def _load_scan_payload(db, batch_id):
    rows = db.scalars(select(ScanRecord).where(ScanRecord.batch_id == batch_id)
                      .order_by(ScanRecord.id)).all()
    return [{
        'scan_id': row.scan_id, 'tag_read': row.tag_read, 'candidates': row.candidates,
        'source': row.source, 'scanned_at': row.scanned_at,
    } for row in rows]


def claim():
    """后台领取待重算批次；中断后租约过期可被再次领取，计件不重复（结果整体替换）。

    返回的输入指纹在领取瞬间现算，execute 提交前再算一次比对；并发补扫一旦落库，
    指纹即变化，旧租约的计算结果被拒绝落账。
    """
    stamp = now()
    with database.Session.begin() as db:
        query = select(StocktakeBatch).where(
            (StocktakeBatch.status == QUEUED)
            | ((StocktakeBatch.status == RUNNING) & (StocktakeBatch.lease_until < stamp))
        ).order_by(StocktakeBatch.id)
        batch = db.scalar(query.limit(1).with_for_update(skip_locked=True))
        if batch is None:
            return None
        if batch.attempt >= MAX_ATTEMPTS:
            batch.status = FAILED
            batch.recalculating = False
            batch.last_error = '后台多次中断，请在补扫修正后发起重新计算'
            batch.finished_at = stamp
            record(db, 'worker', 'stocktake.failed', batch.id, reason=batch.last_error)
            return None
        batch.status = RUNNING
        batch.attempt += 1
        batch.lease_until = stamp + timedelta(minutes=_LEASE_MINUTES)
        scans = _load_scan_payload(db, batch.id)
        leased_checksum = _fingerprint(batch.tools, scans)
        return batch.id, batch.attempt, batch.tools, scans, leased_checksum, len(scans)


def execute(lease):
    batch_id, attempt, tools, scans, leased_checksum, leased_count = lease
    try:
        result = reconcile(tools, scans)
        failure = None
    except ValueError as error:
        result, failure = None, str(error)[:500]
    except Exception:
        log.exception('Stocktake %s failed', batch_id)
        result, failure = None, '对账计算失败，请核对快照与候选号口径'
    with database.Session.begin() as db:
        batch = db.get(StocktakeBatch, batch_id, with_for_update=True)
        if batch is None:
            return
        # 并发补扫已改账：本次租约基于旧输入，结果作废，等新租约按新账重算。
        current_scans = _load_scan_payload(db, batch_id)
        current_checksum = _fingerprint(batch.tools, current_scans)
        stale = (current_checksum != leased_checksum or len(current_scans) != leased_count
                 or batch.attempt != attempt or batch.status != RUNNING)
        if stale:
            # 仍有新账待算则回到排队；保留尝试次数让新租约继续。
            batch.status = QUEUED
            batch.lease_until = None
            record(db, 'worker', 'stocktake.superseded', batch_id,
                   note='补扫产生新账，旧计算结果不落账')
            return
        if failure:
            batch.lease_until = None
            batch.finished_at = now()
            batch.status = FAILED if batch.attempt >= MAX_ATTEMPTS else QUEUED
            batch.recalculating = batch.status == QUEUED
            batch.last_error = failure
            record(db, 'worker', f'stocktake.{batch.status}', batch_id, reason=failure)
            return
        # 整体替换：重算不追加计件；争议组先清后写，中断重试也不会重复计件。
        db.execute(DisputeGroup.__table__.delete().where(DisputeGroup.batch_id == batch_id))
        batch.scans = current_scans
        batch.input_checksum = current_checksum
        batch.result = result
        batch.status = COMPLETE
        batch.recalculating = False
        batch.lease_until = None
        batch.finished_at = now()
        batch.last_error = None
        batch.version += 1
        for group in result['groups']:
            db.add(DisputeGroup(
                batch_id=batch_id, version=batch.version, group_seq=group['group_id'],
                scan_ids=group['scan_ids'], tool_ids=group['tool_ids'],
                optional_pairs=group['optional_pairs'], max_pairs=group['max_pairs_in_group'],
                basis=group['basis'],
            ))
        record(db, 'worker', 'stocktake.complete', batch_id,
               version=batch.version, confirmed=result['metrics']['confirmed'],
               groups=result['metrics']['disputed_groups'])
