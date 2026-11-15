"""真实单作品验收的只读观察器；观察失败绝不触发暂停、重载或重试。"""

from dataclasses import dataclass
from typing import Callable
import json
import time


class InspectionRequired(RuntimeError):
    """保留运行现场，由调用方只读核实；不代表外部任务失败。"""


@dataclass(frozen=True)
class Observation:
    ready: bool
    reason: str


def assess(current: dict, *, think_limit: int = 3) -> Observation:
    """确认同一初稿已完成评审，且耗尽本轮思考额度后才允许收尾。

    单次验收使用三次思考额度：自主立项、继续绘图、原图评审。
    预算必须在启动前由可信 Host 配置设置；本函数只验证账本证据，
    不写预算、不重放未知操作，也不将图片可用等同于整个回合结束。
    """
    works, versions, jobs = (current[key] for key in ('works', 'versions', 'jobs'))
    operations, reviews = current['operations'], current['reviews']
    if max(len(works), len(versions), len(jobs), len(reviews)) > 1:
        raise InspectionRequired('SINGLE_WORK_BOUND_EXCEEDED')
    if any(op['action'] not in {'think', 'submit'} for op in operations):
        raise InspectionRequired('UNEXPECTED_EDIT_OR_SHARE')
    thoughts = [op for op in operations if op['action'] == 'think']
    submits = [op for op in operations if op['action'] == 'submit']
    if len(thoughts) > think_limit or len(submits) > 1:
        raise InspectionRequired('OPERATION_LIMIT_EXCEEDED')
    if any(op['state'] in {'failed', 'rejected'} for op in operations):
        raise InspectionRequired('NON_SUCCESSFUL_OPERATION_PRESERVED')
    if any(op['state'] == 'unknown' for op in thoughts):
        raise InspectionRequired('MODEL_OUTCOME_UNKNOWN_PRESERVED')
    if any(job['status'] in {'failed', 'outcome_unknown'} for job in jobs):
        raise InspectionRequired('DRAWING_OUTCOME_REQUIRES_INSPECTION')
    # render unknown 可以是图片先到、原回合尚未对账；保持观察，由 Artist 查询原请求。
    if current['active_claims'] or any(op['state'] != 'succeeded' for op in operations):
        return Observation(False, 'WAIT_FOR_OPERATION_AND_CLAIM')
    if not (len(works) == len(versions) == len(jobs) == len(reviews) == len(submits) == 1):
        return Observation(False, 'WAIT_FOR_DRAW_AND_REVIEW')
    job, version, work, review, submit = jobs[0], versions[0], works[0], reviews[0], submits[0]
    if job['status'] != 'completed' or job['turn_status'] != 'completed':
        return Observation(False, 'WAIT_FOR_ORIGINAL_DRAW_TURN')
    if not (job['id'] == version['job_id'] == submit['job_id']
            and version['request_id'] == submit['request_id']
            and work['work_id'] == version['work_id'] == review['work_id'] == submit['work_id']
            and review['version_id'] == version['version_id']):
        raise InspectionRequired('ORIGINAL_DRAW_REVIEW_BINDING_MISMATCH')
    review_ops = [op for op in thoughts if op['request_id'] == review['request_id']]
    if len(review_ops) != 1:
        raise InspectionRequired('REVIEW_OPERATION_MISSING')
    operation = review_ops[0]
    params, verdict = json.loads(operation['params_json']), json.loads(review['review'])
    if (operation['work_id'] != work['work_id'] or params['task'] != 'review'
            or params['version_id'] != version['version_id']
            or json.loads(operation['evidence']) != verdict):
        raise InspectionRequired('REVIEW_RECEIPT_MISMATCH')
    decision = verdict['decision']
    settled = ((decision == 'accept' and work['status'] == 'finished')
               or (decision == 'discard' and work['status'] == 'abandoned')
               or (decision == 'revise' and work['status'] == 'planned'
                   and work['parent_version'] == version['version_id']))
    if not settled:
        return Observation(False, 'WAIT_FOR_REVIEW_APPLICATION')
    # 无剩余额度才能消除“读取空闲后、配置重载前”又启动下一次规划的竞态。
    if len(thoughts) != think_limit:
        raise InspectionRequired('THINKING_BUDGET_NOT_EXHAUSTED')
    return Observation(True, 'DRAW_REVIEW_AND_CLAIM_SETTLED')


def observe(read: Callable[[], dict], report: Callable[[dict, Observation], None], *,
            timeout: float, interval: float = 2, think_limit: int = 3,
            clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep) -> dict:
    """只读取和报告；读取、解析、报告或超时失败均原样退出，不附带生命周期动作。"""
    deadline = clock() + timeout
    while clock() < deadline:
        current = read()
        result = assess(current, think_limit=think_limit)
        report(current, result)
        if result.ready:
            return current
        sleep(interval)
    raise InspectionRequired('OBSERVATION_TIMEOUT_PRESERVE_RUNNING_TASKS')


def run_acceptance(read: Callable[[], dict], report: Callable[[dict, Observation], None],
                   finalize: Callable[[dict], None], **options) -> None:
    """唯一的收尾入口：观察成功才调用；故障路径没有 finally 清理或隐式重载。"""
    current = observe(read, report, **options)
    finalize(current)
