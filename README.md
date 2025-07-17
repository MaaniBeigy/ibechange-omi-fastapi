# ibechange-omi-fastapi

Opportune Moment Identification (OMI) of Behavior Change Recommendations (iBeChange-2025)

## Deploy

```bash
docker compose up --build -d
docker compose exec app pytest -v
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
    "mission_start_time": "2025-07-07T10:00:33.919000",
    "mission_end_time": "2025-07-14T10:00:33.919000"
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
    "mission_start_time": "2025-07-07T10:00:33.919000",
    "mission_end_time": "2025-07-14T10:00:33.919000"
  }
}'

```

### Recommendation Plan

```bash
curl "http://localhost:3200/recommendation_plans/?start_time=2025-07-07T08:00:00&end_time=2025-07-07T09:00:00"
```

### Updates

```bash
curl -X POST "http://localhost:3200/updates/" -H "Content-Type: application/json" -d '{
    "new_users": {
      "33333333-3333-3333-3333-333333333333": {
        "user_id": "33333333-3333-3333-3333-333333333333",
        "characteristics": {
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
