"""补卡、休息、计划班次、时薪与假期的显式变更。

约定：
- 补卡是显式输入（parameters.corrections），绝不猜测、绝不合成原始采信行。
- 变更只作用于配对后的班次副本（_corrected 标记），原始 rows / _raw 永远保留，
  因此行数对账不受影响，导出的原始打卡证据可以完整回溯。
- 每条补卡的原始输入随证据一起留存；重算只新增批次，不覆盖既有补卡记录。
- 同一班次同一字段被两条取值不同的补卡命中时拒绝后者（CONSTRAINT），不静默覆盖。
- 同一批补卡重复提交不会重复应用（按 correction_id 幂等）。
"""
import hashlib
import json
from datetime import time as clock
from decimal import Decimal

from app.errors import DomainError
from app.modules.m2_mapping.coerce import day, instant, number

TIME_FIELDS = ('shift_start', 'shift_end', 'break_start', 'break_end')
VALUE_FIELDS = ('at',) + TIME_FIELDS + ('hourly_rate',)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def correction_id(item):
    body = {key: value for key, value in item.items() if key not in ('reason', 'raw', 'index')}
    return hashlib.sha256(canonical(body).encode('utf-8')).hexdigest()[:20]


def _normalize_one(raw):
    if not isinstance(raw, dict):
        raise DomainError('CORRECTION', '补卡记录必须是对象')
    employee = str(raw.get('employee', '')).strip()
    shift_day = str(raw.get('shift_day', '')).strip()
    if not employee or not shift_day:
        raise DomainError('REFERENCE', '补卡缺少员工或归属日')
    item = {'employee': employee, 'shift_day': day(shift_day), 'reason': str(raw.get('reason') or '补卡').strip()}
    if 'punch' in raw and str(raw['punch']).strip():
        punch = str(raw['punch']).strip().upper()
        if punch not in ('IN', 'OUT'):
            raise DomainError('RANGE', '补卡方向必须是 IN 或 OUT')
        item['punch'] = punch
    if 'at' in raw and str(raw['at']).strip():
        item['at'] = instant(str(raw['at']).strip())
    for field in TIME_FIELDS:
        if field in raw and str(raw[field]).strip():
            value = str(raw[field]).strip()
            try:
                clock.fromisoformat(value)
            except ValueError:
                raise DomainError('RANGE', '补卡时间必须是 HH:MM')
            item[field] = value
    if 'hourly_rate' in raw and str(raw['hourly_rate']).strip():
        item['hourly_rate'] = str(number(raw['hourly_rate']))
    if not any(field in item for field in VALUE_FIELDS):
        raise DomainError('RANGE', '补卡未包含任何变更字段')
    if 'at' in item and 'punch' not in item:
        raise DomainError('RANGE', '补卡时刻必须指定方向')
    return item


def normalize(raw):
    """返回 (有效补卡, 被拒绝补卡)。被拒绝项保留原输入，不进入行数对账。"""
    if raw in (None, ''):
        return [], []
    if not isinstance(raw, list):
        raise DomainError('PARAMETERS', 'corrections 必须是列表')
    valid, rejected, seen = [], [], set()
    for index, entry in enumerate(raw):
        try:
            item = _normalize_one(entry)
        except DomainError as error:
            rejected.append({'index': index, 'code': error.code, 'message': error.message, 'raw': entry,
                             'source': 'requested'})
            continue
        key = canonical(item)
        if key in seen:
            continue
        seen.add(key)
        item['index'] = index
        item['raw'] = entry
        valid.append(item)
    return valid, rejected


def _clone(row, **updates):
    copy = dict(row)
    copy.update(updates)
    copy['_corrected'] = True
    return copy


def _apply_one(shift, item):
    changed = sorted(field for field in VALUE_FIELDS if field in item)
    common = {field: item[field] for field in TIME_FIELDS + ('hourly_rate',) if field in item}
    before = {}
    for side in ('in', 'out'):
        before[side] = ({field: shift[side].get(field) for field in ('at',) + TIME_FIELDS + ('hourly_rate',)}
                        if shift[side] is not None else None)

    # 休息、计划班次与时薪是整班属性：IN/OUT 必须一致，否则 m3 的一致性校验会失败。
    for side in ('in', 'out'):
        if shift[side] is not None and common:
            shift[side] = _clone(shift[side], **common)

    punch = item.get('punch')
    if 'at' in item:
        side = 'in' if punch == 'IN' else 'out'
        current = shift[side]
        if current is None:
            template = shift['out' if side == 'in' else 'in']
            if template is None:
                raise DomainError('REFERENCE', '班次缺少可参照的打卡')
            version = int(Decimal(str(template.get('version', '1'))) + 1)
            current = _clone(template, id='CORR-' + correction_id(item), punch=punch,
                             at=item['at'], paid='0', version=str(version))
            shift[side] = current
        else:
            shift[side] = _clone(current, at=item['at'])

    after = {}
    for side in ('in', 'out'):
        if shift[side] is not None:
            after[side] = {field: shift[side].get(field) for field in ('at',) + TIME_FIELDS + ('hourly_rate',)}
    evidence = {
        'correction_id': correction_id(item),
        'employee': item['employee'],
        'shift_day': item['shift_day'],
        'shift_id': shift['id'],
        'reason': item['reason'],
        'changed': changed,
        'values': {field: item[field] for field in VALUE_FIELDS if field in item},
        'before': before,
        'after': after,
        'raw': item.get('raw', {field: item[field] for field in VALUE_FIELDS if field in item}),
    }
    if punch:
        evidence['punch'] = punch
    return evidence


def apply(state):
    """在配对后的班次上应用补卡；原始 rows 保持不变，可反复重放。"""
    parameters = state.get('parameters', {}) or {}
    valid, rejected = normalize(parameters.get('corrections'))
    shifts = {shift['id']: shift for shift in state.get('shifts', [])}
    for rejected_item in rejected:
        rejected_item['source'] = 'requested'
    applied = []
    # (shift_id, punch, field) -> 已采纳值，用于识别互相覆盖的补卡。
    claimed = {}
    for item in valid:
        shift = shifts.get(item['employee'] + '/' + item['shift_day'])
        if shift is None:
            rejected.append({'index': item.get('index'), 'code': 'REFERENCE', 'message': '未找到对应班次',
                             'raw': item.get('raw'), 'source': 'resolved'})
            continue
        conflict = False
        for field in VALUE_FIELDS:
            if field not in item:
                continue
            key = (shift['id'], item.get('punch') if field == 'at' else '', field)
            if key in claimed and claimed[key] != item[field]:
                rejected.append({'index': item.get('index'), 'code': 'CONSTRAINT',
                                 'message': '同一字段存在冲突的补卡', 'raw': item.get('raw'),
                                 'source': 'resolved',
                                 'field': field})
                conflict = True
                break
        if conflict:
            continue
        try:
            applied.append(_apply_one(shift, item))
        except DomainError as error:
            rejected.append({'index': item.get('index'), 'code': error.code, 'message': error.message,
                             'raw': item.get('raw'), 'source': 'resolved'})
            continue
        for field in VALUE_FIELDS:
            if field in item:
                claimed[(shift['id'], item.get('punch') if field == 'at' else '', field)] = item[field]
    state['corrections_requested'] = [item.get('raw') for item in valid]
    state['corrections_applied'] = applied
    state['corrections_rejected'] = rejected
    state['corrections'] = {'requested': state['corrections_requested'],
                            'applied': applied, 'rejected': rejected}
    return state
