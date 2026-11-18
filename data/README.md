# SYNTHETIC DATA - NOT REAL

Every ping, driver id, road and zone in this folder is invented by
`python -m gpsqa.generate --seed 20260310`. The coordinates sit around a
FICTIONAL origin in open ocean (35.0N, 150.0E). No real person, vehicle,
place or company is represented.

- `pings.csv`: input for billing, in arrival order
- `answer_key.csv`: fault label per ping (do not feed to billing)
- `truth_road.csv`: true road distance in zone per driver/local day
- `expected_km.csv`: written by `python -m gpsqa.expected`

Driver speeds (km/h, constant per driver): DRV-01=34.64, DRV-02=59.93, DRV-03=54.93, DRV-04=43.73, DRV-05=36.42
