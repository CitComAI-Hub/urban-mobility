# Urban Mobility Digital Twin (CitCom service)

Web service of VRAIN that runs the **EU LDT Toolbox Urban Mobility** model (SUMO) as a
traffic digital twin of one zone of València, fed with the traffic counts of the
Ajuntament read through the CitCom VRAIN Data Space.

- **Now**: every 15 minutes the twin reads the counts (`TrafficFlowObserved`, offering
  `traffic-flow`), the Toolbox `SumoController` maps them to the zone network,
  routeSampler rebuilds the routes and a mesoscopic simulation gives traffic and CO₂ per
  street. The page shows how many detectors the twin reproduces (GEH < 5).
- **What if…**: close streets and change the demand (50–150 %). The same simulator runs
  the scenario with the demand of the current state: vehicles re-route every 2 minutes
  by the current travel times. The page compares trips, trip time, delay, vehicle-km and
  CO₂, and maps where traffic grows or drops.

## Reliability

Zone Camins al Grau – Poblats Marítims (2.5 km, ~140 detectors), chosen because most of the
traffic entering it is counted (`tools/calibration/find_zones.py`). On 2026-11-18/19:
GEH < 5 on 91 % of the detectors at night, 78 % at the morning peak, 83–85 % at mid-morning
and midday. In a 5-fold hold-out, streets without a detector are estimated no better than
the mean of their road class: scenarios are indicative, not forecasts.

## Run

    PYTHONPATH=src:twin ZONE_NET=... ZONE_ROUTES=... ZONE_BBOX=lon0,lat0,lon1,lat1 \
    TRAFFIC_SOURCE_URL=https://apisix-townhall-vlc.citcom-vrain-dataspace.eu DS_ISSUER_URL=... \
    gunicorn --chdir twin --workers 1 --threads 8 'server:create_app()'

`COUNTS_CSV=<file>` replaces the data space with a CSV of counts (development).
