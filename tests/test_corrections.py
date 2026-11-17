import json
import pytest
from decimal import Decimal
from app import store
from app.errors import DomainError
from app.modules.m3_versions.corrections import normalize, apply as apply_corrections
from app.workflow import run
from app.main import create_app


def test_correction_does_not_touch_source_rows(raw):
    base = run(raw, {})
    corrected = run(raw, {'corrections': [{'employee': 'E001', 'shift_day': '2026-10-01',
                                           'punch': 'OUT', 'at': '2026-10-02T07:00:00+08:00'}]})
    assert corrected['rows'] == base['rows']
    assert corrected['input_count'] == base['input_count']
    assert corrected['reconcile']['balanced']


def test_missing_punch_completed_by_explicit_correction(raw):
    s = run(raw, {}, 3)
    target = next(shift for shift in s['shifts'] if shift['employee'] == 'E001')
    target['out'] = None
    state = apply_corrections(s)
    assert not state['corrections_applied']
    state['parameters'] = {'corrections': [{'employee': 'E001', 'shift_day': '2026-10-01',
                                            'punch': 'OUT', 'at': '2026-10-02T06:00:00+08:00',
                                            'reason': '补离场'}]}
    state = apply_corrections(state)
    assert len(state['corrections_applied']) == 1
    filled = next(shift for shift in state['shifts'] if shift['employee'] == 'E001')
    assert filled['out'] is not None and filled['out']['at'] == '2026-10-01T22:00:00+00:00'
    assert state['corrections_applied'][0]['before']['out'] is None


def test_break_change_isolated_to_shift_copy(raw):
    s = run(raw, {'corrections': [{'employee': 'E001', 'shift_day': '2026-10-01',
                                   'break_start': '01:00', 'break_end': '02:00'}]}, 4)
    e001 = [seg['minutes'] for seg in s['segments'] if seg['employee'] == 'E001']
    assert e001 == [120, 60, 240]
    assert sum(seg['minutes'] for seg in s['segments'] if seg['employee'] == 'E002') == 420
    e001_in = next(row for row in s['rows'] if row['employee'] == 'E001' and row['punch'] == 'IN')
    assert e001_in['break_start'] == '02:00'


def test_conflicting_corrections_rejected_not_overwritten(raw):
    s = run(raw, {'corrections': [
        {'employee': 'E001', 'shift_day': '2026-10-01', 'punch': 'OUT', 'at': '2026-10-02T07:00:00+08:00'},
        {'employee': 'E001', 'shift_day': '2026-10-01', 'punch': 'OUT', 'at': '2026-10-02T08:00:00+08:00'}]})
    assert len(s['corrections_applied']) == 1
    conflict = [item for item in s['corrections_rejected'] if item['code'] == 'CONSTRAINT']
    assert len(conflict) == 1 and conflict[0]['field'] == 'at'
    assert s['business']['minutes'] == 7620


def test_correction_without_target_or_fields_rejected(raw):
    s = run(raw, {'corrections': [
        {'employee': 'NOBODY', 'shift_day': '2026-10-01', 'punch': 'IN', 'at': '2026-10-01T22:00:00+08:00'},
        {'employee': 'E001', 'shift_day': '2026-10-01'},
        {'employee': 'E001', 'shift_day': '2026-10-01', 'punch': 'IN', 'at': '2026-10-01T12:00:00'}]})
    codes = {item['code'] for item in s['corrections_rejected']}
    assert codes == {'REFERENCE', 'RANGE', 'DATE'}
    assert s['reconcile']['balanced']


def test_correction_normalization_is_stable(raw):
    raw_corrections = [{'employee': 'E001', 'shift_day': '2026-10-01', 'punch': 'OUT',
                        'at': '2026-10-02T07:00:00+08:00'},
                       {'employee': 'E001', 'shift_day': '2026-10-01', 'punch': 'OUT',
                        'at': '2026-10-02T07:00:00+08:00'}]
    valid, rejected = normalize(raw_corrections)
    assert len(valid) == 1 and not rejected


def test_repricing_does_not_double_normal_minutes(raw):
    first = run(raw, {})
    second = run(raw, {})
    assert first['business'] == second['business']
    assert first['business']['normal_minutes'] == second['business']['normal_minutes']
    assert first['business']['minutes'] == 18 * 420


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'test.db'))
    app = create_app(); app.config['TESTING'] = True
    return app.test_client()


