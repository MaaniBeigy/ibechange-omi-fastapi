# ibechange-omi-fastapi
Opportune Moment Identification (OMI) of Behavior Change Recommendations (iBeChange-2025)


## Deploy

```bash
docker compose up --build -d
docker compose exec app pytest -v
docker compose logs app
docker compose down
```