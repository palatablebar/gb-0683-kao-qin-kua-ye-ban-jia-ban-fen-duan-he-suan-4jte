from decimal import Decimal
from app.workflow import run,load
from app.modules.m5_calculate.rules import price


def segment(day, minutes, shift_day, start, employee='E'):
    return {'id': employee + '/' + shift_day, 'shift_id': employee + '/' + shift_day, 'employee': employee,
            'shift_day': shift_day, 'calendar_day': day, 'start': start,
            'end': start, 'minutes': minutes, 'rate': '20', 'line': 2}


def test_default_basis_is_calendar_day(raw):
    s = run(raw, {})
    assert s['pricing_basis'] == 'calendar_day'
    assert s['business']['basis'] == 'calendar_day'
    assert s['business']['total'] == '3960.00'


def test_calendar_day_splits_night_shift(raw):
    s = run(raw, {})
    rows = [r for r in s['calculations'] if r['employee'] == 'E001']
    assert sum(Decimal(r['amount']) for r in rows) == Decimal('220.00')
    assert [r['code'] for r in rows] == ['HOLIDAY', 'REGULAR', 'REGULAR']
    assert {r['calendar_day'] for r in rows} == {'2026-10-01', '2026-10-02'}
    assert {r['shift_day'] for r in rows} == {'2026-10-01'}


def test_shift_day_basis_keeps_legacy_result(raw):
    # 整班套用归属日费率会把次日 5 小时也算成假日 3 倍。
    s = run(raw, {'pricing_basis': 'shift_day'})
    assert s['pricing_basis'] == 'shift_day'
    assert s['business']['total'] == '7560.00'
    rows = [r for r in s['calculations'] if r['employee'] == 'E001']
    assert all(r['code'] == 'HOLIDAY' for r in rows)
    assert sum(r['minutes'] for r in rows) == 420
    assert {r['shift_day'] for r in rows} == {'2026-10-01'}
    assert sum(Decimal(r['amount']) for r in rows) == Decimal('420.00')


def test_unknown_basis_rejected(raw):
    from app.errors import DomainError
    import pytest
    with pytest.raises(DomainError) as e:
        run(raw, {'pricing_basis': 'weekly'})
    assert e.value.code == 'PARAMETERS'


def test_segments_keep_shift_link_under_calendar_basis(raw):
    s = run(raw, {})
    for row in s['calculations']:
        assert row['shift_id'] == row['id'] == row['employee'] + '/' + row['shift_day']


def test_calendar_pricing_uses_holiday_param(raw):
    s = run(raw, {'holidays': []})
    # 10-01 与 10-02 都是工作日：各日历日正常额度独立计算，7 小时全部 1 倍。
    rows = [r for r in s['calculations'] if r['employee'] == 'E001']
    assert not any(r['code'] == 'HOLIDAY' for r in rows)
    assert all(r['code'] == 'REGULAR' for r in rows)
    assert sum(Decimal(r['amount']) for r in rows) == Decimal('140.00')


def test_normal_quota_shared_across_two_segments_same_day():
    a = segment('2026-10-02', 300, '2026-10-01', '2026-10-02T00:00:00+08:00')
    b = segment('2026-10-02', 300, '2026-10-02', '2026-10-02T15:00:00+08:00')
    rows = price([a, b], set(), 'calendar_day')
    overtime = [r for r in rows if r['code'] == 'OVERTIME']
    assert sum(r['minutes'] for r in overtime) == 120
    assert sum(r['minutes'] for r in rows if r['code'] == 'REGULAR') == 480


def test_repricing_is_not_cumulative():
    a = segment('2026-10-02', 540, '2026-10-02', '2026-10-02T09:00:00+08:00')
    first = price([a], set(), 'calendar_day')
    second = price([a], set(), 'calendar_day')
    assert first == second
    assert sum(r['minutes'] for r in second if r['code'] == 'REGULAR') == 480


def test_quota_resets_next_calendar_day():
    a = segment('2026-10-02', 540, '2026-10-02', '2026-10-02T09:00:00+08:00')
    b = segment('2026-10-05', 540, '2026-10-05', '2026-10-05T09:00:00+08:00')
    rows = price([a, b], set(), 'calendar_day')
    assert sum(r['minutes'] for r in rows if r['code'] == 'REGULAR') == 960
