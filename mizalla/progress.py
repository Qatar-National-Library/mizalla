from .models import Task


def project_completion(tasks: list[Task]) -> int:
    weighted_total = 0
    total_weight = 0
    for task in tasks:
        for step in task.steps:
            weighted_total += step.percentage_complete * task.weight
            total_weight += task.weight
    if total_weight == 0:
        return 0
    return round(weighted_total / total_weight)


def current_step(task: Task):
    return next(
        (step for step in sorted(task.steps, key=lambda item: (item.priority, item.id)) if 0 < step.percentage_complete < 100),
        None,
    )