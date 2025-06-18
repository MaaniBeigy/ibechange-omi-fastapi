import os
import random
from datetime import datetime, timedelta
from typing import List

from dotenv import load_dotenv

load_dotenv()


def generate_random_schedule(
    start_time: datetime, end_time: datetime
) -> List[datetime]:
    min_notifications_per_day = int(os.getenv("MIN_NOTIFICATIONS_PER_DAY", 1))
    max_notifications_per_day = int(os.getenv("MAX_NOTIFICATIONS_PER_DAY", 5))
    min_notifications_per_week = int(os.getenv("MIN_NOTIFICATIONS_PER_WEEK", 5))
    max_notifications_per_week = int(os.getenv("MAX_NOTIFICATIONS_PER_WEEK", 20))
    start_hour = datetime.strptime(os.getenv("START_HOUR", "08:00"), "%H:%M").time()
    end_hour = datetime.strptime(os.getenv("END_HOUR", "22:00"), "%H:%M").time()

    schedules = []
    current_date = start_time.date()
    end_date = end_time.date()
    days = (end_date - current_date).days + 1

    # Calculate total notifications needed
    total_notifications = random.randint(
        min_notifications_per_week, max_notifications_per_week
    )
    notifications_per_day = total_notifications // days
    if notifications_per_day < min_notifications_per_day:
        notifications_per_day = min_notifications_per_day
    elif notifications_per_day > max_notifications_per_day:
        notifications_per_day = max_notifications_per_day

    while current_date <= end_date:
        for _ in range(notifications_per_day):
            # Generate random time within start_hour and end_hour
            hour = random.randint(start_hour.hour, end_hour.hour - 1)
            minute = random.randint(0, 59)
            scheduled_time = datetime.combine(
                current_date, datetime.min.time()
            ) + timedelta(hours=hour, minutes=minute)
            if start_time <= scheduled_time <= end_time:
                schedules.append(scheduled_time)
        current_date += timedelta(days=1)

    random.shuffle(schedules)
    return schedules[:total_notifications]
