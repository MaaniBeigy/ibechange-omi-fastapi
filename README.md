[![alt text](docs/IBC-logo.jpg)](https://ibechange.eu/)
![alt text](docs/eu.png)

# ibechange-omi-fastapi

[![License: CC-BY-4.0-International](https://img.shields.io/badge/License-CC%20BY%204.0-blue.svg)](LICENSE)
[![coverage report](docs/coverage.svg)](.logs/coverage.txt)

# Data model

![data-model](docs/data_model.svg)
Opportune Moment Identification (OMI) of Behavior Change Recommendations (iBeChange-2025)

## Deploy

```bash
docker volume create metabase_data
docker compose up --build -d
docker compose exec app pytest -v
docker compose exec app pytest --cov=app --cov-report=html -v
docker compose exec app pytest --cov-report term --cov app tests/ > .logs/coverage.txt
docker compose logs app
docker compose down
```

### Selected Content

```bash
curl -X POST "http://localhost:3200/selected_contents/" -H "Content-Type: application/json" -d '{
  "474dbf4c-8922-4dac-aa35-92632a7bcaf8": {
    "contents": [
      { "id": "NRs6", "type": "resource", "mission_id": "NM31" },
      { "id": "NRc54", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc54", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc53", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc20", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc53", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc54", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc53", "type": "recommendation", "mission_id": "NM31" }
    ],
    "mission_start_time": "2025-10-17T10:00:33.919000",
    "mission_end_time": "2025-12-29T10:00:33.919000",
    "plan_id": "7b8a9f54-801a-4ab0-afe0-06f100441793"
  },
  "10b58b39-9a4c-46ad-b98d-03dc28735e6b": {
    "contents": [
      { "id": "NRc30", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc54", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc53", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc54", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc53", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc53", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc54", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc19", "type": "recommendation", "mission_id": "NM31" },
      { "id": "NRc53", "type": "recommendation", "mission_id": "NM31" }
    ],
    "mission_start_time": "2025-10-28T10:00:33.919000",
    "mission_end_time": "2025-12-31T10:00:33.919000",
    "plan_id": "3b8a9f54-701a-1ab0-afe0-05f100441796"
  }
}'

```

```bash
curl "http://localhost:3200/selected_contents/"
```


### Recommendation Plan

```bash
curl "http://localhost:3200/recommendation_plans/?start_time=2025-11-07T14:00:00&end_time=2025-11-07T18:00:00"
```

### Notification Plan

```bash
curl "http://localhost:3200/notification_plans/?start_time=2025-11-07T14:00:00&end_time=2025-11-07T18:00:00"
```

### Updates

```bash
curl -X POST "http://localhost:3200/updates/" -H "Content-Type: application/json" -d '{
    "new_users": {
      "33333333-3333-3333-3333-333333333333": {
        "gender": "female",
        "height": 165,
        "userAge": 30,
        "weight": 60,
        "wearable": "yes",
        "residence": "TestCity",
        "enrolmentDate": "2025-07-16",
        "informedConsent": "signed",
        "recruitmentCenter": "TEST",
        "level": 0
      }
    },
    "user_feedback": {
      "33333333-3333-3333-3333-333333333333": {
        "events": [
          {
            "process_id": 123,
            "timestamp": "2025-07-16T12:00:00Z",
            "event_name": "notification_sent",
            "properties": {
              "content_id": "ERc1",
              "content_type": "recommendation",
              "mission_id": "EM1"
            }
          }
        ]
      }
    },
    "disabled_users": {
      "33333333-3333-3333-3333-333333333333": {
        "date_disabled": "2025-07-12T13:00:00Z"
      }
    },
    "health_habit_assessments": {
      "33333333-3333-3333-3333-333333333333": [
        {
          "hhs": {
            "smoking": 20.0
          },
          "assessment_timestamp": "2025-07-16T12:30:00Z"
        }
      ]
    },
    "new_missions_and_contents": {
      "33333333-3333-3333-3333-333333333333": {
        "update_timestamp": "2025-07-16T12:45:00Z",
        "new_missions": [
          {
            "mission": "PM1",
            "recommendations": ["PRc1","PRc2"],
            "resources": [],
            "prescribed": false,
            "selection_timestamp": "2025-07-16T12:45:00Z",
            "finish_timestamp": "2025-07-16T13:00:00Z"
          }
        ]
      }
    },
    "escalation_level": {
      "33333333-3333-3333-3333-333333333333": [
        {
          "update_timestamp": "2025-07-16T12:50:00Z",
          "level": 1,
          "pillar_id": "EO"
        }
      ]
    }
  }'
```

```bash
curl -X POST "http://localhost:3200/updates/" \
     -H "Content-Type: application/json" \
     --data-binary @docs/example_updates_mini.json
```

### Physical Activity

```bash
curl -X POST "http://localhost:3200/physical_activity/" \
     -H "Content-Type: application/json" \
     --data-binary @docs/physical_activity_example.json
```

### Emotional Wellbeing

```bash
curl -X POST "http://localhost:3200/emotional_wellbeing/" \
     -H "Content-Type: application/json" \
     --data-binary @docs/emotional_wellbeing_example.json
```
