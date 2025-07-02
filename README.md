# ibechange-omi-fastapi

Opportune Moment Identification (OMI) of Behavior Change Recommendations (iBeChange-2025)

## Deploy

```bash
docker compose up --build -d
docker compose exec app pytest -v
docker compose logs app
docker compose down
```

```bash
curl -X POST "http://localhost:3200/selected_contents/" -H "Content-Type: application/json" -d '{
  "selected_contents": {
    "user_01": {
      "contents": ["content_1", "content_2"],
      "mission_start_time": "2025-01-10T08:00:00",
      "mission_end_time": "2025-01-20T18:00:00"
    }
  }
}'

curl "http://localhost:3200/recommendation_plans/?start_time=2025-01-10T08:00:00&end_time=2025-01-10T22:00:00"
```
