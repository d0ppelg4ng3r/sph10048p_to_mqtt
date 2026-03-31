import time
from task_scheduler import TaskScheduler


def task_a():
    print(f"[A] tick at {time.strftime('%H:%M:%S')}")


def task_b(count=[0]):
    count[0] += 1
    print(f"[B] count={count[0]} at {time.strftime('%H:%M:%S')}")


if __name__ == '__main__':
    sched = TaskScheduler()
    sched.add_task(task_a, interval=2.0, name='task_a', run_immediately=True)
    sched.add_task(task_b, interval=3.0, name='task_b')

    print('Starting scheduler for 10 seconds...')
    sched.start()

    try:
        time.sleep(10)
    finally:
        print('Stopping scheduler...')
        sched.stop()
        print('Stopped.')
