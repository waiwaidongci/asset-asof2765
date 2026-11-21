from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.stocktake import DisputeResolution, ScanRecord, StocktakeBatch
from src.schemas.requests import DisputeResolveRequest, StocktakeBatchRequest, StocktakeScanRequest
from src.services import stocktake
from src.services.audit import record
from src.utils.time import iso
from src.utils.wakeup import wake

router = APIRouter(prefix='/stocktake')

_STOCKTAKE_STATUS = {
    stocktake.QUEUED: '排队重算', stocktake.RUNNING: '正在计算',
    stocktake.COMPLETE: '对账完成', stocktake.FAILED: '计算失败',
}


def batch_view(batch, *, detail=False):
    view = {
        'id': batch.id, 'name': batch.name, 'snapshot_date': batch.snapshot_date,
        'status': batch.status, 'status_label': _STOCKTAKE_STATUS.get(batch.status, batch.status),
        'version': batch.version, 'attempt': batch.attempt, 'author': batch.author,
        'recalculating': batch.recalculating, 'last_error': batch.last_error,
        'created_at': iso(batch.created_at), 'updated_at': iso(batch.updated_at),
        'finished_at': iso(batch.finished_at),
        'tool_count': len(batch.tools), 'scan_count': len(batch.scans),
    }
    if detail:
        view['tools'] = batch.tools
        view['scans_payload'] = batch.scans
        view['result'] = batch.result
    return view


def require_batch(db, batch_id, lock=False):
    batch = db.get(StocktakeBatch, batch_id, with_for_update=lock)
    if not batch:
        raise HTTPException(404, '找不到该盘点批次')
    return batch


@router.get('/batches')
def batches(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(StocktakeBatch).order_by(StocktakeBatch.id.desc()).limit(100))
    return [batch_view(item) for item in items]


@router.post('/batches', status_code=202)
def create(body: StocktakeBatchRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        batch = stocktake.create_batch(db, body.name, body.snapshot_date, body.tools, actor.username)
        db.commit()
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    wake()
    return batch_view(batch, detail=True)


@router.get('/batches/{batch_id}')
def detail(batch_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    batch = require_batch(db, batch_id)
    view = batch_view(batch, detail=True)
    view['scan_ledger'] = [
        {'scan_id': row.scan_id, 'tag_read': row.tag_read, 'candidates': row.candidates,
         'source': row.source, 'scanned_at': row.scanned_at, 'input_version': row.input_version,
         'author': row.author, 'created_at': iso(row.created_at)}
        for row in db.scalars(select(ScanRecord).where(ScanRecord.batch_id == batch_id)
                              .order_by(ScanRecord.id))
    ]
    view['resolutions'] = [
        {'group_id': row.group_id, 'scan_id': row.scan_id, 'tool_id': row.tool_id,
         'note': row.note, 'author': row.author, 'created_at': iso(row.created_at)}
        for row in db.scalars(select(DisputeResolution).where(DisputeResolution.batch_id == batch_id)
                              .order_by(DisputeResolution.id))
    ]
    return view


@router.post('/batches/{batch_id}/scans', status_code=202)
def append_scans(batch_id: int, body: StocktakeScanRequest, db: Session = Depends(connection),
                 actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    batch = require_batch(db, batch_id, lock=True)
    try:
        added = stocktake.add_scans(db, batch, body.scans, actor.username)
        db.commit()
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    wake()
    return {'added': added, 'batch': batch_view(batch)}


@router.post('/batches/{batch_id}/rebuild', status_code=202)
def rebuild(batch_id: int, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    batch = require_batch(db, batch_id, lock=True)
    batch.status = stocktake.QUEUED
    batch.recalculating = True
    batch.attempt = 0
    batch.last_error = None
    record(db, actor.username, 'stocktake.rebuild_requested', batch.id, version=batch.version)
    db.commit()
    wake()
    return batch_view(batch)


@router.get('/batches/{batch_id}/findings')
def findings(batch_id: int, outcome: str | None = Query(None),
             db: Session = Depends(connection), actor: User = Depends(current)):
    """盘点页面的分类查询：未盘到、借出/退役/无效号误扫，均带判定依据。"""
    batch = require_batch(db, batch_id)
    if not batch.result:
        raise HTTPException(409, '该批次尚未完成对账，请等待后台重算')
    allowed = {'confirmed', 'disputed', 'not_counted', 'lent_misscan',
               'retired_misscan', 'unknown_misscan'}
    if outcome is not None and outcome not in allowed:
        raise HTTPException(422, '结论类型不在支持范围内')
    result = batch.result
    data = {
        'headline': result['headline'],
        'metrics': result['metrics'],
        'confirmed': result['confirmed'],
        'groups': result['groups'],
        'scans': result['scans'],
        'tools': result['tools'],
        'offline_tools': result['offline_tools'],
    }
    if outcome:
        data['filter'] = outcome
        data['scans'] = [item for item in result['scans'] if item['outcome'] == outcome]
        if outcome == 'not_counted':
            data['tools'] = [item for item in result['tools'] if item['outcome'] == 'not_counted']
    return data


@router.post('/batches/{batch_id}/resolve')
def resolve(batch_id: int, body: DisputeResolveRequest, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN))):
    batch = require_batch(db, batch_id, lock=True)
    if not batch.result:
        raise HTTPException(409, '批次尚无对账结果，无法核示')
    group = next((item for item in batch.result['groups'] if item['group_id'] == body.group_id), None)
    if group is None:
        raise HTTPException(422, '指定的待核组不存在或已在重算后合并')
    if body.scan_id not in group['scan_ids'] or body.tool_id not in group['tool_ids']:
        raise HTTPException(422, '核示配对必须取自该待核组的扫码与工具')
    if not any(pair['scan_id'] == body.scan_id and pair['tool_id'] == body.tool_id
               for pair in group['optional_pairs']):
        raise HTTPException(422, '该扫码与工具不存在候选关系，无法核示')
    item = db.scalar(select(DisputeResolution).where(
        DisputeResolution.batch_id == batch_id, DisputeResolution.group_id == body.group_id)
        .with_for_update())
    if item:
        item.scan_id, item.tool_id, item.note, item.author = body.scan_id, body.tool_id, body.note, actor.username
    else:
        item = DisputeResolution(batch_id=batch_id, group_id=body.group_id, scan_id=body.scan_id,
                                 tool_id=body.tool_id, note=body.note, author=actor.username)
        db.add(item)
    record(db, actor.username, 'stocktake.resolved', batch_id,
           group_id=body.group_id, scan_id=body.scan_id, tool_id=body.tool_id)
    db.commit()
    return {'group_id': body.group_id, 'scan_id': body.scan_id, 'tool_id': body.tool_id}
