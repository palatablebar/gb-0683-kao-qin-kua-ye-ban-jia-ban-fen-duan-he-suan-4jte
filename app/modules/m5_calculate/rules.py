"""工时切段计价。

计价口径（parameters.pricing_basis）：
- calendar_day（默认，裁决口径）：按实际日历日判断法定假日、周末并切分倍率；
  片段保留 shift_day / shift_id，工资证据仍能对应回原班次。
- shift_day（兼容口径）：整班套用归属日的日历类型，仅用于与旧结果对比。

两种口径下，8 小时正常工时额度都按“同一员工同一实际日历日”累计，跨班片段共享，
因此同一员工同一天的两段工作不会各得一份额度，重跑也不会重复累计。
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict

from app.errors import DomainError

BASIS_CALENDAR = 'calendar_day'
BASIS_SHIFT = 'shift_day'
BASES = (BASIS_CALENDAR, BASIS_SHIFT)
NORMAL_MINUTES = 480


def resolve_basis(parameters):
    basis = (parameters or {}).get('pricing_basis', BASIS_CALENDAR)
    if basis not in BASES:
        raise DomainError('PARAMETERS', 'pricing_basis 只能是 calendar_day 或 shift_day')
    return basis


def _rate_day(segment, basis):
    return segment['calendar_day'] if basis == BASIS_CALENDAR else segment['shift_day']


def price(segments, holidays, basis=BASIS_CALENDAR):
    if basis not in BASES:
        raise DomainError('PARAMETERS', '计价口径无效')
    normal_used = defaultdict(int)
    out = []
    for s in sorted(segments, key=lambda r: (r['start'], r['employee'], r.get('shift_day', ''))):
        rate_day = _rate_day(s, basis)
        d = date.fromisoformat(rate_day)
        # 8 小时正常额度始终按“同一员工同一实际日历日”累计，与计价口径无关，
        # 因此同一天的两段工作（不论班次归属日）共享额度，重跑也不会重复累计。
        quota_key = (s['employee'], s['calendar_day'])
        minutes = s['minutes']
        rate = Decimal(str(s['rate']))
        if rate_day in holidays:
            parts = [(minutes, Decimal(3), 'HOLIDAY')]
        elif d.weekday() >= 5:
            parts = [(minutes, Decimal(2), 'REST')]
        else:
            regular = min(minutes, max(0, NORMAL_MINUTES - normal_used[quota_key]))
            normal_used[quota_key] += minutes
            parts = [(regular, Decimal(1), 'REGULAR'), (minutes - regular, Decimal('1.5'), 'OVERTIME')]
        for count, multiplier, code in parts:
            if count:
                item = dict(s)
                item.update(shift_id=s['id'], rate_day=rate_day, basis=basis,
                            minutes=count, multiplier=str(multiplier), code=code,
                            amount=str((rate * count * multiplier / 60).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)))
                out.append(item)
    return out


def resolve_holidays(parameters):
    raw = (parameters or {}).get('holidays', ['2026-10-01'])
    if isinstance(raw, str) or not isinstance(raw, (list, tuple, set)):
        raise DomainError('PARAMETERS', 'holidays 必须是日期列表')
    holidays = set()
    for value in raw:
        try:
            holidays.add(date.fromisoformat(str(value).strip()).isoformat())
        except ValueError:
            raise DomainError('DATE', '假日日期无效')
    return holidays


def apply(state):
    parameters = state['parameters']
    basis = resolve_basis(parameters)
    holidays = resolve_holidays(parameters)
    calculations = price(state['segments'], holidays, basis)
    total = sum((Decimal(r['amount']) for r in calculations), Decimal(0))
    # 正常工时额度按员工+费率日封顶，重算前后由同一条计算路径产生，不会累加两次。
    normal_minutes = sum(r['minutes'] for r in calculations if r['code'] == 'REGULAR')
    paid = sum((Decimal(str(r['paid'])) for r in state['rows']), Decimal(0))
    state['pricing_basis'] = basis
    state['holidays'] = sorted(holidays)
    state['calculations'] = calculations
    state['business'] = {'total': str(total), 'paid': str(paid), 'difference': str(total - paid),
                         'minutes': sum(r['minutes'] for r in calculations),
                         'normal_minutes': normal_minutes, 'basis': basis,
                         'unit': 'CNY'}
    return state
