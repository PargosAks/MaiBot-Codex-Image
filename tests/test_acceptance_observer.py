"""验收观察器的取消竞态回归；所有时间线为内存替身，不接触真实 Host。"""

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import sys

import pytest


SPEC = spec_from_file_location('acceptance_observer', Path(__file__).parents[1] / 'scripts/acceptance_observer.py')
observer = module_from_spec(SPEC)
sys.modules[SPEC.name] = observer
SPEC.loader.exec_module(observer)


@pytest.fixture
def complete():
    verdict = json.dumps({'decision': 'accept', 'reason': 'original image reviewed'})
    return {
        'works': [{'work_id': 'work', 'status': 'finished', 'parent_version': None}],
        'versions': [{'version_id': 'version', 'work_id': 'work', 'job_id': 'job', 'request_id': 'render'}],
        'jobs': [{'id': 'job', 'status': 'completed', 'turn_status': 'completed'}],
        'operations': [
            {'request_id': 'plan1', 'action': 'think', 'state': 'succeeded', 'work_id': None,
             'params_json': json.dumps({'task': 'plan'})},
            {'request_id': 'plan2', 'action': 'think', 'state': 'succeeded', 'work_id': None,
             'params_json': json.dumps({'task': 'plan'})},
            {'request_id': 'render', 'action': 'submit', 'state': 'succeeded', 'work_id': 'work', 'job_id': 'job'},
            {'request_id': 'review', 'action': 'think', 'state': 'succeeded', 'work_id': 'work',
             'params_json': json.dumps({'task': 'review', 'version_id': 'version'}), 'evidence': verdict},
        ],
        'reviews': [{'request_id': 'review', 'work_id': 'work', 'version_id': 'version', 'review': verdict}],
        'active_claims': 0,
    }


def test_render_unknown_then_review_must_not_trigger_early_put(complete):
    """复现旧事故：第一次读到空闲时图已到达，但原回执和下一次评审尚未完成。"""
    unknown = deepcopy(complete)
    unknown['works'][0]['status'] = 'review_pending'
    unknown['operations'] = unknown['operations'][:-1]
    unknown['operations'][2]['state'] = 'unknown'
    unknown['reviews'] = []
    in_review = deepcopy(complete)
    in_review['works'][0]['status'] = 'review_pending'
    in_review['operations'][-1]['state'] = 'submitting'
    in_review['reviews'] = []
    in_review['active_claims'] = 1
    recorded = deepcopy(complete)
    recorded['active_claims'] = 1
    timeline = iter([unknown, in_review, recorded, complete])
    writes, reports = [], []
    def read():
        assert writes == []
        return next(timeline)
    observer.run_acceptance(read, lambda state, decision: reports.append(decision.ready),
                            lambda state: writes.append(('pause', state)),
                            timeout=20, clock=lambda: 0, sleep=lambda _: None)
    # 直接覆盖运行入口；前面的暂时空闲、评审在途和领取未退出都不能调用收尾。
    assert reports == [False, False, False, True]
    assert writes == [('pause', complete)]


@pytest.mark.parametrize('status,turn', [('running', 'running'), ('artifact_ready', 'running'),
                                       ('artifact_ready', 'completed')])
def test_image_and_review_do_not_replace_original_draw_terminal(complete, status, turn):
    complete['jobs'][0].update(status=status, turn_status=turn)
    assert not observer.assess(complete).ready


@pytest.mark.parametrize('state', ['reserved', 'submitting', 'accepted'])
def test_wait_for_inflight_review_even_if_review_row_visible(complete, state):
    complete['operations'][-1]['state'] = state
    assert not observer.assess(complete).ready


@pytest.mark.parametrize('failure', ['unknown', 'failed', 'rejected'])
def test_model_uncertainty_or_failure_never_enters_pause_path(complete, failure):
    complete['operations'][-1]['state'] = failure
    writes = []
    with pytest.raises(observer.InspectionRequired):
        observer.run_acceptance(lambda: complete, lambda *_: None, writes.append, timeout=10)
    assert writes == []


@pytest.mark.parametrize('field', ['job_id', 'request_id', 'work_id', 'version_id'])
def test_original_image_or_review_binding_mismatch_rejected(complete, field):
    complete['versions'][0][field] = 'other'
    with pytest.raises(observer.InspectionRequired):
        observer.assess(complete)


def test_review_receipt_must_match_successful_operation(complete):
    complete['operations'][-1]['evidence'] = json.dumps({'decision': 'discard'})
    with pytest.raises(observer.InspectionRequired, match='REVIEW_RECEIPT'):
        observer.assess(complete)


def test_review_row_waits_for_work_update(complete):
    complete['works'][0]['status'] = 'review_pending'
    assert not observer.assess(complete).ready


def test_idle_claim_without_exhausted_budget_is_not_safe_to_reload(complete):
    complete['operations'].pop(0)
    with pytest.raises(observer.InspectionRequired, match='BUDGET_NOT_EXHAUSTED'):
        observer.assess(complete)


@pytest.mark.parametrize('action', ['share', 'edit'])
def test_new_render_or_send_is_not_accepted(complete, action):
    complete['operations'].append({'action': action, 'state': 'reserved'})
    with pytest.raises(observer.InspectionRequired):
        observer.assess(complete)


def test_observer_timeout_has_no_lifecycle_callback(complete):
    complete['active_claims'] = 1
    ticks = iter([0, 0, 2])
    writes = []
    with pytest.raises(observer.InspectionRequired, match='PRESERVE_RUNNING_TASKS'):
        observer.run_acceptance(lambda: complete, lambda *_: None, writes.append, timeout=1,
                               clock=lambda: next(ticks), sleep=lambda _: None)
    assert writes == []


def test_database_read_error_is_not_a_business_failure():
    def broken_read():
        raise OSError('temporary read failure')
    writes = []
    with pytest.raises(OSError, match='temporary read failure'):
        observer.run_acceptance(broken_read, lambda *_: None, writes.append, timeout=10)
    assert writes == []


def test_report_failure_does_not_continue_or_recover(complete):
    def broken_report(*_):
        raise OSError('report unavailable')
    with pytest.raises(OSError, match='report unavailable'):
        observer.observe(lambda: complete, broken_report, timeout=10)