def test_recalculate_persists_correction_without_overwriting_original(client, tmp_path, monkeypatch):
    submitted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = submitted['run_id']
    version = submitted['review_version']
    corrected = client.post('/api/runs/' + run_id + '/recalculate', json={'parameters': {
        'corrections': [{'employee': 'E001', 'shift_day': '2026-10-01', 'punch': 'OUT',
                         'at': '2026-10-02T07:00:00+08:00', 'reason': '补离场'}]}}).get_json()
    assert corrected['active_revision'] > submitted['active_revision']
    assert corrected['business']['minutes'] == 7620
    assert corrected['review_version'] == version + 1 and corrected['decision'] == '待复核'
    stored = store.corrections(run_id)
    assert len(stored) == 1 and stored[0]['employee'] == 'E001'
    from app.workflow import source_of
    original = run(source_of(store.get(run_id)), {})
    assert original['business']['minutes'] == 7560
    # 再次提交同源输入不得覆盖已采纳的补卡。
    client.post('/api/runs', json={'sample': 'dirty.csv'})
    assert len(store.corrections(run_id)) == 1
    assert store.get(run_id)['business']['minutes'] == 7620


def test_review_log_is_append_only(client):
    submitted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = submitted['run_id']
    client.post('/api/runs/' + run_id + '/review', json={'version': 1, 'decision': '通过'})
    client.post('/api/runs/' + run_id + '/recalculate', json={'parameters': {'holidays': []}})
    history = client.get('/api/runs/' + run_id + '/reviews').get_json()
    assert [h['decision'] for h in history] == ['通过', '待复核（重算）']
    assert history[0]['version'] == 2 and history[1]['version'] == 3


def test_switch_basis_keeps_revisions_and_rolls_back_on_failure(client):
    submitted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = submitted['run_id']
    switched = client.post('/api/runs/' + run_id + '/basis', json={'basis': 'shift_day'})
    assert switched.status_code == 200
    body = switched.get_json()
    assert body['pricing_basis'] == 'shift_day' and body['business']['total'] == '7560.00'
    assert body['active_revision'] == 2
    # 基线结果仍可按修订号导出。
    old = client.get('/api/archive/' + run_id + '/export.csv?revision=1')
    assert old.status_code == 200 and old.headers['X-Archive-Revision'] == '1'
    # 非法的口径切换不改动生效版本。
    bad = client.post('/api/runs/' + run_id + '/basis', json={'basis': 'weekly'})
    assert bad.status_code == 422
    current = client.get('/api/runs/' + run_id).get_json()
    assert current['pricing_basis'] == 'shift_day' and current['active_revision'] == 2


def test_recalculate_with_bad_holiday_keeps_previous_result(client):
    submitted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = submitted['run_id']
    before = client.get('/api/runs/' + run_id).get_json()
    bad = client.post('/api/runs/' + run_id + '/recalculate', json={'parameters': {'holidays': 'not-a-list'}})
    assert bad.status_code == 422
    after = client.get('/api/runs/' + run_id).get_json()
    assert after['active_revision'] == before['active_revision']
    assert after['business'] == before['business']


def test_export_falls_back_to_active_revision(client):
    submitted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = submitted['run_id']
    client.post('/api/runs/' + run_id + '/basis', json={'basis': 'shift_day'})
    missing = client.get('/api/archive/' + run_id + '/export.csv?revision=99')
    assert missing.status_code == 200
    assert missing.headers['X-Archive-Fallback'] == 'active'
    assert missing.headers['X-Archive-Revision'] == '2'
    current = client.get('/api/archive/' + run_id + '/export.csv')
    assert current.headers['X-Archive-Fallback'] == 'none'
    assert current.headers['X-Archive-Revision'] == '2'


def test_export_contains_correction_evidence(client):
    submitted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = submitted['run_id']
    client.post('/api/runs/' + run_id + '/recalculate', json={'parameters': {
        'corrections': [{'employee': 'E001', 'shift_day': '2026-10-01', 'punch': 'OUT',
                         'at': '2026-10-02T07:00:00+08:00', 'reason': '补离场'}]}})
    text = client.get('/api/archive/' + run_id + '/export.csv').data.decode('utf-8-sig')
    assert '补卡采纳' in text and '补离场' in text
    assert '计价口径' in text


def test_recalculate_identical_result_does_not_create_revision(client):
    submitted = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()
    run_id = submitted['run_id']
    same = client.post('/api/runs/' + run_id + '/recalculate', json={'parameters': {}}).get_json()
    assert same['changed'] is False and same['active_revision'] == 1
    assert len(client.get('/api/runs/' + run_id + '/revisions').get_json()['revisions']) == 1
